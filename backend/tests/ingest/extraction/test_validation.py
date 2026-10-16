"""Chapter-grounding regressions. Canned semantic verdicts test enforcement, not model accuracy."""
from copy import deepcopy
import json

import pytest
from openai.lib._pydantic import to_strict_json_schema
from pydantic import ValidationError

from app.ingest.extraction.pipeline import content_hash_of, ingest_chapter, prepare_chapter
from app.ingest.extraction.prompts import extract_system_for, extract_user_prompt
from app.ingest.extraction.schema import Extraction
from app.ingest.extraction.validation import (
    AssertionEvidence,
    ExtractionGroundingRejected,
    ExtractionSafetyError,
    ExtractionVerdict,
    FieldEvidence,
    assert_observed_entity_names,
    merge_reviewed_extractions,
    review_extraction,
    source_passages,
)
from app.llm.client import LLMClient
from app.memory.store import Store
from tests.ingest._grounding import canned_grounding_verdict, review_payload, reviewed_source


TEXT = ("Aldric and Berenice were colleagues. Aldric handed the copper lantern to Berenice. "
        "He stayed at the forge.")
EXTRACTION = {
    "chapter_summary": "Berenice received a copper lantern from Aldric, who remained at the forge.",
    "entities": [
        {"canonical_name": "Aldric", "type": "character", "aliases": [],
         "matched_roster": False, "state": "remains at the forge"},
        {"canonical_name": "Berenice", "type": "character", "aliases": [],
         "matched_roster": False, "state": "has the copper lantern"},
    ],
    "relationships": [{"src": "Aldric", "dst": "Berenice", "rel_type": "social",
                       "label": "colleague of"}],
    "events": [{"summary": "Berenice received the lantern from Aldric.",
                "participants": ["Aldric", "Berenice"]}],
    "themes": [{"name": "giving", "description": "Aldric gives Berenice a lantern."}],
}


def _cleared(_system, user, **_kwargs):
    return canned_grounding_verdict(user), {"in": 10, "out": 2}


def test_absent_new_name_is_rejected_before_any_review_or_memory_write(tmp_path):
    extraction = deepcopy(EXTRACTION)
    calls = []
    with pytest.raises(ExtractionGroundingRejected, match="unobserved") as rejected:
        review_extraction("Aldric walked alone by the river.", extraction, [],
                          complete=lambda *_a, **_k: calls.append(True))
    assert calls == []
    assert rejected.value.repair_feedback
    assert "Berenice" not in str(rejected.value) + repr(rejected.value)
    assert "Berenice" in rejected.value.repair_feedback[0]
    assert "/entities/1" in rejected.value.repair_feedback[0]
    store = Store(data_dir=str(tmp_path))
    try:
        client = LLMClient(provider="stub", allow_stub=True, env={})
        chapter = {"key": "one", "ordinal": 1, "text": "Aldric walked alone by the river."}
        with pytest.raises(ExtractionSafetyError, match="unobserved"):
            prepare_chapter(chapter, extraction, client, roster=[])
        with store.book("b", meta={"title": "B"}) as mem:
            assert mem.view(1).characters() == []
            assert mem.view(1).timeline() == []
            assert mem.chapter_completion("one", 1, content_hash_of(chapter["text"])) is None
    finally:
        store.close()


def test_name_repair_identifies_all_missing_identities_without_public_disclosure():
    with pytest.raises(ExtractionGroundingRejected) as rejected:
        assert_observed_entity_names("Nobody was named in this passage.", EXTRACTION, [])
    error = rejected.value
    assert "Aldric" not in str(error) and "Berenice" not in repr(error)
    assert all(name in error.repair_feedback[0] for name in ("Aldric", "Berenice"))
    assert all(path in error.repair_feedback[0] for path in ("/entities/0", "/entities/1"))
    assert sum(map(len, error.repair_feedback)) <= 1024


