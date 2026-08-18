"""LIT-14 — the runtime LLM-judge backstop (ADR 0004 Vector 3). It is the semantic net for the class
the deterministic gate structurally cannot catch: a paraphrased FUTURE EVENT in past tense with no
future name and no future-tense modal (the LIT-25 subject-binding residual — 'Fyodor was murdered.'
once 'murder' has legitimately entered the facts). ``references_future`` is a HARD blocker;
``unsupported_claims`` is a soft signal; the wrapper fails CLOSED (an LLM error = no verdict = unsafe,
per ADR 0004: 'a recap with no judge verdict counts as unsafe')."""
import pytest

from app.eval.spoiler_gate.judge import (
    JUDGE_SYSTEM,
    JudgeUnavailable,
    JudgeVerdict,
    judge_prompt,
    judge_recap,
)


class FakeClient:
    """A client whose ``complete`` returns a canned verdict (or raises), recording each call."""

    def __init__(self, verdict=None, exc=None):
        self._verdict = verdict
        self._exc = exc
        self.calls = []

    def complete(self, system, user, tier="cheap", schema=None):
        self.calls.append({"system": system, "user": user, "tier": tier, "schema": schema})
        if self._exc is not None:
            raise self._exc
        return self._verdict, {"in": 10, "out": 5}


FACTS = {
    "characters": ["Fyodor", "Mitya"],
    "chapter_summaries": ["Fyodor married twice and neglected his sons."],
    "events": ["A visitor confessed to a murder he committed years ago."],
    "aliases": ["Dmitri"],
}


def test_judge_prompt_contains_the_recap_and_the_facts():
    p = judge_prompt("Fyodor married twice.", FACTS)
    assert "Fyodor married twice." in p                       # the recap under review
    assert "A visitor confessed to a murder he committed years ago." in p   # the supplied facts
    assert JUDGE_SYSTEM                                        # a non-empty judge charter exists


def test_judge_prompt_renders_empty_sections_as_none_not_a_bare_bullet():
    p = judge_prompt("a recap", {"characters": [], "chapter_summaries": [], "events": []})
    assert "KEY EVENTS:\n(none)" in p           # not a dangling "- " line
    assert "\n- \n" not in p and not p.rstrip().endswith("- ")


def test_judge_passes_a_safe_recap():
    client = FakeClient(verdict={"references_future": False, "unsupported_claims": []})
    verdict, usage = judge_recap(client, "Fyodor married twice and neglected his sons.", FACTS)
    assert verdict["references_future"] is False
    assert client.calls[0]["tier"] == "large"                 # the judge runs on the capable tier
    assert client.calls[0]["schema"] is JudgeVerdict          # structured verdict, not free text
    assert usage["in"] >= 0


def test_judge_flags_a_subject_bound_future_event():
    # the residual: 'murder' is in the facts (the visitor's past confession), but 'Fyodor was
    # murdered' binds it to a future outcome — deterministically invisible, the judge's job
    client = FakeClient(verdict={"references_future": True,
                                 "unsupported_claims": ["Fyodor was murdered"]})
    verdict, _ = judge_recap(client, "Fyodor was murdered by his son.", FACTS)
    assert verdict["references_future"] is True
    assert verdict["unsupported_claims"]


def test_judge_fails_closed_on_an_llm_error():
    client = FakeClient(exc=RuntimeError("provider 500"))
    with pytest.raises(JudgeUnavailable):
        judge_recap(client, "anything", FACTS)


def test_judge_fails_closed_on_a_malformed_verdict():
    client = FakeClient(verdict={"unsupported_claims": []})   # references_future missing
    with pytest.raises(JudgeUnavailable):
        judge_recap(client, "x", FACTS)


def test_judge_verdict_is_openai_strict_shaped():
    # same contract as the extraction schema: extra=forbid + both fields required so the strict
    # transform is a no-op, not a repair
    assert JudgeVerdict.model_config.get("extra") == "forbid"
    assert set(JudgeVerdict.model_fields) == {"references_future", "unsupported_claims"}
