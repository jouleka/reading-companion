"""LIT-27 deterministic entity/event-role binding regressions."""

from app.eval.spoiler_gate.binding import unsupported_event_bindings


ENTITIES = [
    {"entity_id": 1, "canonical_name": "Fyodor Pavlovitch Karamazov",
     "aliases": ["Fyodor Pavlovitch"]},
    {"entity_id": 2, "canonical_name": "Pavel Smerdyakov", "aliases": ["Smerdyakov"]},
    {"entity_id": 3, "canonical_name": "Dmitri Fyodorovitch Karamazov",
     "aliases": ["Dmitri", "Mitya"]},
    {"entity_id": 4, "canonical_name": "Adelaïda Ivanovna",
     "aliases": ["Fyodor Pavlovitch's first wife"]},
]


def _facts(*events):
    return {
        "characters": [entity["canonical_name"] for entity in ENTITIES],
        "aliases": [alias for entity in ENTITIES for alias in entity["aliases"]],
        "chapter_summaries": [],
        "events": list(events),
        "_entities": ENTITIES,
    }


def test_unrelated_event_vocabulary_cannot_be_rebound_to_visible_people():
    facts = _facts(
        "A mysterious visitor confessed to a murder fourteen years earlier.",
        "A judge discussed an old conviction from another province.",
    )
    hits = unsupported_event_bindings(
        "Fyodor was murdered. Dmitri was wrongly convicted.", facts
    )
    assert {(hit["family"], hit["role"]) for hit in hits} == {
        ("homicide", "patient"),
        ("conviction", "patient"),
    }


def test_homicide_synonyms_preserve_an_established_patient_binding():
    facts = _facts("Fyodor Pavlovitch Karamazov was murdered in his house.")
    assert unsupported_event_bindings("Fyodor was killed in his house.", facts) == []


def test_subject_object_swap_is_not_licensed_by_shared_words():
    facts = _facts("Smerdyakov murdered Fyodor.")
    hits = unsupported_event_bindings("Fyodor murdered Smerdyakov.", facts)
    assert {(hit["entity_id"], hit["role"]) for hit in hits} == {
        (1, "agent"),
        (2, "patient"),
    }


def test_named_agent_with_pronoun_object_still_requires_support():
    facts = _facts("A mysterious visitor confessed to committing a murder.")
    hits = unsupported_event_bindings("Smerdyakov killed him.", facts)
    assert any(hit["entity_id"] == 2 and hit["role"] == "agent" for hit in hits)


def test_passive_by_phrase_binds_both_patient_and_agent():
    facts = _facts("Fyodor was murdered by Smerdyakov.")
    assert unsupported_event_bindings("Fyodor was killed by Smerdyakov.", facts) == []


def test_a_known_death_does_not_license_an_invented_homicide():
    facts = _facts("Adelaïda died suddenly in Petersburg.")
    assert unsupported_event_bindings("Adelaïda was murdered.", facts)
    assert unsupported_event_bindings("Adelaïda was dead.", facts) == []


def test_homicide_entails_death_in_the_safe_direction_only():
    facts = _facts("Fyodor was murdered in his house.")
    assert unsupported_event_bindings("Fyodor died in his house.", facts) == []


def test_characterization_is_outside_the_high_consequence_binding_grammar():
    facts = _facts("Fyodor was a neglectful and coarse father.")
    assert unsupported_event_bindings("Fyodor was coarse and neglectful.", facts) == []


def test_relational_alias_does_not_steal_another_characters_name_token():
    facts = _facts("Fyodor was murdered.")
    assert unsupported_event_bindings("Fyodor was killed.", facts) == []


def test_suspicion_does_not_entail_the_suspected_event():
    facts = _facts("Ivan suspected that Smerdyakov murdered Fyodor.")
    assert unsupported_event_bindings("Smerdyakov murdered Fyodor.", facts)
    assert unsupported_event_bindings("Ivan suspected that Smerdyakov murdered Fyodor.", facts) == []


def test_an_accusation_does_not_entail_guilt_or_the_outcome():
    facts = _facts("Dmitri was accused of murdering Fyodor.")
    assert unsupported_event_bindings("Dmitri murdered Fyodor.", facts)
    assert unsupported_event_bindings("Dmitri was accused of murdering Fyodor.", facts) == []


def test_reflexive_hanging_binds_the_named_person_as_agent_and_patient():
    facts = _facts("Smerdyakov hangs himself.")
    assert unsupported_event_bindings("Smerdyakov hanged himself.", facts) == []
    assert unsupported_event_bindings("Smerdyakov died.", facts) == []


def test_duplicate_entity_rows_are_coalesced_by_full_name_aliases():
    entities = ENTITIES + [
        {"entity_id": 30, "canonical_name": "Dmitri Karamazov",
         "aliases": ["Dmitri Fyodorovitch Karamazov"]},
    ]
    facts = _facts("A visitor confessed to a murder.")
    facts["_entities"] = entities
    hits = unsupported_event_bindings("Dmitri murdered Fyodor.", facts)
    assert any(hit["family"] == "homicide" and hit["role"] == "agent" for hit in hits)


def test_ambiguous_shared_first_name_does_not_guess_an_identity():
    entities = [
        {"entity_id": 10, "canonical_name": "Natasha Alpha", "aliases": []},
        {"entity_id": 11, "canonical_name": "Natasha Beta", "aliases": []},
    ]
    facts = {"characters": [], "aliases": [], "chapter_summaries": [],
             "events": ["Natasha Alpha was arrested."], "_entities": entities}
    assert unsupported_event_bindings("Natasha was arrested.", facts)


def test_reduced_passive_does_not_turn_the_victim_into_the_agent():
    facts = _facts("Marfa discovered Fyodor murdered in his house.")
    assert unsupported_event_bindings("Fyodor was killed in his house.", facts) == []
    hits = unsupported_event_bindings("Fyodor murdered him.", facts)
    assert any(hit["entity_id"] == 1 and hit["role"] == "agent" for hit in hits)