def test_observed_object_description_still_needs_full_independent_identity_review():
    text = "Alice took a jar from a shelf. It was labelled ORANGE MARMALADE."
    value = {"chapter_summary": "Alice took a jar.", "entities": [{
        "canonical_name": "jar labelled ORANGE MARMALADE", "type": "object", "aliases": [],
        "matched_roster": False, "state": None}], "relationships": [], "events": [], "themes": []}
    assert_observed_entity_names(text, value, [])
    seen = []
    def reject(system, user, **kwargs):
        seen.append(review_payload(user))
        return canned_grounding_verdict(user, unsupported_claims=["identity not established"]), {}
    with pytest.raises(ExtractionGroundingRejected):
        review_extraction(text, value, [], complete=reject)
    assert len(seen) == 1 and seen[0]["extraction"] == value
    for changed in ("character", "place", "faction"):
        value["entities"][0]["type"] = changed
        with pytest.raises(ExtractionGroundingRejected, match="unobserved"):
            assert_observed_entity_names(text, value, [])


@pytest.mark.parametrize("text", ["A jar was on the shelf. The label was unreadable.",
    "ORANGE MARMALADE was in a basket. Later she found a jar labelled JAM.",
    "A jar sat on a shelf. " + "A bird sang. " * 20 + "A box was labelled ORANGE MARMALADE."])
def test_object_description_cannot_import_missing_reversed_or_distant_name_fragments(text):
    value = {"entities": [{"canonical_name": "jar labelled ORANGE MARMALADE",
                          "type": "object", "aliases": []}]}
    with pytest.raises(ExtractionGroundingRejected, match="unobserved"):
        assert_observed_entity_names(text, value, [])


@pytest.mark.parametrize("mutation", ["roles", "negation", "alias"])
def test_semantic_rejection_of_roles_negation_and_false_aliases_fails_the_whole_extraction(mutation):
    extraction = deepcopy(EXTRACTION)
    if mutation == "roles":
        extraction["events"][0]["summary"] = "Aldric received the lantern from Berenice."
    elif mutation == "negation":
        extraction["chapter_summary"] = "Aldric did not hand a lantern to Berenice."
    else:
        extraction["entities"][0]["aliases"] = ["Berenice"]
    # Every name is present, so the semantic reviewer must receive the complete disputed assertion.
    assert_observed_entity_names(TEXT, extraction, [])
    observed = []

    def fixture_reviewer(_system, user, **_kwargs):
        data = json.loads(user.split("(untrusted JSON data):\n", 1)[1])
        observed.append(data)
        assert data["extraction"] == extraction
        assert reviewed_source(user) == TEXT
        return canned_grounding_verdict(user, unsupported_claims=[mutation]), {"in": 10}

    with pytest.raises(ExtractionSafetyError, match="could not be grounded"):
        review_extraction(TEXT, extraction, [], complete=fixture_reviewer)
    assert len(observed) == 1


def test_concealment_theme_is_reviewed_as_an_assertion_and_rejection_withholds_clearance():
    """Live counterexample: a reviewer approved 'hidden' although the source only said 'placed'.

    This pins the evidence/policy and enforcement, not a fake claim that a mock measures live NLI.
    """
    text = "Mara placed the brass key inside a red tin beneath the kitchen sink. The door stayed shut."
    value = {
        "chapter_summary": "Mara placed a brass key in a red tin.",
        "entities": [{"canonical_name": "Mara", "type": "character", "aliases": [],
                      "matched_roster": False, "state": None}],
        "relationships": [], "events": [],
        "themes": [{"name": "Secrecy and concealment",
                    "description": "The brass key is hidden inside a tin beneath the kitchen sink."}],
    }
    seen = []

    def reviewer(system, user, **_kwargs):
        seen.append(json.loads(user.split("(untrusted JSON data):\n", 1)[1]))
        assert "Inspect theme names and descriptions as assertions" in system
        assert "does not establish that it was hidden" in system
        assert "harmless atmosphere" in system
        return canned_grounding_verdict(user, unsupported_claims=[
            "Placement and a shut door do not establish concealment or secret intent.",
        ]), {"in": 10, "out": 5}

    with pytest.raises(ExtractionSafetyError, match="could not be grounded"):
        review_extraction(text, value, [], complete=reviewer)
    assert seen[0]["extraction"]["themes"] == value["themes"]
    assert "".join(part["text"] for part in seen[0]["source_passages"]) == text


