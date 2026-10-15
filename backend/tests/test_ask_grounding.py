"""Offline lexical eligibility: dispatch tests do not certify identity, speech, or family truth."""
import json

import pytest

from app.ask_grounding import preserves_modal_qualification, semantic_review_eligible


CHEER = ("“And always so, all our lives hand in hand! Hurrah for Karamazov!” Kolya cried once more "
         "rapturously, and once more the boys took up his exclamation: “Hurrah for Karamazov!”")
SPEECH = [{"text": 'Aldric said “Welcome!” Berenice heard him. The boys echoed “Welcome!”'}]
FAMILY = [{
    "id": 1, "ordinal": 1, "chapter_key": "one", "title": "The family",
    "text": "He was married twice, and had three sons, the eldest, Dmitri, by his first "
            "wife, and two, Ivan and Alexey, by his second.",
}]


def test_real_identity_hard_clause_can_reach_the_semantic_judge_without_importing_a_new_name():
    sources = [{"text": "Perezvon ran to Ilusha, who recognized him as Zhutchka."}]
    assert semantic_review_eligible("Perezvon is Zhutchka", sources)
    # The action-bearing remainder of this real revised answer is NOT granted an identity bypass.
    assert not semantic_review_eligible(
        "Perezvon is Zhutchka, not a different dog: Ilusha recognizes Perezvon as Zhutchka, "
        "and Kolya says he trained him.", sources,
    )


@pytest.mark.parametrize("claim", [
    "Aldric is Berenice.", "Aldric was Berenice", "Алексей is Алёша.",
])
def test_named_copular_labels_are_eligible_only_with_own_cited_name_surfaces(claim):
    assert semantic_review_eligible(claim, [{"text": "Aldric Berenice Алексей Алёша"}])
    assert not semantic_review_eligible(claim, [{"text": "An unrelated quiet garden."}])


@pytest.mark.parametrize("claim", [
    "Kolya leads the last cheer, “Hurrah for Karamazov!” and the boys repeat it.",
    "Kolya leads the final cheer, “Hurrah for Karamazov!”",
    'Kolya shouts "Hurrah for Karamazov!" and the boys repeat it.',
    '“Hurrah for Karamazov!” Kolya cried.',
])
def test_real_exact_cheer_with_only_speech_attribution_reaches_semantic_review(claim):
    assert semantic_review_eligible(claim, [{"text": CHEER}])


@pytest.mark.parametrize("claim", [
    'Aldric says “Welcome!”', '“Welcome!” Aldric said.',
    'Aldric shouts “Welcome!” and the boys repeat it.',
    'Aldric leads the cheer, “Welcome!” and Berenice echoes it.',
])
def test_exact_speech_paraphrases_do_not_need_the_same_reporting_verb_as_the_source(claim):
    assert semantic_review_eligible(claim, SPEECH)


@pytest.mark.parametrize("claim", [
    'Aldric murdered Berenice, “Welcome!”',
    'Aldric says “Welcome!” and murders Berenice.',
    'Aldric says “Welcome!” after killing Berenice.',
    'Aldric says “Welcome!” because Berenice died.',
    'Aldric says “Welcome!” while Berenice fled.',
    'Aldric says “Welcome!” and Berenice dies.',
    'Aldric leads Berenice, “Welcome!”',
    'Aldric says “Welcome!” and is Berenice.',
    'Aldric says “Welcome!” then poisons Berenice.',
    'Aldric says “Welcome!” and Berenice repeats it because Aldric died.',
    'Aldric says “Welcome!” @EXACT_QUOTE@',
])
def test_real_quotation_does_not_license_invented_actions_or_attached_cause_time(claim):
    assert not semantic_review_eligible(claim, SPEECH)


@pytest.mark.parametrize("claim", [
    "Aldric is Murdered.", "Aldric is Dead", "Aldric is Berenice's",
    "Aldric is Berenice, who murdered Corvus.", "Aldric is Berenice because they met.",
    "Aldric is Berenice?", "Aldric killed Berenice.", "The murderer is Aldric.",
])
def test_identity_exception_cannot_hide_an_action_state_possession_or_question(claim):
    source = "Aldric Berenice Corvus Murdered Dead The murderer met"
    assert not semantic_review_eligible(claim, [{"text": source}])


