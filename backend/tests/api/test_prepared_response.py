"""Selection cannot synthesize facts, borrow citations, skip review, or change shared clients."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.answer_bank import AnswerBank
from app.answer_reuse import (reviewed_response_request, select_reviewed_response,
                             validated_response_selection)
from app.llm.jev import JevUnavailable
from test_reader_preparation import payload, capture
from test_views import env as env


def verdict(**changes):
    return {"selected_response_id": 0, "selected_claim_ids": [0],
        "selected_claim_texts": ["Berenice met Aldric at the forge."],
        "question_fit": "The named participants and place match.", "compatible_question": True,
        "references_future": False, "unsupported_claims": [], "missing_details": [], **changes}


def candidate():
    value = payload()
    value["claims"].append({"text": "Aldric left at noon.", "citation_ids": [2]})
    value["citations"].append({**value["citations"][0], "id": 2, "excerpt": "Aldric left at noon."})
    return {"reviewed_question": "Describe the meeting and departure.", "payload": value}


def test_packet_keeps_each_claims_exact_proof_separate_and_omits_future(env):
    _, _, app, _ = env
    row = candidate()
    future = deepcopy(row)
    future["payload"]["as_of_chapter"] = 3
    future["payload"]["claims"][0]["text"] = "UNREAD_MARKER"
    scene = [{"ordinal": 2, "chapter_key": "two", "text": "An independent scene context."},
             {"ordinal": 3, "chapter_key": "three", "text": "UNREAD_MARKER"}]
    user, retained = reviewed_response_request("Where did Berenice meet Aldric?", [row, future],
        frontier=2, scene=scene, settings=app.state.settings)
    data = json.loads(user)
    assert "UNREAD_MARKER" not in user and retained == [row]
    sources = {s["id"]: s["text"] for s in data["passages"]}
    for claim, original in zip(data["responses"][0]["claims"], row["payload"]["citations"]):
        assert [sources[i] for i in claim["cited_passage_ids"]] == [original["excerpt"]]
        assert not set(claim["cited_passage_ids"]) & set(data["current_scene_passage_ids"])


def test_exact_claim_subset_preserves_citations_and_does_not_mutate_cache():
    row = candidate()
    before = deepcopy(row)
    result = validated_response_selection(verdict(selected_claim_ids=[1],
        selected_claim_texts=["Aldric left at noon."]), [row])
    assert result["payload"]["claims"] == [before["payload"]["claims"][1]]
    assert result["payload"]["citations"] == [before["payload"]["citations"][1]]
    assert result["reviewed_question"] == before["reviewed_question"] and row == before


@pytest.mark.parametrize("change", [
    {"selected_response_id": None}, {"selected_response_id": -1}, {"selected_response_id": 1},
    {"selected_response_id": True}, {"selected_claim_ids": []}, {"selected_claim_ids": [0, 0]},
    {"selected_claim_ids": [-1]}, {"selected_claim_ids": [2]}, {"selected_claim_ids": [True]},
    {"compatible_question": False}, {"references_future": True},
    {"unsupported_claims": ["wrong actor"]}, {"missing_details": ["omitted mission"]},
    {"claims": [{"text": "model attempted to rewrite"}]},
    {"selected_claim_texts": ["Berenice met Aldric at noon."]},
])
def test_invalid_selection_or_any_failed_check_never_returns_a_response(change):
    with pytest.raises(JevUnavailable):
        validated_response_selection(verdict(**change), [candidate()])


def test_ambiguous_kinship_cannot_pass_even_with_a_false_model_approval():
    row = candidate()
    row["payload"]["claims"][0]["text"] = "Aldric was Berenice's maternal cousin."
    row["payload"]["citations"][0]["excerpt"] = "Aldric was a cousin of Berenice's mother."
    with pytest.raises(JevUnavailable, match="relationship chain"):
        validated_response_selection(verdict(selected_claim_texts=[row["payload"]["claims"][0]["text"]]), [row])


def test_missing_conversation_subject_declines_before_a_paid_call(env):
    _, _, app, _ = env
    with pytest.raises(JevUnavailable, match="explicit question subject"):
        reviewed_response_request("Where did she put it?", [candidate()], frontier=2,
                                  scene=[], settings=app.state.settings)


def test_question_name_needs_its_own_primary_identity_bridge_even_after_model_approval(env):
    _, _, app, _ = env
    row = candidate()
    q = "Where did Bea meet Aldric?"
    with pytest.raises(JevUnavailable, match="Question identity"):
        validated_response_selection(verdict(), [row], question=q)
    scene = [{"ordinal": 2, "chapter_key": "two", "href": "two.xhtml", "title": "Two",
              "text": "Berenice, also called Bea, left her home."}]
    before = deepcopy(row)
    user, retained = reviewed_response_request(q, [row], frontier=2, scene=scene, settings=app.state.settings)
    data = json.loads(user)
    selected = validated_response_selection(verdict(), retained, question=q)
    assert row == before
    assert selected["payload"]["claims"][0]["text"] == before["payload"]["claims"][0]["text"]
    assert scene[0]["text"] in [c["excerpt"] for c in selected["payload"]["citations"]]
    assert set(data["current_scene_passage_ids"]) <= set(data["responses"][0]["claims"][0]["cited_passage_ids"])
    # Attaching possible evidence is not an approval, even when all names are present.
    with pytest.raises(JevUnavailable):
        validated_response_selection(verdict(unsupported_claims=["different person"]), retained, question=q)


def test_future_or_title_only_name_cannot_supply_question_identity(env):
    _, _, app, _ = env
    row = candidate()
    q = "Where did Bea meet Aldric?"
    for source in [
        {"ordinal": 3, "chapter_key": "three", "title": "Three", "text": "Berenice is Bea."},
        {"ordinal": 2, "chapter_key": "two", "title": "Bea", "text": "Bea Berenice went away."},
    ]:
        _, retained = reviewed_response_request(q, [row], frontier=2, scene=[source], settings=app.state.settings)
        with pytest.raises(JevUnavailable, match="Question identity"):
            validated_response_selection(verdict(), retained, question=q)


@pytest.mark.parametrize("failure", [False, True])
def test_fresh_independent_review_is_mandatory_and_uses_only_visible_scene(env, monkeypatch, failure):
    _, bid, app, atoms = env
    calls = []
    def complete(*args, **kwargs):
        assert "Corvus" not in kwargs["user"]
        calls.append(kwargs)
        return SimpleNamespace(value=verdict(missing_details=["purpose"] if failure else []))
    monkeypatch.setattr("app.answer_reuse.budgeted_completion", complete)
    def review():
        return select_reviewed_response("Where did Berenice meet Aldric?", [candidate()], frontier=2,
            store=app.state.store, atoms={a["key"]: a for a in atoms}, catalog=app.state.catalog,
            settings=app.state.settings, client=app.state.reader_client, book_id=bid, remember=lambda r: None)
    if failure:
        with pytest.raises(JevUnavailable):
            review()
    else:
        assert review()["payload"]["claims"] == payload()["claims"]
    assert len(calls) == 1


def test_newer_narrower_alias_does_not_displace_the_canonical_complete_answer():
    bank, scope = AnswerBank(), ("b", "i", "s")
    row = candidate()
    original = row["reviewed_question"]
    bank.set((*scope, original), row["payload"])
    narrow = validated_response_selection(verdict(), [row])["payload"]
    bank.set((*scope, "Where was the meeting?"), narrow, reviewed_question=original)
    assert bank.candidates(scope, "When did Aldric leave?")[0]["payload"] == row["payload"]


def test_paraphrase_cache_pressure_preserves_original_episode_after_restart(tmp_path):
    path, scope = tmp_path / "answers.json", ("b", "i", "s")
    bank, row = AnswerBank(max_entries=2, persist_path=path), candidate()
    original = row["reviewed_question"]
    bank.set((*scope, original), row["payload"])
    narrow = validated_response_selection(verdict(), [row])["payload"]
    for index in range(5):
        bank.set((*scope, f"Paraphrase number {index}?"), narrow, reviewed_question=original)
    restarted = AnswerBank(max_entries=2, persist_path=path)
    assert len(restarted.rows) == 2
    assert restarted.get((*scope, original)) == row["payload"]


def test_single_flight_owner_reuses_one_validity_snapshot(env, monkeypatch):
    import app.api.views as views
    api, bid, app, _ = env
    app.state.settings.reader_answer_cache = app.state.settings.reader_reuse_enabled = True
    scope = capture(app, bid)
    app.state.reader_client.answers.set((*scope.answer_key, "Describe the meeting."), payload())
    counts = {"effective": 0, "scope": 0}
    def counted(name, original):
        def call(*args, **kwargs):
            counts[name] += 1
            return original(*args, **kwargs)
        return call
    monkeypatch.setattr(views, "_effective", counted("effective", views._effective))
    monkeypatch.setattr(views, "answer_scope", counted("scope", views.answer_scope))
    monkeypatch.setattr(views, "select_reviewed_response", lambda q, rows, **kw: rows[0])
    value = api.post(f"/api/books/{bid}/ask", json={"question": "Where did Berenice meet Aldric?"})
    assert value.status_code == 200 and value.json()["cached"]
    assert counts == {"effective": 1, "scope": 1}
