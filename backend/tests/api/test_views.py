"""Module E / view routes (ADR 0007 D-A10/D-A11): graph / timeline / notes / search / catch-me-up.
EVERY bookmark-taking route clamps server-side to the version-current high-water (never trusting the
client — the D-A10 falsifiability test asks for bookmark=99 and must get the high-water view); search
embeds the query BEFORE entering the per-book lock; catch-me-up is lazy synthesis behind the LIT-8
runtime gate with the validity-snapshot cache."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from _epub import three_chapter_book
from app.config import Settings
from app.llm.client import ProviderUnavailable
from app.main import create_app
from test_ingest import InlineExecutor


@pytest.fixture
def env(tmp_path):
    settings = Settings(_env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"))
    app = create_app(settings, ingest_executor=InlineExecutor())
    with TestClient(app) as c:
        bid = c.post("/api/books",
                     files={"file": ("b.epub", three_chapter_book(), "application/epub+zip")}
                     ).json()["book_id"]
        with open(os.path.join(settings.data_dir, "books", bid, "atoms.json"), encoding="utf-8") as f:
            atoms = json.load(f)["atoms"]
        # read through chapter 2 -> chapters 1-2 ingested, high-water bookmark = 2
        off = atoms[0]["char_len"] + atoms[1]["char_len"] + 2
        c.put(f"/api/books/{bid}/position", json={"cfi": "x", "offset": off})
        yield c, bid, app, atoms


def test_graph_is_bookmark_bounded_and_clamped(env):
    c, bid, _, _ = env
    g2 = c.get(f"/api/books/{bid}/graph").json()               # default = high-water (2)
    names2 = {ch["canonical_name"] for ch in g2["characters"]}
    assert any("Aldric" in n for n in names2)
    assert any("Berenice" in n for n in names2)
    assert not any("Corvus" in n for n in names2)              # ch3 not read -> hidden
    g1 = c.get(f"/api/books/{bid}/graph", params={"bookmark": 1}).json()
    names1 = {ch["canonical_name"] for ch in g1["characters"]}
    assert any("Aldric" in n for n in names1) and not any("Berenice" in n for n in names1)
    # D-A10 falsifiability: a client asking for bookmark=99 gets the HIGH-WATER view, not the book
    g99 = c.get(f"/api/books/{bid}/graph", params={"bookmark": 99}).json()
    assert g99["as_of_chapter"] == 2
    assert not any("Corvus" in ch["canonical_name"] for ch in g99["characters"])


def test_graph_coalesces_legacy_honorific_duplicates_without_losing_relationships(env):
    c, bid, app, _ = env
    with app.state.store.book(bid) as mem:
        zossima = mem.add_entity("Zossima", "character", revealed_at=1)
        father_zossima = mem.add_entity("Father Zossima", "character", revealed_at=2)
        aldric = next(row["entity_id"] for row in mem.view(2).characters()
                      if row["canonical_name"] == "Aldric")
        mem.add_edge(father_zossima, aldric, "guidance", "counsels", revealed_at=2)

    graph = c.get(f"/api/books/{bid}/graph", params={"bookmark": 2}).json()
    zossimas = [character for character in graph["characters"]
                if "zossima" in character["canonical_name"].lower()]
    assert len(zossimas) == 1
    assert zossimas[0]["canonical_name"] == "Zossima"
    assert "Father Zossima" in zossimas[0]["aliases"]
    assert any(edge["src_entity"] == zossima and edge["dst_entity"] == aldric
               for edge in graph["relationships"])


def test_graph_includes_only_bookmark_visible_aliases(env):
    c, bid, app, _ = env
    with app.state.store.book(bid) as mem:
        aldric = next(ch for ch in mem.view(2).characters() if "Aldric" in ch["canonical_name"])
        mem.add_alias(aldric["entity_id"], "the valley smith", revealed_at=1)
        mem.add_alias(aldric["entity_id"], "Master Aldric", revealed_at=2)

    g1 = c.get(f"/api/books/{bid}/graph", params={"bookmark": 1}).json()
    aldric1 = next(ch for ch in g1["characters"] if ch["entity_id"] == aldric["entity_id"])
    assert aldric1["aliases"] == ["the valley smith"]

    g2 = c.get(f"/api/books/{bid}/graph", params={"bookmark": 2}).json()
    aldric2 = next(ch for ch in g2["characters"] if ch["entity_id"] == aldric["entity_id"])
    assert aldric2["aliases"] == ["the valley smith", "Master Aldric"]


def test_reader_memory_correction_is_exact_frontier_and_spoiler_safe_history(env):
    c, bid, _, _ = env
    graph = c.get(f"/api/books/{bid}/graph", params={"bookmark": 2}).json()
    aldric = next(character for character in graph["characters"]
                  if character["canonical_name"] == "Aldric")
    response = c.post(f"/api/books/{bid}/memory-corrections", json={
        "source_entity_id": aldric["entity_id"],
        "canonical_name": "Aldric Vale",
        "reason": "The completed chapters establish the full name.",
        "bookmark": 2,
    })
    assert response.status_code == 200
    item = response.json()["items"][-1]
    assert item["source_entities"] == [{"entity_id": aldric["entity_id"], "name": "Aldric"}]
    assert item["target_entities"][0]["name"] == "Aldric Vale"
    assert item["effective_at"] == 2

    past_graph = c.get(f"/api/books/{bid}/graph", params={"bookmark": 1}).json()
    assert "Aldric" in {character["canonical_name"] for character in past_graph["characters"]}
    assert c.get(f"/api/books/{bid}/memory-corrections",
                 params={"bookmark": 1}).json()["items"] == []
    current_graph = c.get(f"/api/books/{bid}/graph", params={"bookmark": 2}).json()
    assert "Aldric Vale" in {
        character["canonical_name"] for character in current_graph["characters"]
    }
    stale = c.post(f"/api/books/{bid}/memory-corrections", json={
        "source_entity_id": item["target_entities"][0]["entity_id"],
        "canonical_name": "Aldric Other",
        "reason": "stale frontier",
        "bookmark": 99,
    })
    assert stale.status_code == 409


def test_timeline_and_notes_are_bounded(env):
    c, bid, _, _ = env
    tl = c.get(f"/api/books/{bid}/timeline", params={"bookmark": 99}).json()
    assert tl["as_of_chapter"] == 2
    assert all(ev["revealed_at"] <= 2 for ev in tl["events"])
    notes = c.get(f"/api/books/{bid}/notes").json()
    assert [n["revealed_at"] for n in notes["chapters"]] == [1, 2]


def test_bad_bookmark_params_fail_closed(env):
    c, bid, _, _ = env
    for bad in ("abc", "1.5", "-1", "true"):
        r = c.get(f"/api/books/{bid}/graph", params={"bookmark": bad})
        assert r.status_code == 422, f"bookmark={bad!r} must be rejected, got {r.status_code}"


def test_search_embeds_before_the_lock_and_is_bounded(env):
    c, bid, app, _ = env
    store, client = app.state.store, app.state.client
    orig = client.embed
    seen = []

    def probed(texts):
        seen.append(store._lock_for(bid).locked())
        return orig(texts)

    client.embed = probed
    try:
        r = c.get(f"/api/books/{bid}/search", params={"q": "the magistrate judgment", "bookmark": 99})
    finally:
        client.embed = orig
    assert r.status_code == 200
    assert seen and not any(seen), "the query embed ran under the per-book lock (D-A3 violation)"
    hits = r.json()["hits"]
    assert hits and all(h["revealed_at"] <= 2 for h in hits)   # clamped to high-water


def test_ask_the_book_returns_cited_completed_passages_and_measured_cost(env):
    c, bid, app, _ = env
    original = app.state.client.complete

    def answering(system, user, tier="cheap", schema=None):
        fields = getattr(schema, "model_fields", {}) if schema is not None else {}
        if "insufficient_evidence" in fields:
            return {
                "insufficient_evidence": False,
                "claims": [{
                    "text": "Berenice met Aldric at the forge and they spoke.",
                    "citation_ids": [1],
                }],
            }, {"in": 20, "out": 12}
        if "references_future" in fields:
            return {"references_future": False, "unsupported_claims": []}, {"in": 12, "out": 3}
        return original(system, user, tier=tier, schema=schema)

    app.state.client.complete = answering
    try:
        response = c.post(
            f"/api/books/{bid}/ask",
            json={"question": "Where did Berenice meet Aldric?", "bookmark": 99},
        )
    finally:
        app.state.client.complete = original

    assert response.status_code == 200
    payload = response.json()
    assert payload["as_of_chapter"] == 2
    assert payload["claims"][0]["citation_ids"] == [1]
    assert payload["citations"][0]["ordinal"] <= 2
    assert payload["citations"][0]["href"].endswith(".xhtml")
    assert payload["cost"]["payer"] == "local offline engine"
    assert payload["cost"]["pricing_known"] is True
    assert payload["cost"]["input_tokens"] >= 32


def test_ask_the_book_withholds_an_uncited_future_answer_without_leaking_it(env):
    c, bid, app, _ = env
    original = app.state.client.complete

    def future_answer(system, user, tier="cheap", schema=None):
        fields = getattr(schema, "model_fields", {}) if schema is not None else {}
        if "insufficient_evidence" in fields:
            return {
                "insufficient_evidence": False,
                "claims": [{
                    "text": "Corvus murdered Berenice after the judgment.",
                    "citation_ids": [1],
                }],
            }, {"in": 2, "out": 2}
        return original(system, user, tier=tier, schema=schema)

    app.state.client.complete = future_answer
    try:
        response = c.post(f"/api/books/{bid}/ask", json={"question": "What happens next?"})
    finally:
        app.state.client.complete = original

    assert response.status_code == 502
    assert "corvus" not in response.text.casefold()
    assert "murdered" not in response.text.casefold()


def test_selection_actions_use_only_the_visible_selection_and_return_its_anchor(env):
    c, bid, app, _ = env
    original = app.state.client.complete

    def assisting(system, user, tier="cheap", schema=None):
        fields = getattr(schema, "model_fields", {}) if schema is not None else {}
        if "citation_ids" in fields:
            return {
                "insufficient_evidence": False,
                "text": "Magistrate means a civil official in this passage.",
                "citation_ids": [1],
            }, {"in": 15, "out": 8}
        if "references_future" in fields:
            return {"references_future": False, "unsupported_claims": []}, {"in": 9, "out": 3}
        return original(system, user, tier=tier, schema=schema)

    app.state.client.complete = assisting
    try:
        response = c.post(
            f"/api/books/{bid}/selection-action",
            json={
                "action": "define",
                "text": "magistrate",
                "atom": 3,
                "cfi": "epubcfi(/6/6!/4/2)",
            },
        )
    finally:
        app.state.client.complete = original

    assert response.status_code == 200
    payload = response.json()
    assert payload["text"].startswith("Magistrate means")
    assert payload["citation"]["ordinal"] == 3
    assert payload["citation"]["cfi"] == "epubcfi(/6/6!/4/2)"
    assert payload["cost"]["input_tokens"] == 24
    outside = c.post(
        f"/api/books/{bid}/selection-action",
        json={"action": "explain", "text": "future", "atom": 4, "cfi": "epubcfi(/6/8)"},
    )
    assert outside.status_code == 422


def test_chapter_closeout_is_exact_chapter_cited_and_completed_only(env):
    c, bid, app, _ = env
    original = app.state.client.complete

    def closing_out(system, user, tier="cheap", schema=None):
        fields = getattr(schema, "model_fields", {}) if schema is not None else {}
        if "claims" in fields:
            return {
                "insufficient_evidence": False,
                "claims": [{
                    "text": "Berenice met Aldric at the forge and they spoke.",
                    "citation_ids": [1],
                }],
            }, {"in": 30, "out": 10}
        if "references_future" in fields:
            return {"references_future": False, "unsupported_claims": []}, {"in": 12, "out": 3}
        return original(system, user, tier=tier, schema=schema)

    app.state.client.complete = closing_out
    try:
        response = c.post(f"/api/books/{bid}/chapter-closeout", json={"chapter": 2})
    finally:
        app.state.client.complete = original
    assert response.status_code == 200
    payload = response.json()
    assert payload["chapter"] == payload["as_of_chapter"] == 2
    assert payload["claims"][0]["citation_ids"] == [1]
    assert payload["citations"][0]["ordinal"] == 2
    assert payload["citations"][0]["href"].endswith("c2.xhtml")

    future = c.post(f"/api/books/{bid}/chapter-closeout", json={"chapter": 3})
    assert future.status_code == 409
    assert "Corvus" not in future.text


def test_catch_me_up_synthesizes_lazily_and_caches(env):
    c, bid, app, atoms = env
    client = app.state.client
    synths = []
    orig = client.complete

    def counting(system, user, tier="cheap", schema=None):
        if tier == "large" and schema is None:                 # count SYNTH calls (the judge is separate)
            synths.append(1)
        return orig(system, user, tier=tier, schema=schema)

    client.complete = counting
    try:
        r1 = c.get(f"/api/books/{bid}/catch-me-up").json()
        r2 = c.get(f"/api/books/{bid}/catch-me-up").json()
    finally:
        client.complete = orig
    assert r1["recap"] and r1["as_of_chapter"] == 2 and r1["cast_size"] >= 2
    assert r2["recap"] == r1["recap"]
    assert len(synths) == 1, f"second request must be a cache HIT, got {len(synths)} synth calls"


def test_catch_me_up_cache_survives_a_future_chapter_ingest(env):
    # ingesting ch3 must NOT invalidate the bm=2 recap (the pass-3 churn fixes are what make a
    # lazy-synthesis cache affordable)
    c, bid, app, atoms = env
    client = app.state.client
    calls = []
    orig = client.complete

    def counting(*a, **kw):
        calls.append(1)
        return orig(*a, **kw)

    client.complete = counting
    try:
        c.get(f"/api/books/{bid}/catch-me-up")                              # miss -> synth (1 call)
        total = sum(a["char_len"] for a in atoms)
        c.put(f"/api/books/{bid}/position", json={"cfi": "x", "offset": total})  # ingest ch3 (cheap-tier calls)
        cheap = len(calls)
        r = c.get(f"/api/books/{bid}/catch-me-up", params={"bookmark": 2})  # bm=2 again -> cache HIT
    finally:
        client.complete = orig
    assert len(calls) == cheap, "the bm=2 recap must still be cached after ch3's ingest"
    assert r.json()["as_of_chapter"] == 2


def test_catch_me_up_at_zero_reads_nothing(env):
    c, bid, _, _ = env
    r = c.get(f"/api/books/{bid}/catch-me-up", params={"bookmark": 0}).json()
    assert r["recap"] is None and r["as_of_chapter"] == 0


def test_catch_me_up_refuses_provider_io_when_book_ceiling_is_exhausted(env):
    c, bid, app, _ = env
    app.state.catalog.record_cost(
        bid, phase="test-budget", model="m", output_tokens=app.state.settings.cost_max_output_tokens_per_book
    )
    client, original = app.state.client, app.state.client.complete
    called = []

    def should_not_run(*args, **kwargs):
        called.append(1)
        return original(*args, **kwargs)

    client.complete = should_not_run
    try:
        response = c.get(f"/api/books/{bid}/catch-me-up")
    finally:
        client.complete = original
    assert response.status_code == 429
    assert response.json()["detail"] == "book cost ceiling reached"
    assert called == []


def test_catch_me_up_reports_provider_authentication_failure_truthfully_and_secret_free(env):
    c, bid, app, _ = env
    original = app.state.client.complete

    def unavailable(*args, **kwargs):
        raise ProviderUnavailable("authentication", service="completion")

    app.state.client.complete = unavailable
    try:
        response = c.get(f"/api/books/{bid}/catch-me-up")
    finally:
        app.state.client.complete = original

    assert response.status_code == 503
    assert response.json() == {"detail": {
        "code": "provider_authentication_failed",
        "message": "The AI provider rejected the configured credentials.",
    }}
    assert "spoiler" not in response.text.lower()


def test_rejected_recap_is_never_served_and_error_is_name_free(env):
    """The LIT-8 runtime gate: a synthesized recap naming a FUTURE entity is rejected fail-closed; the
    route retries once, then returns a GENERIC error that carries neither the recap nor any future
    name (the gate's own error channel must not spoil — pass-2 finding baked into the API layer)."""
    c, bid, app, _ = env
    client = app.state.client
    orig = client.complete

    def leaky(system, user, tier="cheap", schema=None):
        if tier == "large":
            return "Corvus the magistrate will rise to power.", {"in": 1, "out": 1}
        return orig(system, user, tier=tier, schema=schema)

    client.complete = leaky
    try:
        r = c.get(f"/api/books/{bid}/catch-me-up")
    finally:
        client.complete = orig
    assert r.status_code == 502
    assert "corvus" not in r.text.lower(), "the error response leaked a future entity name"
    assert "will rise" not in r.text.lower(), "the error response leaked the rejected recap"


def test_catch_me_up_response_carries_no_gate_diagnostics(env):
    """Module E review pass-1 (spoiler lens, HIGH): future_theme_hits / grounded_rate are GATE
    diagnostics computed from the unfiltered audit hatch — a scrubbed-back reader must never receive a
    FUTURE theme label ('theme:Looming Parricide') in the response body. The payload carries reader
    data only."""
    c, bid, app, _ = env
    with app.state.store.book(bid) as mem:
        mem.add_theme("Looming Parricide", "future theme label", revealed_at=3)
    client = app.state.client
    orig = client.complete

    def clean_but_theme_matching(system, user, tier="cheap", schema=None):
        if schema is not None and "references_future" in getattr(schema, "model_fields", {}):
            return {"references_future": False, "unsupported_claims": []}, {"in": 1, "out": 1}
        if tier == "large":                                    # grounded recap; 'looming' matches the theme
            return "Aldric the smith arrived in the valley, looming at the forge.", {"in": 1, "out": 1}
        return orig(system, user, tier=tier, schema=schema)

    client.complete = clean_but_theme_matching
    try:
        r = c.get(f"/api/books/{bid}/catch-me-up", params={"bookmark": 2})
    finally:
        client.complete = orig
    assert r.status_code == 200, r.text
    assert "parricide" not in r.text.lower(), "a FUTURE theme label reached the client"
    body = r.json()
    assert "future_theme_hits" not in body and "grounded_rate" not in body


def _is_judge(schema):
    return schema is not None and "references_future" in getattr(schema, "model_fields", {})


def test_catch_me_up_judge_blocks_a_future_reference(env):
    """LIT-14: a recap that PASSES the deterministic gate but the LLM-judge flags as referencing a
    future development is rejected fail-closed (the subject-binding residual the deterministic gate
    structurally can't see). The error is generic — no recap, no judge details."""
    c, bid, app, _ = env
    client = app.state.client
    orig = client.complete

    def dispatch(system, user, tier="cheap", schema=None):
        if _is_judge(schema):
            return {"references_future": True, "unsupported_claims": ["a future killing"]}, {"in": 1, "out": 1}
        return orig(system, user, tier=tier, schema=schema)      # grounded stub recap (passes deterministic)

    client.complete = dispatch
    try:
        r = c.get(f"/api/books/{bid}/catch-me-up")
    finally:
        client.complete = orig
    assert r.status_code == 502
    assert "future killing" not in r.text.lower() and "references_future" not in r.text


def test_catch_me_up_subject_binding_rejects_before_the_judge(env):
    """LIT-27: grounded event vocabulary rebound to Aldric is a deterministic reject, so neither
    generated attempt may reach the paid semantic judge."""
    c, bid, app, _ = env
    with app.state.store.book(bid) as mem:
        mem.add_event("A mysterious visitor confessed to a murder years ago.", 2, 999)
    client = app.state.client
    orig = client.complete
    judge_calls = []

    def dispatch(system, user, tier="cheap", schema=None):
        if _is_judge(schema):
            judge_calls.append(1)
            return {"references_future": False, "unsupported_claims": []}, {"in": 1, "out": 1}
        if tier == "large" and schema is None:
            return "Aldric was murdered.", {"in": 1, "out": 1}
        return orig(system, user, tier=tier, schema=schema)

    client.complete = dispatch
    try:
        r = c.get(f"/api/books/{bid}/catch-me-up")
    finally:
        client.complete = orig
    assert r.status_code == 502
    assert judge_calls == []
    assert "Aldric" not in r.text and "murder" not in r.text.lower()


def test_catch_me_up_judge_unavailable_fails_closed(env):
    """ADR 0004: a recap with no judge verdict counts as unsafe. A judge outage yields a 502, never an
    unjudged recap."""
    c, bid, app, _ = env
    client = app.state.client
    orig = client.complete

    def dispatch(system, user, tier="cheap", schema=None):
        if _is_judge(schema):
            raise RuntimeError("judge provider down")
        return orig(system, user, tier=tier, schema=schema)

    client.complete = dispatch
    try:
        r = c.get(f"/api/books/{bid}/catch-me-up")
    finally:
        client.complete = orig
    assert r.status_code == 502


def test_catch_me_up_serves_a_judge_cleared_recap(env):
    """The judge reviews the EXACT recap it clears (over the bookmark-bounded facts); its soft
    unsupported_claims signal stays server-side (reader-data-only)."""
    c, bid, app, _ = env
    client = app.state.client
    orig = client.complete
    judged = []

    def dispatch(system, user, tier="cheap", schema=None):
        if _is_judge(schema):
            judged.append(user)
            return {"references_future": False, "unsupported_claims": ["mild paraphrase"]}, {"in": 1, "out": 1}
        return orig(system, user, tier=tier, schema=schema)

    client.complete = dispatch
    try:
        r = c.get(f"/api/books/{bid}/catch-me-up").json()
    finally:
        client.complete = orig
    assert r["recap"]
    assert judged and r["recap"] in judged[0]                    # the judge saw the recap it cleared
    assert "unsupported_claims" not in r and "mild paraphrase" not in str(r)


def test_synthesis_spend_is_recorded_even_when_the_judge_blocks(env):
    """LIT-14 review LOW-1: a judge-blocked recap still PAID for the synthesis (and the judge) call —
    that spend must land in the ledger (it feeds the LIT-21 ceilings), not vanish because the verdict
    rejected. Only a deterministic reject before any paid call records nothing."""
    c, bid, app, _ = env
    client = app.state.client
    orig = client.complete

    def dispatch(system, user, tier="cheap", schema=None):
        if _is_judge(schema):
            return {"references_future": True, "unsupported_claims": []}, {"in": 7, "out": 3}
        return orig(system, user, tier=tier, schema=schema)

    client.complete = dispatch
    try:
        assert c.get(f"/api/books/{bid}/catch-me-up").status_code == 502
    finally:
        client.complete = orig
    phases = [row["phase"] for row in app.state.catalog.get_costs(bid)]
    assert "synthesis" in phases, "the paid synthesis call was not recorded on the judge-block path"
    assert "judge" in phases, "the paid judge call was not recorded on the judge-block path"


def test_concurrent_cold_recap_requests_synthesize_once(env):
    """Module E review pass-1 (concurrency lens, MEDIUM): N concurrent misses on one cache key must
    yield ONE paid synthesis (single-flight), not N."""
    import threading
    import time
    c, bid, app, _ = env
    client = app.state.client
    calls = []
    orig = client.complete

    def slow(system, user, tier="cheap", schema=None):
        if tier == "large" and schema is None:                 # the SYNTH call (single-flight target)
            calls.append(1)
            time.sleep(0.3)                                    # force overlap
        return orig(system, user, tier=tier, schema=schema)

    client.complete = slow
    results = []

    def hit():
        results.append(c.get(f"/api/books/{bid}/catch-me-up").status_code)

    try:
        threads = [threading.Thread(target=hit) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        client.complete = orig
    assert results == [200, 200, 200, 200]
    assert len(calls) == 1, f"expected ONE synthesis for 4 concurrent misses, got {len(calls)}"


def test_rejection_is_negatively_cached_no_convoy(env):
    """Module E pass-2 (MEDIUM): with a persistently-rejecting synth model, N concurrent cold requests
    must pay for ONE flight's attempts (2 calls), not 2N in a serial convoy — and a follow-up within
    the failure TTL pays nothing."""
    import threading
    import time
    c, bid, app, _ = env
    client = app.state.client
    calls = []
    orig = client.complete

    def rejecting(system, user, tier="cheap", schema=None):
        if tier == "large":
            calls.append(1)
            time.sleep(0.15)
            return "Corvus the magistrate will rise.", {"in": 1, "out": 1}   # future name + modal
        return orig(system, user, tier=tier, schema=schema)

    client.complete = rejecting
    statuses = []

    def hit():
        statuses.append(c.get(f"/api/books/{bid}/catch-me-up").status_code)

    try:
        threads = [threading.Thread(target=hit) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        followup = c.get(f"/api/books/{bid}/catch-me-up").status_code
    finally:
        client.complete = orig
    assert statuses == [502, 502, 502, 502] and followup == 502
    assert len(calls) == 2, f"expected ONE flight's 2 attempts total, got {len(calls)} paid calls"


def test_views_of_unknown_book_404(env):
    c, _, _, _ = env
    for path in ("graph", "timeline", "notes", "catch-me-up"):
        assert c.get(f"/api/books/nope/{path}").status_code == 404
    assert c.get("/api/books/nope/search", params={"q": "x"}).status_code == 404


# ---- LIT-29: the evolving flowing recap + the 'right now' one-liner ----------


def _capture_large(client):
    """Record the (system, user) of each large-tier recap synthesis call; return (calls, restore)."""
    calls = []
    orig = client.complete

    def rec(system, user, tier="cheap", schema=None):
        if tier == "large" and schema is None:
            calls.append({"system": system, "user": user})
        return orig(system, user, tier=tier, schema=schema)

    client.complete = rec
    return calls, orig


def test_recap_falls_back_to_cumulative_when_no_prior_is_cached(env):
    """Cold cache / a jump: with no recap(N-1) available, fall back to the cumulative synthesis (still
    flowing + anti-repetition). The prompt is the cumulative one — the whole visible cast — NOT the
    evolve prompt."""
    c, bid, app, _ = env
    client = app.state.client
    calls, orig = _capture_large(client)
    try:
        r = c.get(f"/api/books/{bid}/catch-me-up").json()          # bm=2, nothing warmed
    finally:
        client.complete = orig
    assert r["recap"] and r["as_of_chapter"] == 2
    assert calls, "a cold recap must synthesize"
    prompt = calls[-1]["user"]
    assert "as you last told it" not in prompt                     # NOT the evolve path
    assert "Aldric" in prompt and "Berenice" in prompt             # cumulative cast (both chapters)


def test_recap_evolves_from_the_prior_recap_plus_the_delta(env):
    """The key move: recap(2) is synthesized from recap(1) (the story as last told) + ONLY chapter 2's
    delta. The evolve prompt carries the prior recap; the result carries the NEW material and is not
    identical to the prior (the spec's 'extends rather than restates')."""
    c, bid, app, _ = env
    client = app.state.client
    r1 = c.get(f"/api/books/{bid}/catch-me-up", params={"bookmark": 1}).json()   # warm recap(1)
    assert r1["recap"] and r1["as_of_chapter"] == 1
    calls, orig = _capture_large(client)
    try:
        r2 = c.get(f"/api/books/{bid}/catch-me-up", params={"bookmark": 2}).json()
    finally:
        client.complete = orig
    assert r2["recap"] and r2["as_of_chapter"] == 2
    assert calls, "bm=2 is a cold key -> it synthesizes"
    prompt = calls[-1]["user"]
    assert "as you last told it" in prompt                         # EVOLVE path taken
    assert r1["recap"] in prompt                                   # it extends recap(1)
    assert "Berenice" in prompt                                    # folds in the chapter-2 delta
    # extends rather than restates: the evolved recap carries the NEW material and differs from prior
    assert r2["recap"] != r1["recap"]
    assert "Berenice" in r2["recap"]


def test_now_one_liner_is_generated_gated_and_cached(env):
    """The sidebar 'right now' line is its own (cheap-tier) model call, spoiler-gated, cached with the
    recap payload, and its spend is recorded in the ledger."""
    c, bid, app, _ = env
    r1 = c.get(f"/api/books/{bid}/catch-me-up").json()
    assert r1["now"], "the 'right now' one-liner is generated and cleared the gate"
    r2 = c.get(f"/api/books/{bid}/catch-me-up").json()
    assert r2["cached"] is True and r2["now"] == r1["now"]         # cached with the recap
    phases = [row["phase"] for row in app.state.catalog.get_costs(bid)]
    assert "now" in phases, "the one-liner's own model call is cost-recorded"


def test_now_one_liner_failure_degrades_to_absent_never_blocks_the_recap(env):
    """The one-liner is best-effort: a failed/gate-rejected one-liner yields now=None; the recap is
    still served. Spoiler-safety is fail-closed; the one-liner is fail-SAFE-to-absent."""
    c, bid, app, _ = env
    client = app.state.client
    orig = client.complete

    def dispatch(system, user, tier="cheap", schema=None):
        if tier == "cheap" and schema is None and system.startswith("You write a ONE"):
            raise RuntimeError("one-liner model down")
        return orig(system, user, tier=tier, schema=schema)

    client.complete = dispatch
    try:
        r = c.get(f"/api/books/{bid}/catch-me-up").json()
    finally:
        client.complete = orig
    assert r["recap"], "the recap must still be served when the one-liner fails"
    assert r["now"] is None


def test_cast_field_carries_bookmark_bounded_name_entity_pairs(env):
    """LIT-30: cast is now {name, entity_id}[] so a clicked name resolves to its character card. Still
    bookmark-bounded — a name the client makes clickable can never be a future entity."""
    c, bid, _, _ = env
    cast = c.get(f"/api/books/{bid}/catch-me-up").json()["cast"]
    assert isinstance(cast, list) and cast
    assert all(isinstance(it["entity_id"], int) and it["name"] for it in cast)  # each chip -> an entity
    names = {it["name"] for it in cast}
    assert any("Aldric" in n for n in names) and any("Berenice" in n for n in names)
    assert not any("Corvus" in n for n in names)                 # ch3 hidden
    r99 = c.get(f"/api/books/{bid}/catch-me-up", params={"bookmark": 99}).json()
    assert not any("Corvus" in it["name"] for it in r99["cast"])  # clamp holds


def _char_id(c, bid, name_substr, bookmark=None):
    """Resolve a visible character's entity_id via the (clamped) graph route."""
    params = {"bookmark": bookmark} if bookmark is not None else {}
    g = c.get(f"/api/books/{bid}/graph", params=params).json()
    return next((ch["entity_id"] for ch in g["characters"] if name_substr in ch["canonical_name"]), None)


def test_character_card_returns_identity_and_ties_shape(env):
    """LIT-30: GET /character/{id} returns the bookmark-clamped card — identity + a ties list."""
    c, bid, _, _ = env
    aid = _char_id(c, bid, "Aldric")
    assert aid is not None
    card = c.get(f"/api/books/{bid}/character/{aid}").json()
    assert "Aldric" in card["name"] and card["type"] == "character"
    assert card["as_of_chapter"] == 2 and card["first_seen"] == 1       # Aldric@1
    assert isinstance(card["aliases"], list) and isinstance(card["ties"], list)
    assert "status" in card


def test_character_card_hides_before_reveal_and_clamps(env):
    """Spoiler-safe: a character 404s if requested BEFORE its reveal (indistinguishable from
    nonexistent — a probe cannot confirm a future character exists); ?bookmark=99 clamps to the
    high-water. (The deeper 'a fully-future character is hidden' case is covered on the real store in
    tests/eval, since this synthetic env only ingests through chapter 2.)"""
    c, bid, _, _ = env
    berenice = _char_id(c, bid, "Berenice")                            # revealed at ch2
    assert berenice is not None
    assert c.get(f"/api/books/{bid}/character/{berenice}", params={"bookmark": 1}).status_code == 404
    assert c.get(f"/api/books/{bid}/character/{berenice}", params={"bookmark": 2}).status_code == 200
    r99 = c.get(f"/api/books/{bid}/character/{berenice}", params={"bookmark": 99}).json()
    assert r99["as_of_chapter"] == 2                                   # clamped to high-water, not 99


def test_character_card_unknown_book_or_entity_404(env):
    c, bid, _, _ = env
    assert c.get("/api/books/nope/character/1").status_code == 404
    assert c.get(f"/api/books/{bid}/character/999999").status_code == 404
    assert c.get(f"/api/books/{bid}/character/notanint").status_code == 422   # typed path param


def test_evolved_recap_with_a_planted_future_name_is_still_rejected(env):
    """The rewiring must not weaken the gate: a future name planted in the EVOLVE-path recap is still
    rejected fail-closed with a name-free error (the gate + judge are unchanged — only fed facts and
    prose framing changed)."""
    c, bid, app, _ = env
    client = app.state.client
    c.get(f"/api/books/{bid}/catch-me-up", params={"bookmark": 1})   # warm recap(1) -> evolve at 2
    orig = client.complete

    def leaky(system, user, tier="cheap", schema=None):
        if tier == "large" and schema is None:
            return "Corvus the magistrate rises to power.", {"in": 1, "out": 1}
        return orig(system, user, tier=tier, schema=schema)

    client.complete = leaky
    try:
        r = c.get(f"/api/books/{bid}/catch-me-up", params={"bookmark": 2})
    finally:
        client.complete = orig
    assert r.status_code == 502
    assert "corvus" not in r.text.lower()


# ---- LIT-31: /notes enriched with per-chapter highlights + the visible cast (the codex breakdown) ----


def test_notes_carries_per_chapter_highlights_and_cast(env):
    """LIT-31: each chapter entry gains new_characters (who first appears) + events (that chapter's
    beats), and the payload carries the visible cast for name-chip wrapping — all bookmark-bounded."""
    c, bid, _, _ = env
    notes = c.get(f"/api/books/{bid}/notes").json()               # default = high-water (2)
    assert notes["as_of_chapter"] == 2
    # the visible cast is name->entity_id pairs, bounded (no ch3 Corvus)
    assert notes["cast"] and all(isinstance(m["entity_id"], int) and m["name"] for m in notes["cast"])
    assert not any("Corvus" in m["name"] for m in notes["cast"])
    by_ch = {ch["revealed_at"]: ch for ch in notes["chapters"]}
    assert set(by_ch) == {1, 2}
    # each chapter carries the new-character + events shape
    for ch in notes["chapters"]:
        assert isinstance(ch["new_characters"], list) and isinstance(ch["events"], list)
        assert all(isinstance(nc["entity_id"], int) and nc["name"] for nc in ch["new_characters"])
    # Aldric first appears in ch1, Berenice in ch2 (from the synthetic book)
    assert any("Aldric" in nc["name"] for nc in by_ch[1]["new_characters"])
    assert any("Berenice" in nc["name"] for nc in by_ch[2]["new_characters"])
    assert not any("Berenice" in nc["name"] for nc in by_ch[1]["new_characters"])


def test_notes_events_group_under_their_chapter(env):
    """An event stamped at revealed_at=2 surfaces under chapter 2's events, never chapter 1's."""
    c, bid, app, _ = env
    with app.state.store.book(bid) as mem:
        mem.add_event("Berenice confronts the smith at the forge.", revealed_at=2, order_idx=99)
    notes = c.get(f"/api/books/{bid}/notes").json()
    by_ch = {ch["revealed_at"]: ch for ch in notes["chapters"]}
    assert any("confronts the smith" in e for e in by_ch[2]["events"])
    assert not any("confronts the smith" in e for e in by_ch[1]["events"])


def test_notes_highlights_are_bookmark_bounded_and_clamped(env):
    """Falsifiability: a future-revealed character/event never appears at an earlier bookmark, and
    ?bookmark=99 clamps to the high-water — the client can't widen its frontier through /notes."""
    c, bid, app, _ = env
    with app.state.store.book(bid) as mem:                        # plant ch3 (future) facts
        mem.add_entity("Corvus the magistrate", "character", revealed_at=3)
        mem.add_event("Corvus sentences the smith to exile.", revealed_at=3, order_idx=1)
    # at bookmark=1: only chapter 1, only Aldric-era highlights, no Berenice/Corvus anywhere
    n1 = c.get(f"/api/books/{bid}/notes", params={"bookmark": 1}).json()
    assert n1["as_of_chapter"] == 1 and [ch["revealed_at"] for ch in n1["chapters"]] == [1]
    blob1 = str(n1)
    assert "Corvus" not in blob1 and "Berenice" not in blob1
    # at bookmark=99: clamps to 2, Corvus (ch3) still absent
    n99 = c.get(f"/api/books/{bid}/notes", params={"bookmark": 99}).json()
    assert n99["as_of_chapter"] == 2
    assert "Corvus" not in str(n99)


def test_notes_scrub_back_shows_less(env):
    """The LIT-15 property on the whole codex data: a smaller T yields fewer chapters and a smaller
    cast — never more."""
    c, bid, _, _ = env
    n2 = c.get(f"/api/books/{bid}/notes", params={"bookmark": 2}).json()
    n1 = c.get(f"/api/books/{bid}/notes", params={"bookmark": 1}).json()
    assert len(n1["chapters"]) < len(n2["chapters"])
    assert len(n1["cast"]) <= len(n2["cast"])


def test_notes_future_event_alone_never_surfaces(env):
    """Review pass-1 LOW: the events channel needs a falsifiable guard whose token is NOT also an
    entity name (so the entity-path filter can't mask it) — a future event must be absent from /notes
    AND /timeline at every bookmark.

    Honest scope (pass-2 note): the DAL funnel is what makes this safe, and the /timeline assertion
    below is what independently falsifies it (drop the events `revealed_at<=?` clause and /timeline at
    bm=2 surfaces the xyzzy row). The /notes assertion is defense-in-depth but NOT an independent
    check of the route's revealed_at-grouping: a future event has no visible chapter_summary row to
    attach to (summaries are themselves funnel-bounded), so /notes stays clean even if only the
    route-grouping — but not the funnel — were broken. The funnel is the load-bearing guarantee."""
    c, bid, app, _ = env
    with app.state.store.book(bid) as mem:            # a future beat, no entity involved
        mem.add_event("The xyzzy-covenant is sworn at midnight.", revealed_at=3, order_idx=7)
    for bm in (None, 1, 2, 99):
        params = {} if bm is None else {"bookmark": bm}
        n = c.get(f"/api/books/{bid}/notes", params=params).json()
        assert "xyzzy" not in str(n), f"future event leaked at bookmark={bm}"
    # the independent guard: the events funnel clause itself (not just the chapter-grouping) — a
    # future event is absent from the bounded /timeline even asking at ?bookmark=99 (clamps to 2)
    tl = c.get(f"/api/books/{bid}/timeline", params={"bookmark": 99}).json()
    assert all("xyzzy" not in ev["summary"] for ev in tl["events"])


def test_lit9_keeps_the_existing_novel_recap_cache_identity():
    from app.api.views import _recap_prompt_version

    assert _recap_prompt_version("novel") == "recap-v3"
    assert _recap_prompt_version("reference") == "recap-v4:reference"
    assert _recap_prompt_version("novel", "en") == "recap-v3"
    assert _recap_prompt_version("novel", "ru") == "recap-v3:source-ru"