def test_proper_names_must_be_present_in_these_citations_not_other_available_passages():
    assert not semantic_review_eligible('Corvus says “Welcome!”', SPEECH)
    assert not semantic_review_eligible('Aldric says “Hurrah for Corvus!”', SPEECH)
    assert not semantic_review_eligible("Aldric is Corvus", SPEECH)
    # Eligibility remains only a dispatch decision: both observed names can still be the wrong
    # speaker/alias link. The caller MUST independently judge those actual assertions afterward.
    assert semantic_review_eligible('Berenice says “Welcome!”', SPEECH)


def test_quote_must_be_a_complete_exact_utterance_in_one_cited_passage():
    assert not semantic_review_eligible('Aldric says “Goodbye!”', SPEECH)
    assert not semantic_review_eligible('Aldric says “Welcome!”', [{"text": "Aldric said Welcome!"}])
    assert not semantic_review_eligible('Aldric says “killed Berenice”', [
        {"text": 'Aldric said “I never killed Berenice”.'},
    ])
    assert not semantic_review_eligible('Aldric says “Welcome home!”', [
        {"text": 'Aldric said “Welcome'}, {"text": ' home!”'},
    ])
    assert not semantic_review_eligible('Aldric says “Welcome!” and Berenice says “Welcome!”', SPEECH)


def test_quote_does_not_license_an_unobserved_collective_subject():
    assert not semantic_review_eligible('Aldric says “Welcome!” and the crowd repeats it.', SPEECH)


def test_empty_malformed_or_oversized_units_stay_with_the_existing_lexical_rejection():
    assert not semantic_review_eligible("", SPEECH)
    assert not semantic_review_eligible("Aldric is Berenice", [])
    assert not semantic_review_eligible("Aldric is Berenice", None)
    assert not semantic_review_eligible("Aldric is Berenice", ["not a source object"])
    assert not semantic_review_eligible("Aldric is Berenice", [{"text": 5}])
    assert not semantic_review_eligible("Aldric is " + "Berenice " * 100, SPEECH)


def test_real_sons_to_brothers_paraphrase_reaches_judge_despite_zero_lexical_coverage():
    from app.ask import source_facts, validate_ask_draft
    from app.eval.spoiler_gate.grounding import ground_recap

    claim = "His brothers are Dmitri and Ivan."
    # Exact source wording names three sons; none of its non-name words says brother.
    assert "brother" not in FAMILY[0]["text"]
    assert ground_recap(claim, source_facts(FAMILY))["hard"][0]["coverage"] == 0
    assert semantic_review_eligible(claim, FAMILY)
    assert validate_ask_draft({
        "insufficient_evidence": False,
        "claims": [{"text": claim, "citation_ids": [1]}],
    }, FAMILY).claims[0].text == claim


@pytest.mark.parametrize("claim", [
    "Alexey's two brothers are Dmitri and Ivan.",
    "Alexey’s brothers are Dmitri and Ivan.",
    "Dmitri and Ivan are Alexey's brothers.",
    "Alexey's 2 brothers are Dmitri and Ivan.",
    "Dmitri was the eldest of three brothers.",
])
def test_family_relationship_counts_and_possessives_use_only_own_observed_names(claim):
    assert semantic_review_eligible(claim, FAMILY)


def test_family_eligibility_does_not_require_english_names_or_treat_may_as_a_modal():
    assert semantic_review_eligible("Алексей is Иван’s brother.", [{
        "text": "Иван and Алексей were the two sons of Николай.",
    }])
    assert semantic_review_eligible("May is Ivan's sister.", [{
        "text": "May and Ivan were the children of Aldric.",
    }])


def test_observed_but_wrong_family_pair_is_reviewable_never_cleared_by_eligibility():
    from app.ask import AskVerdict, judge_ask_draft, validate_ask_draft

    sources = [dict(FAMILY[0], text="Aldric had a son, Corvus. Berenice had a son, Dorian.")]
    claim = "Corvus and Dorian are brothers."
    assert semantic_review_eligible(claim, sources)
    draft = validate_ask_draft({
        "insufficient_evidence": False,
        "claims": [{"text": claim, "citation_ids": [1]}],
    }, sources)
    reviewed = []

    def reject_family(_system, prompt, *, tier, schema):
        reviewed.append(json.loads(prompt.split("(untrusted JSON data):\n", 1)[1]))
        assert tier == "medium"
        assert schema is AskVerdict
        return {"references_future": False,
                "unsupported_claims": ["Different named parents do not establish shared parentage."]}, {}

    verdict, _ = judge_ask_draft(None, draft, sources, tier="medium", complete=reject_family)
    assert verdict["unsupported_claims"]
    assert reviewed[0]["claims"][0]["claim"] == claim
    assert reviewed[0]["claims"][0]["cited_passages"][0]["text"] == sources[0]["text"]