def test_explicit_time_scoped_states_and_direct_consequences_are_valid_review_evidence():
    text = "Mara placed the key inside the tin. The door stayed shut that evening."
    value = {
        "chapter_summary": "Mara placed the key in the tin, and the door stayed shut that evening.",
        "entities": [
            {"canonical_name": "tin", "type": "object", "aliases": [],
             "matched_roster": False, "state": "Contains the key at that point."},
            {"canonical_name": "door", "type": "object", "aliases": [],
             "matched_roster": False, "state": "Stayed shut that evening."},
        ],
        "relationships": [], "events": [],
        "themes": [{"name": "keys and containers", "description": "A key is placed in a tin."}],
    }

    def reviewer(system, user, **_kwargs):
        payload = json.loads(user.split("(untrusted JSON data):\n", 1)[1])
        assert reviewed_source(user) == text
        assert payload["extraction"] == value
        assert "not claims of timeless persistence" in system
        assert "do not require evidence of persistence beyond the stated time" in system
        assert "a reasonable taxonomy or field choice alone are not unsupported facts" in system
        return _cleared(system, user)

    clearance = review_extraction(text, value, [], complete=reviewer)
    clearance.assert_matches(text, value)


def test_supported_paraphrase_pronoun_states_and_whole_extraction_reach_the_reviewer():
    seen = []

    def reviewer(system, user, **kwargs):
        data = json.loads(user.split("(untrusted JSON data):\n", 1)[1])
        seen.append(data)
        assert "pronouns" in system and "negation" in system
        assert kwargs["tier"] == "large"
        assert data["extraction"] == EXTRACTION
        return _cleared(system, user)

    clearance = review_extraction(TEXT, EXTRACTION, [], complete=reviewer)
    clearance.assert_matches(TEXT, EXTRACTION)
    assert len(seen) == 1 and seen[0]["prior_visible_identities"] == []


def test_native_observed_alias_and_prior_roster_coreference_are_allowed():
    extraction = deepcopy(EXTRACTION)
    extraction["entities"] = [{
        "canonical_name": "Alexey Karamazov", "type": "character", "aliases": ["Алёша"],
        "matched_roster": False, "state": "at the monastery",
    }]
    extraction["relationships"] = extraction["events"] = extraction["themes"] = []
    extraction["chapter_summary"] = "Alexey is at the monastery."
    native = "Алексей Карамазов, по прозвищу Алёша, был в монастыре."
    review_extraction(native, extraction, [], complete=_cleared).assert_matches(native, extraction)

    roster = [{"entity_id": 7, "canonical_name": "Alexey Karamazov", "type": "character",
               "aliases": ["Alyosha", "Алёша"]}]
    # The source uses an existing alias and an explicit pronoun antecedent; the roster supports the
    # canonical/native alias association without importing any new action or location.
    review_extraction("Alyosha entered the monastery. He remained there.", extraction, roster,
                      complete=_cleared)


