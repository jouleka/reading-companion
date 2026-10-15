"""Chapter-grounding regressions. Canned semantic verdicts test enforcement, not model accuracy."""
from copy import deepcopy
import json

import pytest

from app.ingest.extraction.pipeline import content_hash_of, ingest_chapter, prepare_chapter
from app.ingest.extraction.prompts import extract_system_for, extract_user_prompt
from app.ingest.extraction.schema import Extraction
from app.ingest.extraction.validation import (
    ExtractionSafetyError,
    assert_observed_entity_names,
    merge_reviewed_extractions,
    review_extraction,
)
from app.llm.client import LLMClient
from app.memory.store import Store


TEXT = "Aldric handed the copper lantern to Berenice. He stayed at the forge."
EXTRACTION = {
    "chapter_summary": "Berenice received a copper lantern from Aldric, who remained at the forge.",
    "entities": [
        {"canonical_name": "Aldric", "type": "character", "aliases": [],
         "matched_roster": False, "state": "remains at the forge"},
        {"canonical_name": "Berenice", "type": "character", "aliases": [],
         "matched_roster": False, "state": "has the copper lantern"},
    ],
    "relationships": [{"src": "Aldric", "dst": "Berenice", "rel_type": "other",
                       "label": "gave a copper lantern to"}],
    "events": [{"summary": "Berenice received the lantern from Aldric.",
                "participants": ["Aldric", "Berenice"]}],
    "themes": [{"name": "giving", "description": "Aldric gives Berenice a lantern."}],
}


def _cleared(*_args, **_kwargs):
    return {"references_future": False, "unsupported_claims": []}, {"in": 10, "out": 2}


def test_absent_new_name_is_rejected_before_any_review_or_memory_write(tmp_path):
    extraction = deepcopy(EXTRACTION)
    calls = []
    with pytest.raises(ExtractionSafetyError, match="unobserved"):
        review_extraction("Aldric walked alone by the river.", extraction, [],
                          complete=lambda *_a, **_k: calls.append(True))
    assert calls == []
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
        assert data["current_chapter_text"] == TEXT
        return {"references_future": False, "unsupported_claims": [mutation]}, {"in": 10}

    with pytest.raises(ExtractionSafetyError, match="could not be grounded"):
        review_extraction(TEXT, extraction, [], complete=fixture_reviewer)
    assert len(observed) == 1


def test_supported_paraphrase_pronoun_states_and_whole_extraction_reach_the_reviewer():
    seen = []

    def reviewer(system, user, **kwargs):
        data = json.loads(user.split("(untrusted JSON data):\n", 1)[1])
        seen.append(data)
        assert "pronouns" in system and "negation" in system
        assert kwargs["tier"] == "cheap"
        assert data["extraction"] == EXTRACTION
        return _cleared()

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
    native = "Алёша был в монастыре."
    review_extraction(native, extraction, [], complete=_cleared).assert_matches(native, extraction)

    roster = [{"entity_id": 7, "canonical_name": "Alexey Karamazov", "type": "character",
               "aliases": ["Alyosha", "Алёша"]}]
    # The source uses an existing alias and an explicit pronoun antecedent; the roster supports the
    # canonical/native alias association without importing any new action or location.
    review_extraction("Alyosha entered the monastery. He remained there.", extraction, roster,
                      complete=_cleared)


@pytest.mark.parametrize("verdict", [
    {"references_future": False},
    {"references_future": False, "unsupported_claims": "none"},
    {"references_future": "false", "unsupported_claims": []},
])
def test_missing_or_malformed_review_is_not_a_clearance(verdict):
    with pytest.raises(ExtractionSafetyError, match="unavailable or invalid"):
        review_extraction(TEXT, EXTRACTION, [], complete=lambda *_a, **_k: (verdict, {}))


def test_provider_review_failure_is_closed_and_does_not_expose_provider_text():
    def broken(*_args, **_kwargs):
        raise RuntimeError("private provider details and an invented future event")

    with pytest.raises(ExtractionSafetyError) as error:
        review_extraction(TEXT, EXTRACTION, [], complete=broken)
    assert str(error.value) == "extraction review was unavailable or invalid"


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