def test_family_words_must_occur_in_own_cited_raw_and_names_cannot_come_from_other_sources():
    from app.ask import AskSafetyError, validate_ask_draft

    unrelated = dict(FAMILY[0], text="Alexey carried the lantern to Dmitri and Ivan.")
    assert not semantic_review_eligible("Alexey's brothers are Dmitri and Ivan.", [unrelated])
    assert not semantic_review_eligible("His brothers are Dmitri and Corvus.", FAMILY)
    other = dict(FAMILY[0], id=2, text="Corvus was Aldric's son.")
    with pytest.raises(AskSafetyError, match="named identity"):
        validate_ask_draft({
            "insufficient_evidence": False,
            "claims": [{"text": "His brothers are Dmitri and Corvus.", "citation_ids": [1]}],
        }, [*FAMILY, other], known_names=["Corvus"])


@pytest.mark.parametrize("claim", [
    "His brothers were murdered.",
    "His brothers are Dmitri and Ivan, who were murdered.",
    "His brothers are Dmitri and Ivan, and Ivan fled.",
    "His brothers are Dmitri and Ivan because Alexey is their father.",
    "His brothers are Dmitri and Ivan, so Alexey is their father.",
    "His brothers are Dmitri and Ivan after Alexey was their father.",
    "His brothers are Dmitri and Ivan; Alexey died.",
    "His brothers will be Dmitri and Ivan.",
    "His brothers are Dmitri and Ivan before Alexey returns.",
    "His brothers are Dmitri and Ivan, therefore Alexey is their father.",
    "His brothers are Dmitri and Ivan while Alexey is their father.",
    "His brothers are Dead and Murdered.",
    "His brothers are Dmitri and Ivan?",
    "His brothers are Dmitri and Ivan @EXACT_QUOTE@",
    "Alexey has 11 brothers.",
    "His brothers are Dmitri and Ivan on the third day.",
])
def test_family_exception_cannot_carry_actions_cause_time_future_or_extra_descriptions(claim):
    # Include the attacks' action words too: observing vocabulary does not permit event dispatch.
    sources = [dict(FAMILY[0], text=FAMILY[0]["text"] + (
        " Alexey was their father. Ivan fled after Alexey died and Dmitri was murdered. "
        "Dead Murdered Returns Third Day."
    ))]
    assert not semantic_review_eligible(claim, sources)


def test_family_route_requires_a_named_relationship_not_only_role_or_count_words():
    assert not semantic_review_eligible("His brothers are his sons.", FAMILY)
    assert not semantic_review_eligible("Dmitri and Ivan.", FAMILY)
    assert not semantic_review_eligible("His brothers, Dmitri and Ivan.", FAMILY)


@pytest.mark.parametrize(("modal", "predicate", "object_"), [
    ("must", "spat", "it out"), ("might", "dropped", "the glass"),
    ("may", "drowned", "in the river"), ("could", "broken", "the cup"),
    ("would", "fallen", "from the ledge"), ("should", "escaped", "the room"),
])
def test_source_derived_modal_perfect_predicate_cannot_be_reported_as_a_direct_fact(
    modal, predicate, object_,
):
    cited = [{"text": f"Aldric {modal} have {predicate} {object_}."}]
    assert not preserves_modal_qualification(f"Aldric had {predicate} {object_}.", cited)
    assert preserves_modal_qualification(f"Aldric {modal} have {predicate} {object_}.", cited)


def test_real_pin_inference_cannot_be_promoted_by_attribution_or_a_semicolon():
    cited = [{"text": (
        "Krassotkin said, ‘So he must have spat it out, since he is alive. "
        "You did not see him do it.’ Kolya spoke to Ilusha."
    )}]
    assert not preserves_modal_qualification(
        "Krassotkin says Zhutchka is alive; he had spat out the food with the pin in it.", cited,
    )
    assert not preserves_modal_qualification("Kolya explains that the dog spat out the pin.", cited)
    assert not preserves_modal_qualification("Kolya says that the dog spat out the pin.", cited)
    assert preserves_modal_qualification("Kolya infers that the dog had spat out the pin.", cited)
    assert preserves_modal_qualification("Kolya says that the dog must have spat it out.", cited)