def test_observed_nickname_does_not_establish_an_unprovided_formal_identity():
    text = "Alyosha arrived at the monastery."
    extraction = {
        "chapter_summary": "Alyosha arrived at the monastery.",
        "entities": [{"canonical_name": "Alexey Fyodorovitch Karamazov", "type": "character",
                      "aliases": ["Alyosha"], "matched_roster": False, "state": None}],
        "relationships": [], "events": [], "themes": [],
    }
    # The alias grounds an observed surface, not its proposed full-name equivalence. That requires
    # semantic/source evidence; a famous-book association alone must never count as that evidence.
    assert_observed_entity_names(text, extraction, [])

    def reviewer(_system, user, **_kwargs):
        data = json.loads(user.split("(untrusted JSON data):\n", 1)[1])
        assert reviewed_source(user) == text
        assert data["prior_visible_identities"] == []
        assert data["extraction"]["entities"][0]["canonical_name"] not in text
        return canned_grounding_verdict(user, unsupported_claims=[
            "The source does not establish this formal name for Alyosha.",
        ]), {"in": 10}

    with pytest.raises(ExtractionSafetyError, match="could not be grounded"):
        review_extraction(text, extraction, [], complete=reviewer)


@pytest.mark.parametrize("verdict", [
    {"references_future": False, "unsupported_claims": []},  # legacy shape is never a pass
    {"references_future": False},
    {"references_future": False, "unsupported_claims": "none"},
    {"references_future": "false", "unsupported_claims": []},
])
def test_missing_or_malformed_review_is_not_a_clearance(verdict):
    with pytest.raises(ExtractionSafetyError, match="unavailable or invalid"):
        review_extraction(TEXT, EXTRACTION, [], complete=lambda *_a, **_k: (verdict, {}))


def test_extraction_verdict_requires_provider_visible_complete_evidence_schema():
    schema = ExtractionVerdict.model_json_schema()
    assert schema == to_strict_json_schema(ExtractionVerdict)
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(schema["required"]) == {
        "references_future", "unsupported_claims", "field_audits",
    }
    description = schema["properties"]["unsupported_claims"]["description"]
    assert "ONLY actual propositions asserted by the candidate" in description
    assert "supported-but cautions" in description
    assert "hypothetical stronger meanings" in description
    assert "invented reading" in description
    assert "ENTIRE original text" in schema["properties"]["field_audits"]["description"]
    for model in (AssertionEvidence, FieldEvidence):
        assert model.model_json_schema() == to_strict_json_schema(model)
        assert model.model_json_schema()["additionalProperties"] is False
    assert "original full field context" in (
        AssertionEvidence.model_json_schema()["properties"]["segment_id"]["description"].lower()
    )
    assert ExtractionVerdict.model_validate({
        "references_future": False, "unsupported_claims": [], "field_audits": [],
    }, strict=True).unsupported_claims == []
    with pytest.raises(ValidationError):
        ExtractionVerdict.model_validate({"references_future": False})
    with pytest.raises(ValidationError):
        ExtractionVerdict.model_validate({
            "references_future": False, "unsupported_claims": [], "field_audits": [],
            "editorial_notes": [],
        })


def test_grounding_repair_diagnostics_are_private_immutable_and_bounded():
    diagnostics = [f"private-claim-{index}: " + "x" * 400 for index in range(9)]
    error = ExtractionGroundingRejected("generic grounding rejection", feedback=diagnostics)
    assert isinstance(error.repair_feedback, tuple)
    assert 1 <= len(error.repair_feedback) <= 4
    assert sum(map(len, error.repair_feedback)) == 1024
    assert error.args == ("generic grounding rejection",)
    assert str(error) == "generic grounding rejection"
    assert repr(error) == "ExtractionGroundingRejected('generic grounding rejection')"
    assert "private-claim" not in str(error) + repr(error)
    four = ExtractionGroundingRejected("generic", feedback=[str(index) for index in range(8)])
    assert four.repair_feedback == ("0", "1", "2", "3")


def test_rejecting_verdict_carries_specific_feedback_without_public_claim_leak():
    secret = "A private invented future event that must not appear in a reader-facing error."
    schemas = []

    def reviewer(_system, user, *, tier, schema):
        schemas.append(schema)
        assert tier == "large"
        return canned_grounding_verdict(user, references_future=True,
                                       unsupported_claims=[secret]), {"in": 10}

    with pytest.raises(ExtractionGroundingRejected) as rejected:
        review_extraction(TEXT, EXTRACTION, [], complete=reviewer)
    error = rejected.value
    assert schemas == [ExtractionVerdict]
    assert secret in error.repair_feedback
    assert "plot development not established" in error.repair_feedback[0]
    assert secret not in str(error) + repr(error)
    assert str(error) == "extraction could not be grounded in its chapter"


