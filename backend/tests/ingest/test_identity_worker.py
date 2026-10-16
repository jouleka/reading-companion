"""The optional identity route stays outside locks and inside chapter spend/publication fences."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.cost import CostCeilingExceeded
from app.ingest.extraction.pipeline import StalePreparedRoster, all_entities
from app.llm.jev import JevUnavailable
from app.main import create_app
from tests.api._epub import epub_ncx
from tests.ingest._grounding import canned_grounding_verdict
from tests.ingest.test_grounding_repair import InlineExecutor, _advance, _assert_no_pending, _memory
from tests.test_jev import answer


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(_env_file=None, data_dir=str(tmp_path), llm_provider="stub", allow_stub=True)
    book = epub_ncx([
        ("one.xhtml", "Chapter I", "Chapter I", "Aldric worked at the forge. "*12),
        ("two.xhtml", "Chapter II", "Chapter II", "Aldric, now called Rowan, repaired the lantern. "*12),
        ("three.xhtml", "Chapter III", "Chapter III", "Corvus concealed the future verdict. "*12),
    ])
    app = create_app(settings, ingest_executor=InlineExecutor())
    with TestClient(app) as api:
        response = api.post("/api/books", files={"file": ("synthetic.epub", book, "application/epub+zip")})
        assert response.status_code == 201
        bid = response.json()["book_id"]
        _, chapters = app.state.worker._segmented(bid)
        assert api.put(f"/api/books/{bid}/position", json={"cfi": "one",
            "offset": len(chapters[0]["text"])+2}).status_code == 200
        assert app.state.catalog.get_state(bid)["ingest_progress"] == 1
        yield SimpleNamespace(api=api, app=app, settings=settings, worker=app.state.worker,
            book_id=bid, client=app.state.client, catalog=app.state.catalog, chapters=chapters)


def extraction():
    return {"chapter_summary": "Rowan repaired the lantern.", "entities": [{
        "canonical_name": "Rowan", "type": "character", "aliases": [],
        "matched_roster": False, "state": "repairing the lantern"}],
        "relationships": [], "events": [{"summary": "Rowan repaired the lantern.",
                                          "participants": ["Rowan"]}], "themes": []}


@pytest.mark.parametrize("mode", ["accept", "uncertain", "outage", "gpt_rejects", "gpt_invalid",
                                    "gpt_outage", "budget", "shutdown", "disabled", "missing_jev"])
def test_identity_review_is_budgeted_atomic_frontier_bounded_and_independent(runtime, monkeypatch, mode):
    runtime.settings.jev_identity_enabled = mode != "disabled"
    baseline, calls, jev_calls = _memory(runtime), [], []
    def assert_unlocked():
        assert not runtime.app.state.store._lock_for(runtime.book_id).locked()
        assert _memory(runtime) == baseline
    def evaluate(state, questions):
        assert_unlocked()
        assert "Corvus" not in json.dumps(state) and "future verdict" not in json.dumps(state)
        assert "entity_id" not in json.dumps(state) and "repairing the lantern" not in json.dumps(state)
        jev_calls.append(state)
        if mode == "outage":
            raise JevUnavailable("private-provider-error")
        if mode == "shutdown":
            runtime.worker._stopping.set()
        return {k: answer(q["criteria"], choice="uncertain" if mode == "uncertain" else "same", p=.999)
                for k, q in questions.items()}, {"in": 100, "out": 20}
    if mode != "missing_jev":
        runtime.client.jev = SimpleNamespace(model="jev-1.13.0", evaluate=evaluate, close=lambda: None)
    def complete(system, user, *, tier, schema):
        assert_unlocked()
        assert "Corvus" not in system+user
        calls.append(schema.__name__)
        if schema.__name__ == "Extraction":
            return extraction(), {"in": 11, "out": 5}
        if schema.__name__ == "ExtractionVerdict":
            return canned_grounding_verdict(user), {"in": 11, "out": 5}
        assert schema.__name__ == "IdentityVerdict"
        assert "confidence" not in user and '"choice"' not in user
        if mode == "gpt_outage":
            raise RuntimeError("private-provider-error")
        return {"same_identity": mode != "gpt_rejects", "passage_ids": [999] if mode == "gpt_invalid" else [1]}, {"in": 11, "out": 5}
    monkeypatch.setattr(runtime.client, "complete", complete)
    if mode == "budget":
        def exhausted(*a, **k):
            raise CostCeilingExceeded("book budget")
        monkeypatch.setattr("app.ingest.worker.budgeted_jev", exhausted)
    status = _advance(runtime)
    if mode in {"budget", "shutdown"}:
        assert _memory(runtime) == baseline
        assert runtime.catalog.get_state(runtime.book_id)["ingest_progress"] == 1
        assert status["status"] != "done"
    else:
        assert status["status"] == "done"
        assert calls.count("ExtractionVerdict") == 1
        with runtime.app.state.store.book(runtime.book_id) as mem:
            characters = mem.view(2).entities_of_type("character")
            assert len(characters) == (1 if mode == "accept" else 2)
            assert len(mem.view(1).entities_of_type("character")) == 1
            assert all("Rowan" != a["surface_form"] for e in mem.view(1).entities_of_type("character")
                       for a in mem.view(1).aliases_of(e["entity_id"]))
    assert bool(jev_calls) == (mode not in {"disabled", "missing_jev", "budget"})
    ledger = runtime.catalog.get_costs(runtime.book_id)
    rows = [r for r in ledger if r["phase"].startswith("jev-identity") or r["phase"].startswith("identity-review")]
    assert all(r["chapter_ordinal"] == 2 for r in rows)
    assert sum(r["phase"].startswith("jev-identity") for r in rows) == len(jev_calls)
    assert calls.count("IdentityVerdict") == int(mode in {"accept", "gpt_rejects", "gpt_invalid", "gpt_outage"})
    _assert_no_pending(runtime)


def test_roster_correction_repeats_identity_review_outside_lock_without_reextracting(runtime, monkeypatch):
    from app.ingest import worker as module
    runtime.settings.jev_identity_enabled = True
    observed, calls = [], []
    def evaluate(state, questions):
        assert not runtime.app.state.store._lock_for(runtime.book_id).locked()
        observed.append(state)
        return {k: answer(q["criteria"], choice="same", p=.999) for k, q in questions.items()}, {"in": 100, "out": 20}
    runtime.client.jev = SimpleNamespace(model="jev-1.13.0", evaluate=evaluate, close=lambda: None)
    def complete(system, user, *, tier, schema):
        calls.append(schema.__name__)
        if schema.__name__ == "Extraction":
            return extraction(), {"in": 11, "out": 5}
        if schema.__name__ == "ExtractionVerdict":
            return canned_grounding_verdict(user), {"in": 11, "out": 5}
        return {"same_identity": True, "passage_ids": [1]}, {"in": 11, "out": 5}
    monkeypatch.setattr(runtime.client, "complete", complete)
    original = module.ingest_chapter
    commits = []
    def corrected(mem, ch, prepared):
        commits.append(prepared)
        if len(commits) == 1:
            roster = all_entities(mem.view(1))
            mem.add_alias(roster[0]["entity_id"], "Smith", 1)
            raise StalePreparedRoster("simulated user correction")
        return original(mem, ch, prepared)
    monkeypatch.setattr(module, "ingest_chapter", corrected)
    assert _advance(runtime)["status"] == "done"
    assert calls.count("Extraction") == calls.count("ExtractionVerdict") == 1
    assert calls.count("IdentityVerdict") == len(observed) == 2
    assert observed[0]["prior_visible_identities"][0]["aliases"] == []
    assert observed[1]["prior_visible_identities"][0]["aliases"] == ["Smith"]
    assert commits[0]["resolution_clearance"] != commits[1]["resolution_clearance"]
    _assert_no_pending(runtime)


def test_optional_paid_identity_attempts_are_capped_across_preparation_retries(runtime, monkeypatch):
    from app.ingest import worker as module
    runtime.settings.jev_identity_enabled = True
    calls = []
    def evaluate(state, questions):
        calls.append(state)
        return {k: answer(q["criteria"], choice="uncertain") for k, q in questions.items()}, {"in": 100, "out": 20}
    runtime.client.jev = SimpleNamespace(model="jev-1.13.0", evaluate=evaluate, close=lambda: None)
    def complete(system, user, *, tier, schema):
        return (extraction() if schema.__name__ == "Extraction" else canned_grounding_verdict(user)), {"in": 11, "out": 5}
    monkeypatch.setattr(runtime.client, "complete", complete)
    original = module.prepare_chapter
    def many_candidates(ch, value, client, **kwargs):
        # Exercise admission independently of extractor deduplication, on the exact current source.
        for _ in range(12):
            assert kwargs["identity_review"](ch["text"], value["entities"][0], kwargs["roster"]) is None
        return original(ch, value, client, **kwargs)
    monkeypatch.setattr(module, "prepare_chapter", many_candidates)
    assert _advance(runtime)["status"] == "done"
    assert len(calls) == 8
    _assert_no_pending(runtime)
