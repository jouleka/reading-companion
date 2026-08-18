"""Just-in-time hosted completion calls with atomic owner spend reservations."""

from __future__ import annotations

import asyncio
import uuid

from app.cost.limits import (
    BudgetedResult,
    CostCeilingExceeded,
    estimate_input_tokens,
    usd_of,
)
from app.hosted.tenant.models import OwnerId
from app.llm.client import LLMClient


def completion_client(setting: dict, secret: str) -> LLMClient:
    provider = setting["provider"]
    if provider == "offline":
        raise RuntimeError("offline provider settings cannot make a completion call")
    kwargs = {
        "provider": provider,
        "base_url": setting.get("base_url"),
        "cheap_model": setting["model"],
        "large_model": setting["model"],
        "max_retries": 2,
    }
    if provider == "anthropic":
        kwargs["anthropic_key"] = secret
    else:
        kwargs["api_key"] = secret
    return LLMClient(**kwargs)


def close_completion_client(client: LLMClient) -> None:
    for attribute in ("_oai", "_http"):
        handle = getattr(client, attribute, None)
        if handle is not None and hasattr(handle, "close"):
            handle.close()


async def budgeted_hosted_completion(
    repository,
    owner_id: OwnerId,
    book_id: uuid.UUID,
    settings,
    setting: dict,
    client: LLMClient,
    *,
    phase: str,
    system: str,
    user: str,
    schema=None,
    max_output_tokens: int = 1200,
) -> BudgetedResult:
    cap = min(max_output_tokens, settings.cost_max_output_tokens_per_call)
    estimated_input = estimate_input_tokens(system, user, schema)
    generic_reask = bool(
        schema is not None
        and setting["provider"] == "openai-compatible"
        and not getattr(client, "_is_native_openai", False)
    )
    peak_input = estimated_input + (cap + 512 if generic_reask else 0)
    if peak_input > settings.cost_max_input_tokens_per_call:
        raise CostCeilingExceeded("completion input exceeds the per-call ceiling")
    reserved_input = estimated_input * 2 + cap + 512 if generic_reask else estimated_input
    reserved_output = cap * (2 if generic_reask else 1)
    reserved_usage = {"in": reserved_input, "out": reserved_output}
    reserved_usd = usd_of(setting["model"], reserved_usage)
    reservation = await repository.reserve_provider_call(
        owner_id,
        book_id,
        phase=phase,
        provider=setting["provider"],
        model=setting["model"],
        reserved_input_tokens=reserved_input,
        reserved_output_tokens=reserved_output,
        reserved_usd=f"{reserved_usd:.10f}",
        idempotency_key=f"interactive:{uuid.uuid4()}",
        setting_id=uuid.UUID(setting["id"]),
        expected_setting_updated_at=setting["updated_at"],
        credential_id=uuid.UUID(setting["credential_id"]),
    )
    reservation_id = reservation["id"]
    try:
        value, usage = await asyncio.to_thread(
            client.complete,
            system,
            user,
            tier="large",
            schema=schema,
            max_output_tokens=cap,
        )
    except Exception:
        await repository.settle_provider_call(
            owner_id,
            reservation_id,
            input_tokens=reserved_input,
            output_tokens=reserved_output,
            usd=f"{reserved_usd:.10f}",
        )
        raise
    actual = {
        "in": int(usage.get("in", 0) or 0),
        "out": int(usage.get("out", 0) or 0),
    }
    if actual == {"in": 0, "out": 0}:
        actual = reserved_usage
    actual_usd = usd_of(setting["model"], actual)
    if actual["in"] > reserved_input or actual["out"] > reserved_output or actual_usd > reserved_usd:
        await repository.settle_provider_call(
            owner_id,
            reservation_id,
            input_tokens=reserved_input,
            output_tokens=reserved_output,
            usd=f"{reserved_usd:.10f}",
        )
        raise CostCeilingExceeded("provider usage exceeded its reserved hard limit")
    await repository.settle_provider_call(
        owner_id,
        reservation_id,
        input_tokens=actual["in"],
        output_tokens=actual["out"],
        usd=f"{actual_usd:.10f}",
    )
    return BudgetedResult(value, actual, actual_usd, str(reservation_id))
