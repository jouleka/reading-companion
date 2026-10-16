"""Identity authority, exact evidence and publication fences; canned replies are not quality evidence."""
from copy import deepcopy
import json

import pytest

from app.cost import CostCeilingExceeded
from app.ingest.extraction.identity import IdentityVerdict, review_identity
from app.ingest.extraction.pipeline import all_entities, ingest_chapter, prepare_chapter
from app.ingest.extraction.resolve import resolve_chapter
from app.ingest.extraction.validation import ExtractionSafetyError
from app.llm.client import LLMClient
from app.llm.jev import JevUnavailable
from app.memory.store import Store
from tests.test_jev import answer


TEXT = "Nell, now called Rook, repaired the boat. Orin watched from the shore."
ENTITY = {"canonical_name": "Rook", "type": "character", "aliases": [],
          "matched_roster": False, "state": None}
ROSTER = [{"entity_id": 7, "canonical_name": "Nell", "type": "character", "aliases": []},
          {"entity_id": 9, "canonical_name": "Orin", "type": "character", "aliases": []}]


def nominate(state, questions):
    return {key: answer(q["criteria"], choice="same" if state["candidates"][i]["canonical_name"] == "Nell"
                       else "different", p=.999) for i, (key, q) in enumerate(questions.items())}


def approve(system, user, *, tier, schema):
    assert schema is IdentityVerdict and tier == "large"
    assert "confidence" not in user and '"choice"' not in user
    return {"same_identity": True, "passage_ids": [1]}, {"in": 10, "out": 5}


def reviewed(text=TEXT, entity=None, roster=None, **kwargs):
    return review_identity(text, deepcopy(ENTITY if entity is None else entity),
        deepcopy(ROSTER if roster is None else roster),
        run=kwargs.pop("run", nominate), complete=kwargs.pop("complete", approve), **kwargs)


@pytest.mark.parametrize("reverse", [False, True])
def test_explicit_link_has_exact_source_entity_and_full_roster_scope(reverse):
    roster = deepcopy(ROSTER[::-1] if reverse else ROSTER)
    link = reviewed(roster=roster)
    assert link.entity_id == 7
    link.assert_matches(TEXT, ENTITY, roster)
    for text, entity, rows in [(TEXT+" changed", ENTITY, roster),
            (TEXT, {**ENTITY, "canonical_name": "Orin"}, roster),
            (TEXT, ENTITY, roster[::-1]),
            (TEXT, ENTITY, [{**r, "aliases": ["changed"]} for r in roster])]:
        with pytest.raises(ExtractionSafetyError, match="does not match"):
            link.assert_matches(text, entity, rows)


@pytest.mark.parametrize("mode", ["uncertain", "different", "low_p", "low_conf", "two", "missing", "foreign", "outage", "malformed"])
def test_jev_cannot_authorize_a_merge_without_one_confident_complete_nomination(mode):
    def run(state, questions):
        if mode == "outage":
            raise JevUnavailable("unavailable")
        values = nominate(state, questions)
        if mode in {"uncertain", "different"}:
            values["c0"] = answer(questions["c0"]["criteria"], choice=mode)
        elif mode == "low_p":
            values["c0"] = answer(questions["c0"]["criteria"], choice="same", p=.989)
        elif mode == "low_conf":
            values["c0"]["confidence"] = .89
        elif mode == "two":
            values["c1"] = answer(questions["c1"]["criteria"], choice="same", p=.999)
        elif mode == "missing":
            values.pop("c0")
        elif mode == "foreign":
            values["foreign"] = values["c0"]
        elif mode == "malformed":
            values["c0"] = None
        return values
    assert reviewed(run=run, complete=lambda *a, **k: pytest.fail("No GPT review without nomination")) is None


