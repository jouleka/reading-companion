"""Broader preparation keeps complete answer review, question-premise review and source boundaries."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from openai.lib._pydantic import to_strict_json_schema

from app.answer_bank import AnswerBank
from app.preparation_plan import (
    PreparedQuestions,
    PreparationReview,
    diverse_order,
    preparation_facts,
    reviewed_candidates,
    validated_candidates,
)
from app.reader_prepare import preparation_key, prepare
from test_views import env as env
from test_reader_preparation import capture, payload


FACTS = [
    {
        "id": 1,
        "kind": "event",
        "ordinal": 2,
        "text": "Nell gave Orin a key because he needed to open the gate.",
    }
]


def question(text="Who gave Orin a key?", kind="event", parent=None, basis=None):
    return {
        "question": text,
        "kind": kind,
        "basis_ids": [1] if basis is None else basis,
        "follow_up_to": parent,
    }


def audit(candidates, *, rejected=()):
    return {
        "audits": [
            {
                "question_id": c["id"],
                "safe_to_prepare": c["id"] not in rejected,
                "basis_ids": [1] if c["id"] not in rejected else [],
            }
            for c in candidates
        ]
    }


@pytest.mark.parametrize("schema", [PreparedQuestions, PreparationReview])
def test_new_schemas_are_natively_strict_compatible(schema):
    assert to_strict_json_schema(schema) == schema.model_json_schema()


def test_context_adds_returning_cast_states_and_ties_without_unread_or_expired_fields(env):
    _api, bid, app, _atoms = env
    with app.state.store.book(bid) as mem:
        cast = {r["canonical_name"]: r["entity_id"] for r in mem.view(2).characters()}
        aldric, berenice = cast["Aldric"], cast["Berenice"]
        mem.add_alias(aldric, "Smith", 2)
        mem.add_alias(aldric, "FUTURE ALIAS", 3)
        mem.add_state(aldric, 2, {"note": "working at the forge"})
        mem.add_state(aldric, 3, {"note": "FUTURE STATE"})
        mem.add_edge(aldric, berenice, "social", "teaches smithing", 1, invalid_at=3)
        mem.add_edge(aldric, berenice, "rivalry", "FUTURE TIE", 3)
        mem.add_entity("Corvus", "character", 3)
        facts = preparation_facts(mem, 2)
    text = json.dumps(facts)
    assert "working at the forge" in text and "teaches smithing" in text
    assert "Aldric" in text and "Smith" in text
    assert all(s not in text for s in ("FUTURE", "Corvus", "invalid_at", "entity_id"))
    assert all(1 <= f["ordinal"] <= 2 for f in facts)
    assert [f["id"] for f in facts] == list(range(1, len(facts) + 1))
    identity = next(f for f in facts if f["kind"] == "character" and "Aldric" in f["text"])
    assert identity["ordinal"] == 2  # The alias was not known at the canonical's first appearance.


def test_context_limits_pack_whole_fields_including_cast_among_many_events(env):
    _api, bid, app, _atoms = env
    with app.state.store.book(bid) as mem:
        for i in range(30):
            mem.add_event(f"Aldric lifted tool {i}.", 2, order_idx=i + 10)
        mem.add_event("Aldric " + "源" * 12000, 2, order_idx=100)
        facts = preparation_facts(mem, 2, byte_limit=1500)
        assert any(f["kind"] == "character" for f in facts)
        assert not any("源" in f["text"] for f in facts)
        assert len(json.dumps(facts, ensure_ascii=False).encode()) <= 1500
        assert preparation_facts(mem, 2, byte_limit=1) == []


@pytest.mark.parametrize(
    "fault",
    [
        "missing_basis",
        "foreign_basis",
        "duplicate_basis",
        "duplicate_question",
        "forward_parent",
        "missing_parent",
        "parent_on_primary",
        "followup_chain",
    ],
)
def test_candidate_validation_preserves_original_ids_and_rejects_bad_references(fault):
    rows = [question(), question("Why did Nell give Orin the key?", kind="follow_up", parent=1)]
    if fault == "missing_basis":
        rows[1]["basis_ids"] = []
        with pytest.raises(ValueError):
            validated_candidates({"questions": rows}, FACTS)
        return
    if fault == "foreign_basis":
        rows[1]["basis_ids"] = [99]
    if fault == "duplicate_basis":
        rows[1]["basis_ids"] = [1, 1]
    if fault == "duplicate_question":
        rows[1]["question"] = "WHO gave Orin a key!"
    if fault == "forward_parent":
        rows[1]["follow_up_to"] = 3
    if fault == "missing_parent":
        rows[1]["follow_up_to"] = None
    if fault == "parent_on_primary":
        rows[1]["kind"] = "motive"
    if fault == "followup_chain":
        rows.append(question("What did Orin need the key for?", kind="follow_up", parent=2))
    result = validated_candidates({"questions": rows}, FACTS)
    assert [c["id"] for c in result] == ([1, 2] if fault == "followup_chain" else [1])


@pytest.mark.parametrize(
    "fault", ["missing", "foreign", "reordered", "duplicate", "rewritten", "boolean"]
)
def test_review_requires_exact_original_question_coverage(fault):
    candidates = validated_candidates(
        {"questions": [question(), question("Who is Orin?", "character")]}, FACTS
    )
    value = audit(candidates)
    if fault == "missing":
        value["audits"].pop()
    if fault == "foreign":
        value["audits"][1]["question_id"] = 99
    if fault == "reordered":
        value["audits"].reverse()
    if fault == "duplicate":
        value["audits"][1]["question_id"] = 1
    if fault == "rewritten":
        value["audits"][0]["question"] = "Spoiler?"
    if fault == "boolean":
        value["audits"][0]["question_id"] = True
    with pytest.raises(ValueError):
        reviewed_candidates(value, candidates, FACTS)


@pytest.mark.parametrize("fault", ["false", "empty", "foreign", "duplicate", "parent_rejected"])
def test_only_positive_complete_question_evidence_allows_a_suggestion_candidate(fault):
    candidates = validated_candidates(
        {"questions": [question(), question("Why did Nell give Orin the key?", "follow_up", 1)]},
        FACTS,
    )
    value = audit(candidates, rejected=[1] if fault == "parent_rejected" else [])
    if fault == "false":
        value["audits"][1]["safe_to_prepare"] = False
    if fault == "empty":
        value["audits"][1]["basis_ids"] = []
    if fault == "foreign":
        value["audits"][1]["basis_ids"] = [99]
    if fault == "duplicate":
        value["audits"][1]["basis_ids"] = [1, 1]
    result = reviewed_candidates(value, candidates, FACTS)
    assert [c["id"] for c in result] == ([] if fault == "parent_rejected" else [1])


def test_diversity_covers_all_primary_kinds_two_followups_and_only_one_recap():
    rows = [question(f"Character {i}?", "character") for i in range(4)]
    rows += [
        question("Which tie?", "relationship"),
        question("What happened?", "event"),
        question("What explicit reason?", "motive"),
        question("Recap?", "summary"),
        question("Another recap?", "summary"),
        question("Which object?", "follow_up", 6),
        question("Where did the event occur?", "follow_up", 6),
    ]
    candidates = validated_candidates({"questions": rows}, FACTS)
    before = deepcopy(candidates)
    ordered = diverse_order(candidates, limit=8)
    kinds = [r["kind"] for r in ordered]
    assert len(ordered) == 8 and set(kinds) == {
        "character",
        "relationship",
        "event",
        "motive",
        "summary",
        "follow_up",
    }
    assert kinds.count("summary") == 1 and kinds.count("follow_up") == 2
    for index, row in enumerate(ordered):
        if row["kind"] == "follow_up":
            assert row["follow_up_to"] in {r["id"] for r in ordered[:index]}
    assert candidates == before


def test_only_explicitly_premise_reviewed_questions_become_durable_suggestions(tmp_path):
    path = tmp_path / "answers.json"
    bank, scope = AnswerBank(persist_path=path), ("b", "incarnation", "scope")
    original, injected = "Where was the meeting?", "Why did Corvus kill Berenice?"
    bank.set((*scope, original), payload())
    bank.set((*scope, injected), payload())
    assert bank.suggestions(scope) == []  # Foreground cached answers grant no premise clearance.
    bank.mark_suggestion((*scope, original))
    bank.set((*scope, "Where did they meet?"), payload(), reviewed_question=original)
    bank.mark_suggestion((*scope, "Where did they meet?"))  # No transitive alias approval.
    restarted = AnswerBank(persist_path=path)
    assert restarted.suggestions(scope, frontier=2) == [original]
    assert restarted.suggestions(scope, frontier=1) == []
    restarted.invalidate_book("b")
    assert not AnswerBank(persist_path=path).suggestions(scope)


def test_new_planner_attempt_is_versioned_without_discarding_existing_reviewed_answers(env):
    _api, bid, app, _atoms = env
    scope = capture(app, bid)
    bank = app.state.reader_client.answers
    bank.mark_prepared(scope.answer_key)  # Prior three-question rollout.
    bank.set((*scope.answer_key, "Where was the meeting?"), payload())
    first = preparation_key(scope, app.state.settings)
    assert first != scope.answer_key and not bank.prepared(first)
    app.state.settings.reader_prepare_max_questions = 10
    assert first != preparation_key(scope, app.state.settings)
    assert bank.get((*scope.answer_key, "Where was the meeting?"))


@pytest.mark.parametrize(
    "failure",
    [
        "review_rejects",
        "review_invalid",
        "answer_refuses",
        "answer_fails",
        "reset",
        "correction",
        "delete",
    ],
)
def test_broader_preparation_never_offers_failed_or_stale_questions(env, monkeypatch, failure):
    api, bid, app, _atoms = env
    scope = capture(app, bid)
    app.state.settings.reader_prepare_enabled = app.state.settings.reader_answer_cache = True
    calls = []

    def complete(*args, **kwargs):
        calls.append(kwargs["schema"].__name__)
        if kwargs["schema"].__name__ == "PreparedQuestions":
            return SimpleNamespace(
                value={
                    "questions": [
                        question("Who is Berenice?", "character"),
                        question("Where did Berenice meet Aldric?", "follow_up", 1),
                    ]
                }
            )
        rows = json.loads(kwargs["user"])["questions"]
        value = audit(rows, rejected=[1] if failure == "review_rejects" else [])
        if failure == "review_invalid":
            value["audits"].pop()
        return SimpleNamespace(value=value)

    def answer(book_id, body, catalog, store, settings, client, worker):
        calls.append(body.question)
        if failure == "answer_fails":
            raise RuntimeError("provider unavailable")
        if failure == "answer_refuses":
            return {**payload(), "insufficient_evidence": True, "claims": []}
        if failure == "reset":
            api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 0})
        if failure == "delete":
            api.delete(f"/api/books/{bid}")
        if failure == "correction":
            with store.book(bid) as mem:
                mem.add_entity("Corrected identity", "character", 2)
        client.answers.set((*scope.answer_key, body.question), payload())
        return payload()

    monkeypatch.setattr("app.reader_prepare.budgeted_completion", complete)
    monkeypatch.setattr("app.api.views.ask_the_book", answer)
    try:
        prepare(app, scope)
    except Exception:
        assert failure in {"review_invalid", "reset", "correction", "delete"}
    assert app.state.reader_client.answers.suggestions(scope.answer_key) == []
    # A safe, self-contained follow-up gets its own answer gates after a parent answer failure.
    # Rejected premises and stale reading/source scopes still suppress it entirely.
    assert ("Where did Berenice meet Aldric?" in calls) == (failure in {"answer_fails", "answer_refuses"})


def test_eight_safe_answers_use_normal_route_and_only_reviewed_questions_are_offered(
    env, monkeypatch
):
    api, bid, app, _atoms = env
    settings = app.state.settings
    settings.reader_prepare_enabled = settings.reader_answer_cache = True
    settings.reader_prepare_max_questions = 8
    scope, calls = capture(app, bid), []
    rows = [question(f"Who is character {i}?", "character") for i in (1, 2)]
    rows += [
        question("What is the tie?", "relationship"),
        question("What happened?", "event"),
        question("What reason is stated?", "motive"),
        question("What is the recap?", "summary"),
        question("Where did the event happen?", "follow_up", 4),
        question("When did the event happen?", "follow_up", 4),
    ]

    def complete(*a, **kwargs):
        if kwargs["schema"].__name__ == "PreparedQuestions":
            return SimpleNamespace(value={"questions": rows})
        return SimpleNamespace(value=audit(json.loads(kwargs["user"])["questions"]))

    def answer(book_id, body, catalog, store, settings, client, worker):
        assert client.preparing and body.bookmark == 2
        calls.append(body.question)
        client.answers.set((*scope.answer_key, body.question), payload())
        return payload()

    monkeypatch.setattr("app.reader_prepare.budgeted_completion", complete)
    monkeypatch.setattr("app.api.views.ask_the_book", answer)
    prepare(app, scope)
    prepare(app, scope)
    assert len(calls) == 8
    assert api.get(f"/api/books/{bid}/prepared-questions").json()["questions"] == calls
    assert api.get(f"/api/books/{bid}/prepared-questions?bookmark=1").json()["questions"] == []
    app.state.reader_client.answers.set((*scope.answer_key, "Why did Corvus attack?"), payload())
    assert api.get(f"/api/books/{bid}/prepared-questions").json()["questions"] == calls