def test_provider_review_failure_is_closed_and_does_not_expose_provider_text():
    def broken(*_args, **_kwargs):
        raise RuntimeError("private provider details and an invented future event")

    with pytest.raises(ExtractionSafetyError) as error:
        review_extraction(TEXT, EXTRACTION, [], complete=broken)
    assert str(error.value) == "extraction review was unavailable or invalid"


def _scene_extraction(summary):
    return {
        "chapter_summary": summary,
        "entities": [
            {"canonical_name": "Mara", "type": "character", "aliases": ["May"],
             "matched_roster": False, "state": None},
            {"canonical_name": "Jonas", "type": "character", "aliases": [],
             "matched_roster": False, "state": None},
        ],
        "relationships": [], "events": [], "themes": [],
    }


SCENE_SOURCE = ("Mara, called May by Jonas, arrived at the lighthouse. "
                "Jonas lent Mara a brass key.")
SCENE_JOIN = ("Mara arrived at the lighthouse, where Jonas called her May "
              "and lent her a brass key.")


def _field_audit(verdict, payload, path):
    field = next(field for field in payload["fields"] if field["path"] == path)
    audit = next(audit for audit in verdict["field_audits"] if audit["field_id"] == field["id"])
    return audit


def test_live_unsupported_location_join_requires_its_own_evidence_and_blocks_clearance():
    """Actual accepted live counterexample; canned rejection tests the new per-field gate, not NLI."""
    calls = []

    def reviewer(system, user, *, tier, schema):
        payload = review_payload(user)
        calls.append(payload)
        assert tier == "large" and schema is ExtractionVerdict
        assert reviewed_source(user) == SCENE_SOURCE
        assert payload["extraction"]["chapter_summary"] == SCENE_JOIN
        assert "adjacent independent sentences alone do not establish where" in system
        verdict = canned_grounding_verdict(user)
        audit = _field_audit(verdict, payload, "/chapter_summary")
        audit["assertions"] = [
            {"segment_id": 0, "source_ids": [1], "support": "supported"},
            {"segment_id": 1,
             "source_ids": [], "support": "unsupported"},
        ]
        # A top-level clear verdict cannot override a specific unsupported assertion.
        assert verdict["unsupported_claims"] == []
        return verdict, {"in": 20, "out": 10}

    with pytest.raises(ExtractionGroundingRejected) as rejected:
        review_extraction(SCENE_SOURCE, _scene_extraction(SCENE_JOIN), [], complete=reviewer)
    assert len(calls) == 1
    assert rejected.value.repair_feedback == (
        "/chapter_summary: where Jonas called her May and lent her a brass key.",
    )
    assert "Jonas" not in str(rejected.value) + repr(rejected.value)


@pytest.mark.parametrize("source", [
    "Mara arrived at the lighthouse. At the lighthouse, Jonas called her May and lent her a brass key.",
    "Mara arrived at the lighthouse. Jonas called her May and lent her a brass key there.",
])
def test_explicit_location_and_clear_there_coreference_support_the_same_join(source):
    def reviewer(_system, user, **_kwargs):
        payload = review_payload(user)
        verdict = canned_grounding_verdict(user)
        audit = _field_audit(verdict, payload, "/chapter_summary")
        audit["assertions"] = [
            {"segment_id": 0, "source_ids": [1], "support": "supported"},
            {"segment_id": 1,
             "source_ids": [1, 2], "support": "supported"},
        ]
        assert reviewed_source(user) == source
        return verdict, {"in": 20, "out": 10}

    value = _scene_extraction(SCENE_JOIN)
    review_extraction(source, value, [], complete=reviewer).assert_matches(source, value)


