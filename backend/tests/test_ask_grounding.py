"""Offline lexical-eligibility tests: these do not certify identity or speaker entailment."""
import pytest

from app.ask_grounding import semantic_review_eligible


CHEER = ("“And always so, all our lives hand in hand! Hurrah for Karamazov!” Kolya cried once more "
         "rapturously, and once more the boys took up his exclamation: “Hurrah for Karamazov!”")
SPEECH = [{"text": 'Aldric said “Welcome!” Berenice heard him. The boys echoed “Welcome!”'}]


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
