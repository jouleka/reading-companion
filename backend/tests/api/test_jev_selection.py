"""Jev source selection receives only the route's verified completed-source candidate pool."""
import json
from types import SimpleNamespace

from test_views import env as env
from tests.test_jev import answer


def test_jev_selector_cannot_add_future_evidence_and_answer_still_receives_fresh_review(env, monkeypatch):
    api, bid, app, _ = env
    client = app.state.client
    client.provider = "openai-compatible"
    client.cheap_model = client.large_model = "gpt-6-luna"
    selected, calls = [], []
    def evaluate(state, questions):
        assert "Corvus" not in json.dumps(state)
        if "candidates" in state:
            assert all(s["ordinal"] <= 2 for s in state["candidates"])
            selected[:] = [next(s["id"] for s in state["candidates"]
                                if "Berenice met Aldric" in s["text"])]
            return {key: answer(q["criteria"], choice="direct" if int(key) in selected else "irrelevant")
                    for key, q in questions.items()}, {"in": 100, "out": 30}
        return {key: answer(q["criteria"]) for key, q in questions.items()}, {"in": 100, "out": 30}
    client.jev = SimpleNamespace(model="jev-1.13.0", evaluate=evaluate, close=lambda: None)
    def complete(system, user, tier="cheap", schema=None):
        calls.append(schema.__name__)
        if schema.__name__ == "AskDraft":
            return {"insufficient_evidence": False, "claims": [{
                "text": "Berenice met Aldric at the forge and they spoke.", "citation_ids": selected,
            }]}, {"in": 100, "out": 30}
        assert schema.__name__ == "AskVerdict"
        data = json.loads(user.split("(untrusted JSON data):\n")[1])
        assert [s["id"] for s in data["claims"][0]["cited_passages"]] == selected
        return {"references_future": False, "unsupported_claims": []}, {"in": 100, "out": 30}
    monkeypatch.setattr(client, "complete", complete)
    response = api.post(f"/api/books/{bid}/ask", json={"question": "Where did Berenice meet Aldric?",
                                                       "bookmark": 999})
    assert response.status_code == 200, response.text
    assert calls == ["AskDraft", "AskVerdict"]
    assert response.json()["as_of_chapter"] == 2 and "Corvus" not in response.text
    assert sum(c["provider"] == "typesafe" for c in response.json()["cost"]["calls"]) == 2