@pytest.mark.parametrize("verdict", [
    {"same_identity": False, "passage_ids": []}, {"same_identity": True, "passage_ids": []},
    {"same_identity": True, "passage_ids": [99]}, {"same_identity": True, "passage_ids": [1, 1]},
    {"same_identity": True, "passage_ids": [2]}, {"same_identity": True, "passage_ids": [True]},
    {"same_identity": "true", "passage_ids": [1]}, {"same_identity": True, "passage_ids": [1], "extra": 1},
])
def test_false_jev_positive_still_requires_valid_independent_evidence(verdict):
    assert reviewed(complete=lambda *a, **k: (verdict, {})) is None


@pytest.mark.parametrize("phase", ["jev", "gpt"])
def test_budget_failure_is_not_hidden_as_an_optional_miss(phase):
    def exhausted(*a, **k):
        raise CostCeilingExceeded("budget")
    with pytest.raises(CostCeilingExceeded):
        reviewed(**{"run" if phase == "jev" else "complete": exhausted})


def test_provider_outage_cannot_grant_identity_authority():
    def unavailable(*a, **k):
        raise RuntimeError("private-provider-detail")
    assert reviewed(complete=unavailable) is None


@pytest.mark.parametrize("text,entity,roster", [
    ("Rook waited.", ENTITY, ROSTER),  # No observed earlier name.
    ("Nell waited.", ENTITY, ROSTER),  # No observed new name.
    (TEXT, {**ENTITY, "type": "place"}, ROSTER),
    ("The elder greeted Nell.", {**ENTITY, "canonical_name": "the elder"}, ROSTER),
    (TEXT, ENTITY, [{**ROSTER[0], "entity_id": ("PENDING", 0)}]),
    (TEXT, ENTITY, [ROSTER[0], {**ROSTER[0], "entity_id": 8}]),  # Identical names.
    ("Rook called Robin.", ENTITY, [
        {**ROSTER[0], "aliases": ["Robin"]}, {**ROSTER[1], "aliases": ["Robin"]}]),
])
def test_missing_or_shared_anchors_never_call_a_provider(text, entity, roster):
    def unexpected(*a, **k):
        pytest.fail("Ineligible identity must be resolved without IO")
    assert reviewed(text, entity, roster, run=unexpected, complete=unexpected) is None


def test_candidate_and_utf8_limits_preserve_complete_source_without_paid_io():
    def unexpected(*a, **k):
        pytest.fail("Overlarge review must be skipped")
    assert reviewed(TEXT+" 源"*12000, run=unexpected) is None
    assert reviewed(input_limit=100, run=unexpected) is None
    roster = [{**ROSTER[0], "entity_id": i+1, "canonical_name": f"Person{i}"} for i in range(5)]
    assert reviewed("Rook greeted "+" ".join(r["canonical_name"] for r in roster),
                    roster=roster, run=unexpected) is None


def test_source_is_indexed_once_per_nomination_and_oversized_text_skips_before_candidate_work(monkeypatch):
    from app.ingest.extraction import identity as module
    original, calls = module.word_tokens, []
    def counted(value):
        calls.append(value)
        return original(value)
    monkeypatch.setattr(module, "word_tokens", counted)
    assert reviewed().entity_id == 7
    assert calls.count(TEXT) == 1
    calls.clear()
    assert reviewed(TEXT*1000) is None
    assert not calls


def test_provider_cannot_modify_evidence_or_receive_states_ids_and_embeddings():
    roster = [{**r, "state": "FUTURE SECRET", "embed": [1, 2]} for r in ROSTER]
    def run(state, questions):
        assert "FUTURE SECRET" not in json.dumps(state) and "entity_id" not in json.dumps(state)
        assert "embed" not in json.dumps(state)
        assert "state.candidates[0]" in questions["c0"]["instructions"]
        values = nominate(state, questions)
        state["source_passages"][0]["text"] = "INJECTED SOURCE"
        return values
    def complete(system, user, **kwargs):
        assert "INJECTED SOURCE" not in user and TEXT.split(". ")[0] in user
        return approve(system, user, **kwargs)
    assert reviewed(roster=roster, run=run, complete=complete).entity_id == 7


