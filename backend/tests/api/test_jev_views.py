"""The Jev fast path must preserve actual route gates, ledger entries and independent review."""
import json
from types import SimpleNamespace

import pytest

from test_views import env as env, _supported_citation_audit
from app.llm.jev import JevUnavailable
from tests.test_jev import answer


@pytest.mark.parametrize("mode", ["pass", "uncertain", "outage", "reject_final", "budget"])
@pytest.mark.parametrize("route", ["ask", "chapter-closeout"])
def test_jev_evidence_route_keeps_spoiler_boundary_fallback_and_independent_judge(
    env, monkeypatch, mode, route,
):
    api, bid, app, _ = env
    client, settings = app.state.client, app.state.settings
    client.provider = "openai-compatible"
    client.cheap_model = client.large_model = "gpt-6-luna"
    calls, jev_calls = [], []
    selected = [1] if route == "chapter-closeout" else []
    settings.jev_selection_enabled = False
    jev = SimpleNamespace(model="jev-1.13.0", close=lambda: None)
    client.jev = jev

    def evaluate(state, questions):
        assert not app.state.store._lock_for(bid).locked()
        assert "Corvus" not in json.dumps(state)
        assert all(s["completed_chapter"] <= 2 for claim in state["claims"]
                   for s in claim["cited_sources"])
        jev_calls.append(state)
        if mode == "outage":
            raise JevUnavailable("private-test-key")
        values = {key: answer(q["criteria"], p=0.7 if mode == "uncertain" else 0.99)
                  for key, q in questions.items()}
        return values, {"in": 100, "out": 30}
    jev.evaluate = evaluate

    def complete(system, user, tier="cheap", schema=None):
        fields = getattr(schema, "model_fields", {})
        calls.append(schema.__name__)
        payload = (json.loads(user.split("(untrusted JSON data):\n")[1])
                   if "(untrusted JSON data):\n" in user else None)
        if "source_ids" in fields:
            selected[:] = [next(s["id"] for s in payload["candidates"]
                                if "Berenice met Aldric" in s["text"])]
            return {"source_ids": selected}, {"in": 50, "out": 10}
        if "claims" in fields:
            return {"insufficient_evidence": False, "claims": [{
                "text": "Berenice met Aldric at the forge and they spoke.",
                "citation_ids": selected,
            }]}, {"in": 50, "out": 20}
        if "claim_audits" in fields:
            return _supported_citation_audit(payload, selected), {"in": 50, "out": 30}
        if "references_future" in fields:
            assert "Corvus" not in user
            assert [p["id"] for p in payload["claims"][0]["cited_passages"]] == selected
            return {"references_future": False, "unsupported_claims":
                    ["unsupported"] if mode == "reject_final" else []}, {"in": 50, "out": 10}
        raise AssertionError("unexpected generation")
    monkeypatch.setattr(client, "complete", complete)
    if mode == "budget":
        settings.cost_max_input_tokens_per_call = 100
    body = {"chapter": 2} if route == "chapter-closeout" else {
        "question": "Where did Berenice meet Aldric?", "bookmark": 999,
    }
    response = api.post(f"/api/books/{bid}/{route}", json=body)
    assert "Corvus" not in response.text and "private-test-key" not in response.text
    if mode == "budget":
        assert response.status_code == 429
        assert not jev_calls
        return
    assert response.status_code == (502 if mode == "reject_final" else 200), response.text
    assert "AskVerdict" in calls  # Even a falsely positive Jev cannot bypass the independent judge.
    assert ("AskEvidenceReview" in calls) == (mode in {"uncertain", "outage"})
    assert jev_calls
    rows = [r for r in app.state.catalog.get_costs(bid) if r["phase"].startswith("jev-")]
    assert rows and all(r["model"] == jev.model and r["usd"] > 0 for r in rows)
    assert app.state.catalog.get_cost_reservations(bid) == []
    if mode != "reject_final":
        assert response.json()["as_of_chapter"] == 2
        assert all(s["ordinal"] <= 2 for s in response.json()["citations"])


def test_jev_recap_changes_cache_identity_and_uncertainty_uses_original_review(env, monkeypatch):
    api, bid, app, _ = env
    first = api.get(f"/api/books/{bid}/catch-me-up")
    assert first.status_code == 200
    original = app.state.client.complete
    app.state.settings.jev_recap_enabled = True
    generated, jev_seen = [], []
    def complete(system, user, tier="cheap", schema=None):
        generated.append(schema)
        return original(system, user, tier=tier, schema=schema)
    monkeypatch.setattr(app.state.client, "complete", complete)
    def evaluate(state, questions):
        assert "Corvus" not in json.dumps(state)
        jev_seen.append(state)
        return {key: answer(q["criteria"], p=0.7) for key, q in questions.items()}, {"in": 100, "out": 30}
    app.state.client.jev = SimpleNamespace(model="jev-1.13.0", evaluate=evaluate, close=lambda: None)
    fresh = api.get(f"/api/books/{bid}/catch-me-up", params={"bookmark": 999})
    assert fresh.status_code == 200 and fresh.json()["cached"] is False
    assert jev_seen and generated
    assert fresh.json()["as_of_chapter"] == 2
    calls = len(jev_seen), len(generated)
    assert api.get(f"/api/books/{bid}/catch-me-up").json()["cached"] is True
    assert calls == (len(jev_seen), len(generated))
    reset = api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 0})
    assert reset.status_code == 200
    zero = api.get(f"/api/books/{bid}/catch-me-up", params={"bookmark": 999})
    assert zero.json()["recap"] is None and zero.json()["as_of_chapter"] == 0