@pytest.mark.parametrize("summary", [
    "Iona opened the gate because Pavel rang the bell.",
    "During Pavel's bell-ringing, Iona opened the gate.",
    "The frightened Iona opened the gate.",
])
def test_cause_time_and_descriptive_attachments_are_rejectable_assertions_not_word_overlap(summary):
    source = "Iona opened the gate. Pavel rang the bell."
    value = {"chapter_summary": summary, "entities": [], "relationships": [],
             "events": [], "themes": []}

    def reviewer(system, user, **_kwargs):
        assert "cause, purpose, manner, or attributive description" in system
        verdict = canned_grounding_verdict(user)
        verdict["field_audits"][0]["assertions"][0].update({
            "source_ids": [1, 2], "support": "unsupported",
        })
        return verdict, {"in": 10}

    with pytest.raises(ExtractionGroundingRejected) as rejected:
        review_extraction(source, value, [], complete=reviewer)
    unsupported_fragment = rejected.value.repair_feedback[0].split(": ", 1)[1]
    assert unsupported_fragment in summary


@pytest.mark.parametrize(("source", "summary"), [
    ("Pavel rang the bell, causing Iona to open the gate.",
     "Iona opened the gate because Pavel rang the bell."),
    ("Iona opened the gate while Pavel rang the bell.",
     "During Pavel's bell-ringing, Iona opened the gate."),
    ("Frightened, Iona opened the gate.", "The frightened Iona opened the gate."),
])
def test_explicit_causal_temporal_and_descriptive_support_can_clear(summary, source):
    value = {"chapter_summary": summary, "entities": [], "relationships": [],
             "events": [], "themes": []}
    review_extraction(source, value, [], complete=_cleared).assert_matches(source, value)


@pytest.mark.parametrize("mutation", [
    "missing_field", "duplicate_field", "missing_join", "changed_join", "reordered_fragments",
    "duplicate_fragment", "unknown_segment", "unknown_source", "roster_for_state", "no_evidence",
    "empty_assertions", "legacy_text",
])
def test_incomplete_or_fabricated_evidence_is_invalid_not_a_repairable_grounding_rejection(mutation):
    value = _scene_extraction(SCENE_JOIN)
    value["entities"][0]["state"] = "At the lighthouse."
    roster = [{"canonical_name": "Mara", "type": "character", "aliases": ["May"]}]

    def reviewer(_system, user, **_kwargs):
        payload = review_payload(user)
        verdict = canned_grounding_verdict(user)
        audit = _field_audit(verdict, payload, "/chapter_summary")
        assertion = audit["assertions"][0]
        if mutation == "missing_field":
            verdict["field_audits"].pop()
        elif mutation == "duplicate_field":
            verdict["field_audits"][-1] = verdict["field_audits"][0]
        elif mutation == "missing_join":
            audit["assertions"].pop()
        elif mutation == "changed_join":
            # Text is immutable input, never an accepted provider rewrite of an awkward join.
            assertion["text"] = SCENE_JOIN.replace("where", "and")
        elif mutation == "reordered_fragments":
            audit["assertions"].reverse()
        elif mutation == "duplicate_fragment":
            audit["assertions"][-1] = assertion
        elif mutation == "unknown_segment":
            assertion["segment_id"] = 999999
        elif mutation == "unknown_source":
            assertion["source_ids"] = [999999]
        elif mutation == "roster_for_state":
            _field_audit(verdict, payload, "/entities/0/state")["assertions"][0]["source_ids"] = [-1]
        elif mutation == "no_evidence":
            assertion["source_ids"] = []
        elif mutation == "empty_assertions":
            audit["assertions"] = []
        else:
            assertion.pop("segment_id")
            assertion["text"] = None
        return verdict, {"in": 10}

    with pytest.raises(ExtractionSafetyError, match="unavailable or invalid") as invalid:
        review_extraction(SCENE_SOURCE, value, roster, complete=reviewer)
    assert not isinstance(invalid.value, ExtractionGroundingRejected)


