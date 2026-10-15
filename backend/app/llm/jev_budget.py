"""One bounded typed evaluation through the existing durable per-book cost ledger."""
import json

from app.cost.limits import BudgetedResult, CostCeilingExceeded, usd_of


def budgeted_jev(catalog, settings, jev, book_id, *, phase, state, questions):
    # UTF-8 bytes deliberately overestimate tokens, just as the completion budget does.
    estimated = len(json.dumps({"model": jev.model, "state": state, "questions": questions},
                               ensure_ascii=False).encode("utf-8")) + 256
    # Stay inside the default 32k per-question context; no long-context override.
    if estimated > min(settings.cost_max_input_tokens_per_call, 30000):
        from app.llm.jev import JevUnavailable
        raise JevUnavailable("Jev input requires the existing generative review")
    reserved = {"in": estimated, "out": settings.cost_max_output_tokens_per_call}
    try:
        reservation = catalog.reserve_cost(
            book_id, phase=phase, model=jev.model, input_tokens=reserved["in"],
            output_tokens=reserved["out"], usd=usd_of(jev.model, reserved),
            max_input_tokens=settings.cost_max_input_tokens_per_book,
            max_output_tokens=settings.cost_max_output_tokens_per_book,
            max_usd=settings.cost_max_usd_per_book,
        )
    except RuntimeError as exc:
        raise CostCeilingExceeded(str(exc)) from exc
    try:
        answers, usage = jev.evaluate(state, questions)
    except Exception:
        catalog.settle_cost(book_id, reservation, phase=f"{phase}-failed-reserved")
        raise
    cost = usd_of(jev.model, usage)
    catalog.note_reservation_actual(book_id, reservation, input_tokens=usage["in"],
                                    output_tokens=usage["out"], usd=cost)
    if usage["in"] > reserved["in"] or usage["out"] > reserved["out"]:
        catalog.settle_cost(book_id, reservation, phase=f"{phase}-provider-limit-violation")
        raise CostCeilingExceeded("Jev exceeded its reserved token limit")
    catalog.settle_cost(book_id, reservation)
    return BudgetedResult(answers, usage, cost, reservation)
