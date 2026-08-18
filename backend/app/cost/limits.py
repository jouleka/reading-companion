"""LIT-21 cost ceilings and bounded-completion helpers.

Input estimates deliberately use UTF-8 bytes as a conservative tokenizer-independent upper bound.
That over-reserves for ordinary prose, but it never assumes a provider-specific tokenizer and remains
safe for non-Latin text. The catalog owns durable, atomic reservations; this module owns provider-call
shaping and huge-input splitting.
"""
from __future__ import annotations

from dataclasses import dataclass
import json

from app.llm.client import LLMClient


class CostCeilingExceeded(RuntimeError):
    """A paid call was refused before provider IO because a configured ceiling would be exceeded."""


# Advisory USD pricing already used by the product. Hard token ceilings remain effective for unknown
# models; unknown pricing intentionally returns zero rather than pretending a current price is known.
_USD = {
    "gpt-4o-mini": (0.15e-6, 0.60e-6),
    "gpt-4o": (2.50e-6, 10.0e-6),
    "text-embedding-3-small": (0.02e-6, 0.0),
}


def usd_of(model, usage):
    for name, (input_price, output_price) in _USD.items():
        if name in (model or ""):
            return usage.get("in", 0) * input_price + usage.get("out", 0) * output_price
    return 0.0


def pricing_known(model: str | None) -> bool:
    """Whether the advisory table can truthfully price this provider model."""
    identity = model or ""
    return identity.startswith(("stub:", "stub-")) or any(name in identity for name in _USD)