def test_guarded_resolver_disables_embedding_authority_but_retains_free_exact_and_pending_links():
    def unexpected(*a, **k):
        pytest.fail("Exact names and guarded mode must not embed")
    assert resolve_chapter([ENTITY], ROSTER, unexpected, identity_review=lambda *a: None)[0][1]["action"] == "create"
    same = {**ENTITY, "canonical_name": "Nell"}
    decisions = resolve_chapter([same], ROSTER, unexpected, identity_review=unexpected)
    assert decisions[0][1]["method"] == "exact"
    decisions = resolve_chapter([ENTITY, ENTITY], [], unexpected, identity_review=lambda *a: None)
    assert decisions[1][1]["entity_id"] == ("PENDING", 0)


def test_reviewed_name_is_reused_in_same_chapter_without_mutating_prior_roster():
    roster, calls = deepcopy(ROSTER), []
    def checked(entity, working_roster):
        calls.append(entity)
        return reviewed(TEXT, entity, working_roster).entity_id
    decisions = resolve_chapter([ENTITY, ENTITY], roster, identity_review=checked)
    assert len(calls) == 1 and roster == ROSTER
    assert [d["method"] for _e, d in decisions] == ["reviewed-alias", "alias"]


def _extraction(name, state=None):
    return {"chapter_summary": "", "entities": [{**ENTITY, "canonical_name": name, "state": state}],
            "relationships": [], "events": [], "themes": []}


def test_reviewed_alias_is_chronological_and_commit_is_network_free(tmp_path):
    store, client = Store(data_dir=str(tmp_path)), LLMClient(provider="stub", allow_stub=True, env={})
    try:
        first = {"key": "one", "ordinal": 1, "text": "Nell arrived."}
        prepared = prepare_chapter(first, _extraction("Nell"), client, roster=[])
        with store.book("b", meta={"title": "B"}) as mem:
            ingest_chapter(mem, first, prepared)
            roster = all_entities(mem.view(1))
        second = {"key": "two", "ordinal": 2, "text": TEXT}
        prepared = prepare_chapter(second, _extraction("Rook", "repairing the boat"), client,
            roster=roster, identity_review=lambda t, e, r: reviewed(t, e, r))
        assert prepared["decisions"][0][1]["method"] == "reviewed-alias"
        client.complete = lambda *a, **k: pytest.fail("Commit cannot call a provider")
        with store.book("b") as mem:
            result = ingest_chapter(mem, second, prepared)
            assert result["entities"]["merge"] == 1
            assert len(mem.view(2).entities_of_type("character")) == 1
            assert mem.view(1).aliases_of(roster[0]["entity_id"]) == []
            assert [a["surface_form"] for a in mem.view(2).aliases_of(roster[0]["entity_id"])] == ["Rook"]
            assert mem.chapter_completion("two", 2, prepared["content_hash"]) is not None
    finally:
        store.close()


@pytest.mark.parametrize("tamper", ["decisions", "missing", "forged_callback", "mutating_callback"])
def test_mutated_resolution_or_unbound_identity_cannot_publish_memory(tmp_path, tamper):
    store, client = Store(data_dir=str(tmp_path)), LLMClient(provider="stub", allow_stub=True, env={})
    chapter = {"key": "one", "ordinal": 1, "text": TEXT}
    def callback(text, entity, roster):
        if tamper == "forged_callback":
            return 7
        entity["state"] = "INJECTED"
        return reviewed(text, entity, roster)
    try:
        if tamper.endswith("callback"):
            with pytest.raises(ExtractionSafetyError):
                prepare_chapter(chapter, _extraction("Rook"), client, roster=ROSTER,
                                identity_review=callback)
            return
        prepared = prepare_chapter(chapter, _extraction("Rook"), client, roster=[])
        if tamper == "decisions":
            prepared["decisions"][0][1]["entity_id"] = 7
        else:
            prepared.pop("resolution_clearance")
        with store.book("b", meta={"title": "B"}) as mem:
            with pytest.raises(ExtractionSafetyError):
                ingest_chapter(mem, chapter, prepared)
            assert mem.view(1).chapters() == []
    finally:
        store.close()