def test_identity_roster_evidence_does_not_import_a_state_or_future_database_metadata():
    roster = [{"entity_id": 999, "canonical_name": "Mara", "type": "character", "aliases": ["May"],
               "future_state": "invented future location"}]
    value = _scene_extraction("Mara arrived at the lighthouse.")
    value["entities"] = value["entities"][:1]
    value["entities"][0]["matched_roster"] = True

    def reviewer(_system, user, **_kwargs):
        assert "future_state" not in user and "invented future location" not in user
        verdict = canned_grounding_verdict(user)
        payload = review_payload(user)
        _field_audit(verdict, payload, "/entities/0/identity")["assertions"][0]["source_ids"] = [-1]
        return verdict, {"in": 10}

    review_extraction("May arrived at the lighthouse.", value, roster, complete=reviewer)


@pytest.mark.parametrize("last_failure", [None, "omitted", "unsupported", "outage", "budget"])
def test_dense_extraction_batches_every_whole_field_and_withholds_partial_clearance(last_failure):
    """Exercise a realistic-sized payload without equating serialized size with model token usage."""
    names = [f"Reader{index}" for index in range(30)]
    source = " ".join(f"{name} waited in the hall." for name in names)
    value = {
        "chapter_summary": "Thirty readers waited in the hall.",
        "entities": [{"canonical_name": name, "type": "character", "aliases": [],
                      "matched_roster": False, "state": "Waited in the hall."} for name in names],
        "relationships": [],
        "events": [{"summary": f"{name} waited in the hall.", "participants": [name]}
                   for name in names[:20]],
        "themes": [],
    }
    calls = []

    def reviewer(_system, user, **_kwargs):
        payload = review_payload(user)
        verdict = canned_grounding_verdict(user)
        calls.append((payload, verdict))
        assert reviewed_source(user) == source
        assert 1 <= len(payload["fields"]) <= 24
        assert len(verdict["field_audits"]) == len(payload["fields"])
        assert payload["extraction"] == value
        by_id = {field["id"]: field for field in payload["fields"]}
        for audit in verdict["field_audits"]:
            field = by_id[audit["field_id"]]
            assert [assertion["segment_id"] for assertion in audit["assertions"]] == [
                segment["id"] for segment in field["segments"]
            ]
            assert "".join(segment["text"] for segment in field["segments"]) == field["text"]
            assert all("text" not in assertion for assertion in audit["assertions"])
        if payload["fields"][-1]["id"] == 100:
            if last_failure == "omitted":
                verdict["field_audits"].pop()
            elif last_failure == "unsupported":
                verdict["unsupported_claims"] = ["The final action is unsupported."]
            elif last_failure == "outage":
                raise RuntimeError("unavailable")
            elif last_failure == "budget":
                from app.cost import CostCeilingExceeded
                raise CostCeilingExceeded("budget exhausted")
        return verdict, {"in": 100, "out": 100}

    if last_failure:
        from app.cost import CostCeilingExceeded
        with pytest.raises(CostCeilingExceeded if last_failure == "budget" else ExtractionSafetyError):
            review_extraction(source, value, [], complete=reviewer)
    else:
        review_extraction(source, value, [], complete=reviewer).assert_matches(source, value)
    assert 1 < len(calls) <= 16
    assert [f["id"] for data, _ in calls for f in data["fields"]] == list(range(101))


def test_real_punctuation_and_modifier_boundaries_are_input_references_not_a_copy_task():
    # Attempt 8 was otherwise valid but copying dropped commas/semicolons. Do not weaken coverage:
    # bind every original character to deterministic segments so provider punctuation is irrelevant.
    summary = ("Mara, whom Jonas calls May, arrived at the lighthouse, and Jonas lent her a brass key; "
               "Mara did not lend him a key.")
    source = SCENE_SOURCE + " Mara did not lend a key to Jonas."
    seen = []

    def reviewer(_system, user, **_kwargs):
        payload = review_payload(user)
        field = payload["fields"][0]
        seen.append(field)
        assert "".join(segment["text"] for segment in field["segments"]) == summary
        assert field["text"] == summary
        assert field["segments"][0]["text"] == "Mara,"
        assert any(segment["text"].endswith(";") for segment in field["segments"])
        return canned_grounding_verdict(user), {"in": 10}

    review_extraction(source, _scene_extraction(summary), [], complete=reviewer)
    assert len(seen) == 1


