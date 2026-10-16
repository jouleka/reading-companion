"""Independent review for an unchanged prepared answer to a new reader request."""
from app.ask import (ASK_JUDGE_SYSTEM, AskDraft, AskRetrievalLimit, AskVerdict,
                     ask_judge_prompt, retrieve_ask_sources)
from app.cost import CostCeilingExceeded, budgeted_completion
from app.llm.client import ProviderUnavailable


class ReuseVerdict(AskVerdict):
    compatible_question: bool


REUSE_SYSTEM = ASK_JUDGE_SYSTEM + (
    " Also set compatible_question=true ONLY if the NEW reader_question has established "
    "presuppositions, unambiguous referents and the same occasion as this answer. "
    "A neutral yes/no question does not assert its proposition; a why-question asserts its "
    "event and needs an explicit reason. Reject unsupported new motives, ownership, timing, "
    "identities and instructions to reveal unread material. The original question is not proof. "
    "Check ALL requested details against the unchanged answer. Return missing_details for "
    "omissions. Do not rewrite, supplement or silently correct an answer to make it qualify."
)


def review_reuse(question, candidate, *, frontier, store, atoms, catalog, settings, client,
                 book_id, remember):
    payload = candidate["payload"]
    draft = AskDraft.model_validate({k: payload[k] for k in ("insufficient_evidence", "claims")},
                                    strict=True)
    sources = [{**c, "text": c["excerpt"]} for c in payload["citations"]]
    try:
        with store.book(book_id) as mem:
            view = mem.view(frontier)
            scene = retrieve_ask_sources(question, bookmark=frontier,
                chapters=[r for r in view.chapters() if r["revealed_at"] == frontier],
                read_chapter=view.raw_text, semantic_hits=(), atoms=atoms, limit=4)
    except AskRetrievalLimit:
        return False
    try:
        result = budgeted_completion(catalog, settings, client, book_id, phase="answer-reuse-review",
            system=REUSE_SYSTEM, user=ask_judge_prompt(draft, sources, question=question,
                frontier=frontier, scene_sources=scene, coverage_sources=sources),
            tier="cheap", schema=ReuseVerdict)
        remember(result)
        verdict = ReuseVerdict.model_validate(result.value, strict=True)
    except (CostCeilingExceeded, ProviderUnavailable):
        raise
    except Exception:
        return False
    return (verdict.compatible_question and not verdict.references_future
            and not verdict.unsupported_claims and not verdict.missing_details)
