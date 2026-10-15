"""Late-book recaps retain the review gate without paying for impossible input shapes.

Every runtime is explicitly offline. Long memory below is synthetic, stored only in pytest's
temporary directory; no provider credentials, saved books, or real completions are accessed.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.api._epub import epub_ncx
from app.config import Settings
from app.cost import estimate_input_tokens
from app.eval.spoiler_gate.judge import JUDGE_SYSTEM, JudgeVerdict, judge_prompt
from app.eval.spoiler_gate.synthesis import flowing_system_for, supplied_facts, synth_prompt
from app.main import create_app


class InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"),
        openai_api_key=None, anthropic_api_key=None, openai_base_url=None,
        embed_api_key=None, embed_base_url=None, embed_provider="stub",
        embed_model="lexical-stub-256", llm_cheap_model="gpt-4o-mini",
        llm_large_model="gpt-4o-mini",
    )
    book = epub_ncx([
        ("one.xhtml", "Chapter I", "Chapter I", "Aldric worked at the forge. " * 12),
        ("two.xhtml", "Chapter II", "Chapter II",
         ("Berenice handed the copper lantern to Aldric. "
          "Berenice stayed at the forge and watched Aldric. ") * 20),
        ("three.xhtml", "Chapter III", "Chapter III", "Corvus revealed the future verdict. " * 12),
    ])
    app = create_app(settings, ingest_executor=InlineExecutor())
    with TestClient(app) as api:
        imported = api.post("/api/books", files={
            "file": ("synthetic.epub", book, "application/epub+zip"),
        })
        assert imported.status_code == 201
        book_id = imported.json()["book_id"]
        with app.state.store.book(book_id) as mem:
            mem.set_book_profile(book_type="novel", confidence=1.0,
                                 detector_version="test-fixture", signals=[])
        _segmentation, chapters = app.state.worker._segmented(book_id)
        advanced = api.put(f"/api/books/{book_id}/position", json={
            "cfi": "second", "offset": sum(len(ch["text"]) for ch in chapters[:2]) + 2,
        })
        assert advanced.status_code == 200
        assert app.state.catalog.get_state(book_id)["ingest_progress"] == 2
        calls = []
        client = app.state.client

        def complete(system, user, tier="cheap", schema=None):
            # Direct pure stub logic remains offline even when a cache test changes the reported
            # provider identity. No SDK, environment lookup, or network adapter is reachable here.
            value, usage = client._complete_stub(system, user, tier, schema, 4096)
            calls.append({"system": system, "user": user, "tier": tier,
                          "schema": schema, "value": value})
            return value, usage

        monkeypatch.setattr(client, "complete", complete)
        settings.cost_max_input_tokens_per_call = 8000
        yield SimpleNamespace(
            api=api, app=app, settings=settings, book_id=book_id, client=client,
            catalog=app.state.catalog, chapters=chapters, calls=calls,
        )


def _recap(runtime, bookmark=2):
    return runtime.api.get(f"/api/books/{runtime.book_id}/catch-me-up", params={"bookmark": bookmark})


def _facts(runtime, bookmark=2):
    with runtime.app.state.store.book(runtime.book_id) as mem:
        return supplied_facts(mem, bookmark)


def _syntheses(runtime):
    return [call for call in runtime.calls if call["schema"] is None and call["tier"] == "large"]


def _add_long_event_history(runtime):
    with runtime.app.state.store.book(runtime.book_id) as mem:
        with mem.transaction():
            for index in range(200):
                mem.add_event("Berenice handed the copper lantern to Aldric.", 2,
                              order_idx=100 + index, participants=[])
            mem.add_event("Berenice stayed at the forge and watched Aldric.", 2,
                          order_idx=1000, participants=[])


@pytest.mark.parametrize("warm_prior", [False, True])
def test_long_history_uses_cumulative_exact_evidence_subset_and_caches(runtime, warm_prior):
    if warm_prior:
        prior = _recap(runtime, 1)
        assert prior.status_code == 200 and prior.json()["recap"]
    _add_long_event_history(runtime)
    if warm_prior:
        count = len(runtime.calls)
        assert _recap(runtime, 1).json()["cached"] is True
        assert len(runtime.calls) == count, "future history must not invalidate an earlier recap"
    runtime.calls.clear()
    facts = _facts(runtime)
    selected = {key: facts[key] for key in ("characters", "chapter_summaries")}
    selected["events"] = facts["events"][:20]
    assert estimate_input_tokens(JUDGE_SYSTEM, judge_prompt("", facts), JudgeVerdict) > 8000
    assert estimate_input_tokens(JUDGE_SYSTEM, judge_prompt("", selected), JudgeVerdict) < 8000

    response = _recap(runtime)
    assert response.status_code == 200
    payload = response.json()
    assert payload["recap"] and payload["as_of_chapter"] == 2 and payload["cached"] is False
    [generation] = _syntheses(runtime)
    assert "as you last told it" not in generation["user"]
    assert generation["user"] == synth_prompt(2, selected, book_type="novel")
    reviewed = [call for call in runtime.calls
                if call["schema"] is JudgeVerdict and call["tier"] == "large"]
    assert len(reviewed) == 1
    assert reviewed[0]["user"] == judge_prompt(generation["value"], selected)
    event_section = reviewed[0]["user"].split("KEY EVENTS:\n", 1)[1].split("\n\nRECAP TO REVIEW:", 1)[0]
    assert "Berenice stayed at the forge and watched Aldric." not in event_section
    assert all("Corvus" not in call["user"] and "future verdict" not in call["user"]
               for call in runtime.calls)
    assert all(call["tier"] == "large" for call in runtime.calls), (
        "the optional now-line must not pay when its minimum full-facts review cannot fit")
    assert payload["now"] is None
    count = len(runtime.calls)
    assert _recap(runtime).json()["cached"] is True
    assert len(runtime.calls) == count
    assert runtime.catalog.get_cost_reservations(runtime.book_id) == []


@pytest.mark.parametrize("warm_prior", [False, True])
def test_oversized_cumulative_source_refuses_before_any_provider_io_or_cost(runtime, warm_prior):
    if warm_prior:
        assert _recap(runtime, 1).status_code == 200
    with runtime.app.state.store.book(runtime.book_id) as mem:
        mem.reextract_summary(runtime.chapters[1]["key"], 2, "Aldric worked at the forge. " * 500)
    runtime.calls.clear()
    costs_before = runtime.catalog.get_costs(runtime.book_id)
    response = _recap(runtime)
    assert response.status_code == 429
    assert response.json()["detail"] == "recap evidence exceeds the per-call input ceiling"
    assert runtime.calls == []
    assert runtime.catalog.get_costs(runtime.book_id) == costs_before
    assert runtime.catalog.get_cost_reservations(runtime.book_id) == []


def test_actual_recap_overflow_stops_before_review_without_retry_or_cache(runtime, monkeypatch):
    seen = []
    costs_before = len(runtime.catalog.get_costs(runtime.book_id))

    def large_recap(system, user, tier="cheap", schema=None):
        seen.append((tier, schema))
        assert schema is None and tier == "large", "an oversized recap must never reach review IO"
        return "Aldric worked at the forge. " * 400, {"in": 11, "out": 500}

    monkeypatch.setattr(runtime.client, "complete", large_recap)
    response = _recap(runtime)
    assert response.status_code == 429
    assert response.json()["detail"] == "recap review exceeds the per-call input ceiling"
    assert seen == [("large", None)]
    new_costs = runtime.catalog.get_costs(runtime.book_id)[costs_before:]
    assert len(new_costs) == 1 and new_costs[0]["phase"] == "synthesis"
    assert new_costs[0]["input_tokens"] == 11 and new_costs[0]["output_tokens"] == 500
    assert new_costs[0]["usd"] > 0
    assert runtime.app.state.recaps.stats()["entries"] == 0
    assert runtime.catalog.get_cost_reservations(runtime.book_id) == []


@pytest.mark.parametrize("change", ["model", "provider"])
def test_current_model_and_provider_identity_invalidate_recap_cache_without_repinning(runtime, change):
    with runtime.app.state.store.book(runtime.book_id) as mem:
        original_pin = mem.pinned_identity()["synth_model"]
    assert _recap(runtime).json()["cached"] is False
    assert _recap(runtime).json()["cached"] is True
    assert len(_syntheses(runtime)) == 1
    if change == "model":
        runtime.client.large_model = "gpt-5.4"
    else:
        runtime.client.provider = "anthropic"  # the fixture adapter remains the pure offline stub
    changed = _recap(runtime)
    assert changed.status_code == 200 and changed.json()["cached"] is False
    assert len(_syntheses(runtime)) == 2
    assert _recap(runtime).json()["cached"] is True
    assert len(_syntheses(runtime)) == 2
    with runtime.app.state.store.book(runtime.book_id) as mem:
        assert mem.pinned_identity()["synth_model"] == original_pin


def test_unchanged_actual_model_keeps_earlier_recap_cached_after_later_ingest(runtime):
    assert _recap(runtime).status_code == 200
    assert len(_syntheses(runtime)) == 1
    advanced = runtime.api.put(f"/api/books/{runtime.book_id}/position", json={
        "cfi": "third", "offset": sum(len(ch["text"]) for ch in runtime.chapters),
    })
    assert advanced.status_code == 200
    assert runtime.catalog.get_state(runtime.book_id)["ingest_progress"] == 3
    count = len(runtime.calls)
    assert _recap(runtime, 2).json()["cached"] is True
    assert len(runtime.calls) == count


def test_compatible_structured_review_headroom_is_preflighted_before_synthesis(runtime):
    facts = _facts(runtime)
    runtime.client.provider = "openai-compatible"
    runtime.client._is_native_openai = False
    minimum = estimate_input_tokens(JUDGE_SYSTEM, judge_prompt("", facts), JudgeVerdict)
    runtime.settings.cost_max_input_tokens_per_call = (
        minimum + runtime.settings.cost_max_output_tokens_per_call + 511
    )
    system = flowing_system_for("novel", "und")
    assert estimate_input_tokens(system, synth_prompt(2, facts, book_type="novel")) < (
        runtime.settings.cost_max_input_tokens_per_call)
    costs_before = runtime.catalog.get_costs(runtime.book_id)
    response = _recap(runtime)
    assert response.status_code == 429
    assert runtime.calls == []
    assert runtime.catalog.get_costs(runtime.book_id) == costs_before
    assert runtime.catalog.get_cost_reservations(runtime.book_id) == []