@pytest.mark.parametrize("mode", ["pass", "outage", "all_uncertain", "missing", "overwrite",
                                    "negative", "reject_final"])
@pytest.mark.parametrize("route", ["ask", "chapter-closeout"])
def test_partial_evidence_reviews_only_unresolved_claims_but_final_judge_sees_complete_answer(
    env, monkeypatch, mode, route,
):
    api, bid, app, _ = env
    client, settings = app.state.client, app.state.settings
    client.provider = "openai-compatible"
    client.cheap_model = client.large_model = "gpt-6-luna"
    settings.jev_selection_enabled = False
    settings.jev_partial_evidence_enabled = True
    settings.reader_answer_cache = True
    selected = [1] if route == "chapter-closeout" else []
    evidence_calls, final_calls = [], []
    texts = ["Berenice met Aldric at the forge.", "Berenice and Aldric spoke."]
    def evaluate(state, questions):
        assert "Corvus" not in json.dumps(state)
        assert not app.state.store._lock_for(bid).locked()
        assert all(s["completed_chapter"] <= 2 for c in state["claims"] for s in c["cited_sources"])
        if mode == "outage":
            raise JevUnavailable("unavailable")
        return {key: answer(q["criteria"], p=.7 if key.startswith("c1_") or mode == "all_uncertain"
                           else .99) for key, q in questions.items()}, {"in": 100, "out": 40}
    client.jev = SimpleNamespace(model="jev-1.13.0", evaluate=evaluate, close=lambda: None)
    def complete(system, user, tier="cheap", schema=None):
        data = (json.loads(user.split("(untrusted JSON data):\n")[1])
                if "(untrusted JSON data):\n" in user else None)
        assert "Corvus" not in user
        if schema.__name__ == "SourceSelection":
            selected[:] = [next(s["id"] for s in data["candidates"] if "Berenice met Aldric" in s["text"])]
            return {"source_ids": selected}, {"in": 50, "out": 5}
        if schema.__name__ == "AskDraft":
            return {"insufficient_evidence": False,
                    "claims": [{"text": t, "citation_ids": selected[:]} for t in texts]}, {"in": 50, "out": 20}
        if schema.__name__ == "AskEvidenceReview":
            evidence_calls.append(data)
            expected = [1, 2] if mode in {"outage", "all_uncertain"} else [2]
            assert [c["claim_id"] for c in data["claims"]] == expected
            if expected == [2]:
                assert [c["text"] for c in data["answer_context"]] == texts
                assert "approved" not in data  # No Jev verdicts feed the independent reviewer.
            review = _supported_citation_audit(data, selected)
            if mode == "missing":
                review["claim_audits"] = []
            if mode == "overwrite":
                review["claim_audits"][0]["claim_id"] = 1
            if mode == "negative":
                review["claim_audits"][0]["units"][0].update(
                    supported=False, evidence=[], diagnostic="The assertion lacks proof.")
            return review, {"in": 50, "out": 20}
        assert schema.__name__ == "AskVerdict"
        final_calls.append(data)
        assert [c["claim"] for c in data["claims"]] == texts
        assert all([s["id"] for s in c["cited_passages"]] == selected for c in data["claims"])
        return {"references_future": False,
                "unsupported_claims": ["Rejected independently"] if mode == "reject_final" else []}, {"in": 50, "out": 5}
    monkeypatch.setattr(client, "complete", complete)
    body = {"chapter": 2} if route == "chapter-closeout" else {"question": "What did Berenice and Aldric do?", "bookmark": 999}
    response = api.post(f"/api/books/{bid}/{route}", json=body)
    assert response.status_code == (200 if mode in {"pass", "outage", "all_uncertain"} else 502), response.text
    assert evidence_calls
    assert bool(final_calls) == (mode in {"pass", "outage", "all_uncertain", "reject_final"})
    assert "Corvus" not in response.text
    if response.status_code == 200:
        assert [c["text"] for c in response.json()["claims"]] == texts
        assert all(s["ordinal"] <= 2 for s in response.json()["citations"])
    else:
        assert api.get(f"/api/books/{bid}/prepared-questions").json()["questions"] == []
    assert app.state.catalog.get_cost_reservations(bid) == []