def estimate_input_tokens(system: str, user: str, schema=None) -> int:
    """Conservative provider-independent upper bound, including message framing headroom."""
    schema_bytes = b""
    if schema is not None:
        schema_bytes = json.dumps(
            schema.model_json_schema(), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    return (len((system or "").encode("utf-8")) + len((user or "").encode("utf-8"))
            + len(schema_bytes) + 64)


def _split_bytes(text: str, limit: int) -> list[str]:
    if len(text.encode("utf-8")) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        raw = remaining.encode("utf-8")
        if len(raw) <= limit:
            chunks.append(remaining)
            break
        cut = limit
        while cut and (raw[cut] & 0b1100_0000) == 0b1000_0000:
            cut -= 1
        candidate = raw[:cut].decode("utf-8")
        boundary = max(candidate.rfind("\n\n"), candidate.rfind("\n"), candidate.rfind(" "))
        # Do not emit pathologically tiny chunks just to honor a nearby separator.
        if boundary >= max(1, len(candidate) // 2):
            candidate = candidate[: boundary + 1]
        if not candidate:
            raise ValueError("input chunk byte limit is too small for one Unicode code point")
        chunks.append(candidate)
        remaining = remaining[len(candidate):]
    return chunks


def split_text_for_prompt(text: str, *, prompt_without_text: str, system: str,
                          max_input_tokens: int, schema=None) -> list[str]:
    """Split one chapter at paragraph/newline/word boundaries to fit the hard input ceiling.

    Chunks are non-overlapping and concatenate byte-for-byte to the original chapter. They remain one
    chapter atom downstream; only provider IO is chunked.
    """
    overhead = estimate_input_tokens(system, prompt_without_text, schema)
    available = max_input_tokens - overhead
    if available <= 0:
        raise CostCeilingExceeded(
            f"prompt metadata alone exceeds the per-call input ceiling ({overhead} > {max_input_tokens})"
        )
    return _split_bytes(text, available)


def merge_extractions(parts: list[dict]) -> dict:
    """Merge validated extraction chunks without changing their shared chapter ordinal."""
    if not parts:
        raise ValueError("at least one extraction part is required")
    return {
        "chapter_summary": " ".join(
            part["chapter_summary"].strip() for part in parts if part["chapter_summary"].strip()
        ),
        "entities": [item for part in parts for item in part["entities"]],
        "relationships": [item for part in parts for item in part["relationships"]],
        "events": [item for part in parts for item in part["events"]],
        "themes": [item for part in parts for item in part["themes"]],
    }


@dataclass(frozen=True)
class BudgetedResult:
    value: object
    usage: dict
    usd: float
    reservation_id: str


def _production_complete(client, system, user, *, tier, schema, max_output_tokens):
    """Honor monkey-patched test doubles while capping every unmodified production client call."""
    method = client.complete
    if getattr(method, "__func__", None) is LLMClient.complete:
        return method(system, user, tier=tier, schema=schema,
                      max_output_tokens=max_output_tokens)
    return method(system, user, tier=tier, schema=schema)


def budgeted_completion(catalog, settings, client, book_id, *, phase, system, user, tier,
                        schema=None, chapter_ordinal=None, defer_settlement=False):
    """Reserve worst-case spend, run one bounded provider call, then settle or retain the reservation.

    Provider failures are conservatively settled from the reservation estimate because token usage is
    unavailable. A successful deferred call keeps its actual usage reserved until LIT-7 atomically
    publishes the chapter receipt and catalog row.
    """
    model = client._model_for(tier)
    estimated_input = estimate_input_tokens(system, user, schema)
    generic_reask = bool(
        schema is not None
        and getattr(client, "provider", None) == "openai-compatible"
        and not getattr(client, "_is_native_openai", False)
    )
    peak_input = estimated_input + (settings.cost_max_output_tokens_per_call + 512
                                    if generic_reask else 0)
    if peak_input > settings.cost_max_input_tokens_per_call:
        raise CostCeilingExceeded(
            f"completion input exceeds per-call ceiling "
            f"({peak_input} > {settings.cost_max_input_tokens_per_call})"
        )
    reserved_input = (estimated_input * 2 + settings.cost_max_output_tokens_per_call + 512
                      if generic_reask else estimated_input)
    reserved_output = settings.cost_max_output_tokens_per_call * (2 if generic_reask else 1)
    reserved_usage = {"in": reserved_input, "out": reserved_output}
    try:
        reservation_id = catalog.reserve_cost(
            book_id,
            phase=phase,
            model=model,
            input_tokens=reserved_input,
            output_tokens=reserved_output,
            usd=usd_of(model, reserved_usage),
            max_input_tokens=settings.cost_max_input_tokens_per_book,
            max_output_tokens=settings.cost_max_output_tokens_per_book,
            max_usd=settings.cost_max_usd_per_book,
            chapter_ordinal=chapter_ordinal,
        )
    except RuntimeError as exc:
        raise CostCeilingExceeded(str(exc)) from exc
    try:
        value, usage = _production_complete(
            client, system, user, tier=tier, schema=schema,
            max_output_tokens=settings.cost_max_output_tokens_per_call,
        )
    except Exception:
        catalog.settle_cost(book_id, reservation_id, phase=f"{phase}-failed-reserved")
        raise
    actual = {"in": int(usage.get("in", 0) or 0), "out": int(usage.get("out", 0) or 0)}
    if actual == {"in": 0, "out": 0} and getattr(client, "provider", None) != "stub":
        actual = reserved_usage                         # missing real-provider usage fails conservative
    actual_usd = usd_of(model, actual)
    try:
        catalog.note_reservation_actual(
            book_id, reservation_id, input_tokens=actual["in"], output_tokens=actual["out"],
            usd=actual_usd,
        )
    except ValueError as exc:
        raise RuntimeError("cost reservation disappeared while provider call was in flight") from exc
    if actual["in"] > reserved_input or actual["out"] > reserved_output:
        catalog.settle_cost(book_id, reservation_id, phase=f"{phase}-provider-limit-violation")
        raise CostCeilingExceeded("provider usage exceeded its reserved hard limit")
    if not defer_settlement:
        catalog.settle_cost(book_id, reservation_id)
    return BudgetedResult(value, actual, actual_usd, reservation_id)


def budgeted_embedding(catalog, settings, client, book_id, *, phase, texts, chapter_ordinal=None):
    """Reserve, execute, and settle one embedding batch through the same hard input ceiling."""
    estimated_input = sum(len((text or "").encode("utf-8")) for text in texts) + 32
    if estimated_input > settings.cost_max_input_tokens_per_call:
        raise CostCeilingExceeded(
            f"embedding input exceeds per-call ceiling "
            f"({estimated_input} > {settings.cost_max_input_tokens_per_call})"
        )
    model = client.embed_identity()
    try:
        reservation_id = catalog.reserve_cost(
            book_id,
            phase=phase,
            model=model,
            input_tokens=estimated_input,
            output_tokens=0,
            usd=usd_of(model, {"in": estimated_input, "out": 0}),
            max_input_tokens=settings.cost_max_input_tokens_per_book,
            max_output_tokens=settings.cost_max_output_tokens_per_book,
            max_usd=settings.cost_max_usd_per_book,
            chapter_ordinal=chapter_ordinal,
        )
    except RuntimeError as exc:
        raise CostCeilingExceeded(str(exc)) from exc
    try:
        vectors, usage = client.embed(texts)
    except Exception:
        catalog.settle_cost(book_id, reservation_id, phase=f"{phase}-failed-reserved")
        raise
    actual = {"in": int(usage.get("in", 0) or 0), "out": 0}
    if actual["in"] == 0 and not model.startswith("stub:"):
        actual = {"in": estimated_input, "out": 0}    # missing real-provider usage fails conservative
    actual_usd = usd_of(model, actual)
    try:
        catalog.note_reservation_actual(
            book_id, reservation_id, input_tokens=actual["in"], output_tokens=0, usd=actual_usd
        )
    except ValueError as exc:
        raise RuntimeError("cost reservation disappeared while embedding was in flight") from exc
    if actual["in"] > estimated_input:
        catalog.settle_cost(book_id, reservation_id, phase=f"{phase}-provider-limit-violation")
        raise CostCeilingExceeded("embedding provider usage exceeded its reserved hard limit")
    if actual["in"] == 0 and model.startswith("stub:"):
        catalog.discard_cost_reservation(book_id, reservation_id)
    else:
        catalog.settle_cost(book_id, reservation_id)
    return BudgetedResult(vectors, actual, actual_usd, reservation_id)