@pytest.mark.parametrize("claim", [
    "It is possible that he dropped the glass.",
    "Perhaps he dropped the glass.",
    "Berenice infers that he dropped the glass.",
    "Berenice conjectured that he had dropped the glass.",
])
def test_explicit_inference_or_possibility_qualifies_only_its_own_clause(claim):
    assert preserves_modal_qualification(claim, [{"text": "He might have dropped the glass."}])


@pytest.mark.parametrize("claim", [
    "Berenice infers what happened; he dropped the glass.",
    "It is possible that he drowned. He had dropped the glass.",
    "He must have dropped the glass; he had dropped it in anger.",
    "He may have drowned; he drowned in the river.",
    "He had dropped the glass, and he must have dropped it twice.",
])
def test_a_prior_or_repeated_qualified_assertion_cannot_license_a_direct_assertion(claim):
    cited = [{"text": "He might have dropped the glass. He may have drowned in the river."}]
    assert not preserves_modal_qualification(claim, cited)


@pytest.mark.parametrize("boundary", [", but ", ", yet ", ", however ", ", HOWEVER, "])
def test_unrelated_uncertainty_cannot_cross_an_adversative_clause(boundary):
    cited = [{"text": "The record is incomplete. Aldric must have dropped the glass."}]
    assert not preserves_modal_qualification(
        "Perhaps the record is incomplete" + boundary + "Aldric dropped the glass.", cited,
    )
    assert preserves_modal_qualification(
        "Perhaps the record is incomplete" + boundary + "Aldric must have dropped the glass.", cited,
    )
    assert preserves_modal_qualification(
        "Perhaps the record is incomplete" + boundary + "perhaps Aldric dropped the glass.", cited,
    )


def test_an_adversative_does_not_remove_qualification_from_its_original_assertion():
    cited = [{"text": "The record is incomplete. Aldric must have dropped the glass."}]
    assert preserves_modal_qualification("Perhaps Aldric dropped the glass.", cited)
    assert preserves_modal_qualification("Aldric must have dropped the glass.", cited)
    assert preserves_modal_qualification(
        "Perhaps Aldric dropped the glass, but the record is incomplete.", cited,
    )


def test_observed_direct_predicate_leaves_its_role_and_negation_to_the_semantic_judge():
    cited = [{"text": "Aldric might have dropped the glass. Berenice dropped the plate."}]
    # A direct occurrence prevents a lexical-only decision. This is NOT proof Aldric dropped it.
    assert preserves_modal_qualification("Aldric dropped the glass.", cited)
    assert preserves_modal_qualification("Berenice did not drop the plate.", cited)


def test_modal_occurrences_in_other_citations_do_not_count_as_a_direct_source_predicate():
    assert not preserves_modal_qualification("Aldric drowned.", [
        {"text": "Aldric might have drowned."}, {"excerpt": "Berenice could have drowned."},
    ])
    assert preserves_modal_qualification("Aldric drowned.", [
        {"text": "Aldric might have drowned."}, {"excerpt": "Berenice drowned."},
    ])


def test_modal_negation_adverbs_and_a_verbatim_quote_keep_their_explicit_qualification():
    cited = [{"text": 'Aldric said “He must not have dropped the glass.” He might well have drowned.'}]
    assert not preserves_modal_qualification("He dropped the glass and drowned.", cited)
    assert preserves_modal_qualification('Aldric said “He must not have dropped the glass.”', cited)
    assert preserves_modal_qualification("He might well have drowned.", cited)


def test_common_auxiliaries_and_sources_without_modal_perfect_claims_do_not_add_restrictions():
    assert preserves_modal_qualification("Aldric had a brother and had done his work.", [
        {"text": "Aldric might have had a brother. He must have been nearby and could have done it."},
    ])
    assert preserves_modal_qualification("Aldric dropped the glass.", [
        {"text": "Aldric dropped the glass."},
    ])
    assert preserves_modal_qualification("Aldric dropped the glass.", [])


def test_qualification_check_never_clears_an_invented_event_or_decorated_quotation():
    claim = 'Aldric says “Welcome!” after killing Berenice.'
    assert preserves_modal_qualification(claim, SPEECH)
    assert not semantic_review_eligible(claim, SPEECH)


@pytest.mark.parametrize(("claim", "cited"), [
    ("", SPEECH), (None, SPEECH), ("He dropped it.", None),
    ("He dropped it.", ["not a source"]), ("He dropped it.", [{"text": 5}]),
])
def test_malformed_qualification_inputs_fail_closed(claim, cited):
    assert not preserves_modal_qualification(claim, cited)