@pytest.mark.parametrize("text", [
    "", " \n\t", "Dr. Mara arrived.\nJonas waited!  Why?\n", "阿明到了。 他留下了钥匙！\n",
    ("Iona opened the gate. " * 2000), "x" * 9000,
], ids=["empty", "whitespace", "abbreviations", "native", "many_sentences", "long_sentence"])
def test_numbered_evidence_preserves_all_source_text_with_bounded_passage_overhead(text):
    passages = source_passages(text)
    assert "".join(part["text"] for part in passages) == text
    assert [part["id"] for part in passages] == list(range(1, len(passages) + 1))
    assert len(passages) <= 256


def test_clearance_rejects_mutated_payload_before_commit(tmp_path):
    store = Store(data_dir=str(tmp_path))
    try:
        client = LLMClient(provider="stub", allow_stub=True, env={})
        chapter = {"key": "one", "ordinal": 1, "text": TEXT}
        clearance = review_extraction(TEXT, EXTRACTION, [], complete=_cleared)
        prepared = prepare_chapter(chapter, EXTRACTION, client, roster=[], clearance=clearance)
        prepared["extraction"]["chapter_summary"] = "Aldric poisoned Berenice."
        with store.book("b", meta={"title": "B"}) as mem:
            with pytest.raises(ExtractionSafetyError, match="does not match"):
                ingest_chapter(mem, chapter, prepared)
            assert mem.view(1).chapters() == []
            assert mem.view(1).timeline() == []
    finally:
        store.close()


def test_real_provider_preparation_requires_an_already_paid_clearance():
    class RealProvider:
        provider = "openai-compatible"

    with pytest.raises(ExtractionSafetyError, match="requires a chapter-grounding review"):
        prepare_chapter({"key": "one", "ordinal": 1, "text": TEXT}, EXTRACTION,
                        RealProvider(), roster=[])


def test_merging_clearances_requires_exact_complete_text_and_every_part_payload():
    part = Extraction.model_validate(EXTRACTION).model_dump(mode="json")
    clearance = review_extraction(TEXT, part, [], complete=_cleared)
    merged, whole = merge_reviewed_extractions(TEXT + TEXT, [(TEXT, part, clearance)] * 2)
    whole.assert_matches(TEXT + TEXT, merged)
    with pytest.raises(ExtractionSafetyError, match="complete chapter"):
        merge_reviewed_extractions(TEXT + " missing tail", [(TEXT, part, clearance)])
    with pytest.raises(ExtractionSafetyError, match="does not match"):
        merge_reviewed_extractions(TEXT, [(TEXT, {**part, "chapter_summary": "changed"}, clearance)])


@pytest.mark.parametrize("book_type", ["novel", "reference"])
def test_stub_extraction_uses_source_body_and_never_invents_prompt_or_roster_names(book_type):
    client = LLMClient(provider="stub", allow_stub=True, env={})
    roster = [{"entity_id": 1, "canonical_name": "Berenice", "type": "character", "aliases": []}]
    text = "Aldric walked alone by the river."
    extracted, _usage = client.complete(
        extract_system_for(book_type),
        extract_user_prompt("Vesper", roster, text, book_type=book_type), schema=Extraction,
    )
    assert [entity["canonical_name"] for entity in extracted["entities"]] == ["Aldric"]
    assert "Berenice" not in extracted["chapter_summary"] and "Vesper" not in extracted["chapter_summary"]
    assert text in extracted["chapter_summary"]
