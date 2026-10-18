"""Primary recap proof is independent of omissions in derived event memory; no network."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.eval.spoiler_gate.synthesis import read_text_upto, score_recap
from app.main import create_app
from _epub import epub_ncx
from test_ingest import InlineExecutor
from test_views import env as env, _supported_citation_audit


@pytest.fixture
def event_env(tmp_path):
    settings = Settings(_env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"))
    app = create_app(settings, ingest_executor=InlineExecutor())
    book = epub_ncx([
        ("one.xhtml", "Chapter I", "Chapter I", "Aldric arrived at the forge. " * 12),
        ("two.xhtml", "Chapter II", "Chapter II", "Aldric was arrested at the forge. " * 12),
        ("three.xhtml", "Chapter III", "Chapter III", "Corvus crossed the valley. " * 12),
    ])
    with TestClient(app) as api:
        imported = api.post("/api/books", files={"file": ("book.epub", book, "application/epub+zip")})
        assert imported.status_code == 201
        bid = imported.json()["book_id"]
        atoms = json.loads((Path(settings.data_dir) / "books" / bid / "atoms.json").read_text())["atoms"]
        progress = api.put(f"/api/books/{bid}/position", json={
            "cfi": "chapter-three", "offset": sum(a["char_len"] for a in atoms[:2]) + 2,
        })
        assert progress.status_code == 200 and progress.json()["bookmark"] == 2
        with app.state.store.book(bid) as mem:
            # Simulate old extraction that omitted an action. Original EPUB, raw text and
            # completion hashes stay intact, so this is a valid completed primary snapshot.
            with mem._writer():
                mem._conn.execute("UPDATE chapter_summaries SET summary=?", ("Aldric worked at the forge.",))
                mem._conn.execute("UPDATE events SET summary=?", ("Aldric worked at the forge.",))
            assert mem.completion_frontier(atoms) == 2
        yield api, bid, app, atoms


@pytest.mark.parametrize("fault", [None, "unsupported", "missing", "judge"])
def test_primary_event_missing_from_derived_memory_requires_both_source_reviews(event_env, monkeypatch, fault):
    api, bid, app, _atoms = event_env
    client = app.state.reader_client
    source_text = "Aldric was arrested at the forge."
    text = "Aldric was killed at the forge." if fault in {"unsupported", "judge"} else source_text
    with app.state.store.book(bid) as mem:
        score = score_recap(mem, 2, text, read_text=read_text_upto(mem, 2))
        assert not score["future_entity_leaks"] and not score["prolepsis_hits"]
        assert any(hit["family"] == ("homicide" if text != source_text else "arrest")
                   for hit in score["unsupported_event_bindings"])

    monkeypatch.setattr(client, "provider", "openai-compatible")
    calls = []

    def complete(system, user, tier="cheap", schema=None, **_kwargs):
        if schema is None:
            calls.append("synthesis")
            assert source_text in user and "SOURCE PASSAGES" in user
            return text, {"in": 20, "out": 8}
        data = json.loads(user.split("(untrusted JSON data):\n")[1])
        calls.append(schema.__name__)
        if schema.__name__ == "AskEvidenceReview":
            source = next(source for source in data["sources"]
                          if any(source_text in passage["text"] for passage in source["passages"]))
            value = _supported_citation_audit(data, [source["source_id"]])
            if fault == "unsupported":
                value["claim_audits"][0]["units"][0].update(
                    supported=False, evidence=[], diagnostic="The source does not prove this action.",
                )
            if fault == "missing":
                value["claim_audits"].clear()
            return value, {"in": 20, "out": 20}
        assert schema.__name__ == "AskVerdict"
        assert data["claims"][0]["claim"] == text
        assert any(source_text in source["text"] for source in data["claims"][0]["cited_passages"])
        return {"references_future": False,
                "unsupported_claims": ["wrong actor"] if fault == "judge" else [],
                "missing_details": []}, {"in": 20, "out": 8}

    monkeypatch.setattr(client, "complete", complete)
    response = api.get(f"/api/books/{bid}/catch-me-up")
    if fault is None:
        assert response.status_code == 200, response.text
        assert response.json()["recap"] == text
        assert calls == ["synthesis", "AskEvidenceReview", "AskVerdict"]
    else:
        assert response.status_code == 502
        assert text not in response.text and "wrong actor" not in response.text
        attempt = ["synthesis", "AskEvidenceReview"] + (["AskVerdict"] if fault == "judge" else [])
        assert calls == attempt * 2
        assert app.state.recaps.stats()["entries"] == 0
    assert app.state.catalog.get_cost_reservations(bid) == []


@pytest.mark.parametrize(("text", "signal"), [
    ("Corvus was arrested at the forge.", "future_entity_leaks"),
    ("Aldric will be arrested at the forge.", "prolepsis_hits"),
])
def test_primary_recap_keeps_future_names_and_future_tense_hard_gated(env, monkeypatch, text, signal):
    api, bid, app, _atoms = env
    client = app.state.reader_client
    with app.state.store.book(bid) as mem:
        mem.add_entity("Corvus", "character", revealed_at=3)
        assert score_recap(mem, 2, text, read_text=read_text_upto(mem, 2))[signal]
    monkeypatch.setattr(client, "provider", "openai-compatible")
    calls = []

    def complete(system, user, tier="cheap", schema=None, **_kwargs):
        assert schema is None, "A deterministic future disclosure must never reach source clearance"
        calls.append("synthesis")
        return text, {"in": 20, "out": 8}

    monkeypatch.setattr(client, "complete", complete)
    response = api.get(f"/api/books/{bid}/catch-me-up")
    assert response.status_code == 502 and text not in response.text
    assert calls == ["synthesis", "synthesis"]
    assert app.state.recaps.stats()["entries"] == 0
