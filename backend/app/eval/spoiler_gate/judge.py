"""LIT-14 — the runtime LLM-JUDGE backstop (ADR 0004 Vector 3, the soft-signal reviewer the ADR
assumes). The deterministic gate (``score_recap`` + ``grounding``) catches future NAMES, future
TENSE, untraceable sentences, and (since LIT-27) explicit high-consequence entity/event-role
rebinding. The residual it cannot fully cover is implicit pronoun/coreference or open-domain NLI, so
a capable LLM still judges the recap against the same bookmark-bounded facts the synthesizer was
given. The judge remains defense in depth even for the explicit "Fyodor was murdered" class.

CONTRACT (ADR 0004): ``references_future`` is a HARD blocker (the caller regenerates / fails closed);
``unsupported_claims`` is a SOFT signal — currently DROPPED by the caller (kept off the payload per the
READER-DATA-ONLY rule; a debug sink for it is future observability, not wired today). The judge is
itself an LLM, so it is paired with the deterministic gate, never trusted alone — and it fails CLOSED:
any judge error / missing verdict is treated by the caller as UNSAFE (no recap), because a recap that
was never judged has not been cleared. Spoiler-safe by construction: the judge sees ONLY the
bookmark-bounded supplied facts + the recap (which already passed the deterministic gate)."""
from pydantic import BaseModel, ConfigDict

from app.cost.limits import CostCeilingExceeded


class JudgeVerdict(BaseModel):
    """OpenAI-strict-shaped (extra=forbid + all required) so the native ``.parse`` path and the
    generic ``to_strict_json_schema`` path both accept it without repair."""

    model_config = ConfigDict(extra="forbid")

    references_future: bool
    unsupported_claims: list[str]


class JudgeUnavailable(RuntimeError):
    """The judge could not render a usable verdict (LLM error, or a reply missing the verdict shape).
    The caller MUST treat this as unsafe and withhold the recap (ADR 0004: no verdict = unsafe)."""


JUDGE_SYSTEM = (
    "You are a spoiler-safety reviewer for a 'catch me up' reading companion. You are given the ONLY "
    "facts the reader has unlocked so far, and a recap written from them.\n\n"
    "Set references_future = true ONLY when the recap reveals a FUTURE PLOT DEVELOPMENT: a concrete "
    "event, outcome, death, crime, betrayal, or fate that HAPPENS in the story but is NOT established "
    "by the supplied facts. These are the real spoilers — stating who killed whom, how a conflict "
    "resolves, who is convicted, or a character's fate, when the facts do not already say so. Even if "
    "the individual words all appear in the facts, binding them into a new event the facts do not "
    "assert is a future reveal: e.g. the facts mention that 'a murder was confessed years ago', but "
    "the recap says 'Fyodor was murdered by his son' — that outcome is not in the facts, so true.\n\n"
    "Do NOT set references_future for characterization, description, interpretation, or mild "
    "paraphrase that stays within the established situation — calling a neglectful father 'coarse', a "
    "character 'proud', or restating a known relationship is NOT a plot spoiler. List any such "
    "over-reach in unsupported_claims and leave references_future = false. references_future is about "
    "REVEALING WHAT HAPPENS NEXT, not about wording precision.\n\n"
    "When you are unsure whether a sentence reveals a genuine future EVENT, prefer references_future = "
    "true (withholding a recap is safe); when a sentence is clearly only characterization or "
    "description, references_future = false."
)


def _section(title, items):
    """A prompt section, or '(none)' rather than a dangling bare bullet when the list is empty
    (review LOW-2 — an early bookmark can have no events yet)."""
    items = list(items or [])
    return f"{title}:\n" + ("\n".join(f"- {x}" for x in items) if items else "(none)")


def judge_prompt(recap, facts):
    """Lay the supplied facts beside the recap for the judge (same fact surface the synthesizer got)."""
    return (
        "SUPPLIED FACTS (everything the reader has unlocked):\n\n"
        f"CHARACTERS: {', '.join(facts.get('characters', [])) or '(none)'}\n\n"
        + _section("CHAPTER SUMMARIES", facts.get("chapter_summaries", []))
        + "\n\n"
        + _section("KEY EVENTS", facts.get("events", []))
        + "\n\n"
        "RECAP TO REVIEW:\n" + (recap or "") + "\n\n"
        "Does the recap assert anything not supported by the supplied facts?"
    )


def judge_recap(client, recap, facts, *, tier="large", complete=None):
    """Run the judge over ``recap`` against ``facts``. Returns ``(verdict_dict, usage)``. Raises
    ``JudgeUnavailable`` on any LLM error or a reply that is not a well-formed verdict — the caller
    turns that into a fail-closed rejection. Must be called OUTSIDE the per-book lock (it does IO)."""
    try:
        runner = complete or client.complete
        verdict, usage = runner(JUDGE_SYSTEM, judge_prompt(recap, facts), tier=tier,
                                schema=JudgeVerdict)
    except CostCeilingExceeded:
        raise
    except Exception as e:                                    # any provider error = no verdict = unsafe
        raise JudgeUnavailable(f"judge LLM call failed: {type(e).__name__}") from e
    if not isinstance(verdict, dict) or not isinstance(verdict.get("references_future"), bool):
        raise JudgeUnavailable("judge returned no usable references_future verdict")
    verdict.setdefault("unsupported_claims", [])
    return verdict, usage
