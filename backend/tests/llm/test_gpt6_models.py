"""GPT-6 Luna/Sol request and billing contracts, exercised through the SDK with a fake wire only."""
import asyncio
import json
from types import SimpleNamespace
import uuid

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from app.ask import AskDraft, AskVerdict
from app.ask_selection import SourceSelection
from app.catalog.catalog import Catalog
from app.cost import CostCeilingExceeded, budgeted_completion, estimate_input_tokens, pricing_known, usd_of
from app.cost.limits import completion_usage, reserved_usd_of
from app.hosted.provider_runtime import budgeted_hosted_completion
from app.hosted.tenant.models import OwnerId
from app.eval.spoiler_gate.judge import JudgeVerdict
from app.ingest.extraction.schema import Extraction
from app.ingest.extraction.validation import ExtractionVerdict
from app.llm.client import LLMClient, ProviderUnavailable
from app.llm import versioning


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supported: bool
    text: str


def _response(model, content, *, service_tier="default", cached=0, written=0):
    return httpx.Response(200, json={
        "id": "chatcmpl-offline", "object": "chat.completion", "created": 0, "model": model,
        "service_tier": service_tier,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                  "prompt_tokens_details": {"cached_tokens": cached, "cache_write_tokens": written},
                  "completion_tokens_details": {"reasoning_tokens": 12}},
    })


def _client(handler, model="gpt-6-luna", *, native=True):
    return LLMClient(
        provider="openai-compatible", api_key="synthetic-not-a-real-key", env={},
        base_url="https://api.openai.com/v1" if native else "https://compatible.invalid/v1",
        cheap_model=model, large_model=model, max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        embed_provider="stub", allow_stub=False,
    )


def _settings(**overrides):
    return SimpleNamespace(**{
        "cost_max_input_tokens_per_call": 10000, "cost_max_output_tokens_per_call": 1000,
        "cost_max_input_tokens_per_book": 100000, "cost_max_output_tokens_per_book": 10000,
        "cost_max_usd_per_book": 1.0, **overrides,
    })


@pytest.mark.parametrize("model,reasoning", [("gpt-6-luna", "medium"), ("gpt-6.1-sol", "low")])
@pytest.mark.parametrize("tier", ["cheap", "large"])
@pytest.mark.parametrize("schema", [None, Result])
def test_native_gpt6_uses_model_reasoning_standard_service_and_one_shared_output_cap(
    model, reasoning, tier, schema,
):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        value = json.dumps({"supported": True, "text": "Grounded."}) if schema else "Grounded."
        return _response(model, value)

    value, usage = _client(handler, model).complete(
        "Use only the supplied source.", "Evidence.", tier=tier, schema=schema, max_output_tokens=321,
    )
    [request] = requests
    assert request["model"] == model
    assert request["reasoning_effort"] == reasoning
    assert request["service_tier"] == "default"
    assert request["max_completion_tokens"] == 321
    assert "max_tokens" not in request and "tools" not in request
    if schema:
        assert request["response_format"]["type"] == "json_schema"
        assert request["response_format"]["json_schema"]["strict"] is True
        assert value == {"supported": True, "text": "Grounded."}
    else:
        assert value == "Grounded."
    assert usage == {"in": 100, "out": 20}, "reasoning is already part of completion_tokens"


def test_gpt6_direct_call_still_has_a_bounded_default_output():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return _response("gpt-6-luna", "Grounded.")

    _client(handler).complete("Source only.", "Evidence.")
    assert seen[0]["max_completion_tokens"] == 4096


@pytest.mark.parametrize("model,schema,reasoning", [
    ("gpt-6-luna", AskVerdict, "medium"),
    ("gpt-6-luna", JudgeVerdict, "medium"),
    ("gpt-6.1-sol", AskVerdict, "low"),
])
@pytest.mark.parametrize("tier", ["cheap", "large"])
@pytest.mark.parametrize("cap", [None, 2048, 4096])
def test_native_verdicts_keep_model_reasoning_and_the_existing_total_cap(
    model, schema, reasoning, tier, cap,
):
    requests = []
    verdict = {"references_future": False, "unsupported_claims": []}

    def handler(request):
        requests.append(json.loads(request.content))
        return _response(model, json.dumps(verdict))

    value, usage = _client(handler, model).complete(
        "Judge only the supplied evidence.", "Evidence.", tier=tier,
        schema=schema, max_output_tokens=cap,
    )
    [request] = requests
    assert request["reasoning_effort"] == reasoning
    assert request["service_tier"] == "default"
    assert request["max_completion_tokens"] == (4096 if cap is None else cap)
    assert "max_tokens" not in request and "tools" not in request
    assert request["response_format"]["json_schema"]["name"] == schema.__name__
    assert value == verdict
    assert usage == {"in": 100, "out": 20}, "reasoning must not be billed twice"


@pytest.mark.parametrize("schema,tier,value", [
    (SourceSelection, "cheap", {"source_ids": [1]}),
    (Extraction, "cheap", {"chapter_summary": "A quiet section.", "entities": [],
                           "relationships": [], "events": [], "themes": []}),
    (ExtractionVerdict, "large", {"references_future": False, "unsupported_claims": [],
                                 "field_audits": []}),
    (AskDraft, "large", {"insufficient_evidence": False,
                         "claims": [{"text": "An exact supported statement.", "citation_ids": [1]}]}),
])
def test_luna_selection_extraction_generation_and_memory_review_all_request_medium(schema, tier, value):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return _response("gpt-6-luna", json.dumps(value))

    result, usage = _client(handler).complete(
        "Offline wire-format fixture only.", "Source.", tier=tier, schema=schema, max_output_tokens=2048,
    )
    [request] = requests
    assert request["reasoning_effort"] == "medium"
    assert request["service_tier"] == "default"
    assert request["max_completion_tokens"] == 2048
    assert "max_tokens" not in request and "tools" not in request
    assert result == value and usage == {"in": 100, "out": 20}


@pytest.mark.parametrize("model,native", [
    ("gpt-6-luna", False), ("gpt-6.1-sol", False),
    ("gpt-6-luna-unknown-tier", True), ("gpt-5.4", True),
])
def test_ask_verdict_does_not_add_native_reasoning_options_to_uncertain_or_legacy_models(model, native):
    requests = []
    verdict = {"references_future": False, "unsupported_claims": []}

    def handler(request):
        requests.append(json.loads(request.content))
        return _response(model, json.dumps(verdict))

    value, usage = _client(handler, model, native=native).complete(
        "Judge only the supplied evidence.", "Evidence.", schema=AskVerdict, max_output_tokens=2048,
    )
    [request] = requests
    assert "reasoning_effort" not in request and "service_tier" not in request
    assert request["max_completion_tokens" if native else "max_tokens"] == 2048
    assert value == verdict and usage == {"in": 100, "out": 20}


@pytest.mark.parametrize("model,native", [("gpt-5.4", True), ("gpt-6-luna", False)])
def test_gpt6_specific_options_do_not_change_legacy_or_generic_requests(model, native):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return _response(model, "Grounded.", cached=40, written=30)

    _value, usage = _client(handler, model, native=native).complete(
        "Source only.", "Evidence.", max_output_tokens=321,
    )
    assert "reasoning_effort" not in requests[0]
    assert "service_tier" not in requests[0]
    assert requests[0]["max_completion_tokens" if native else "max_tokens"] == 321
    if not native:
        assert usage == {"in": 100, "out": 20}, "proxy metadata cannot establish native discounts"


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6.1-sol"])
def test_gpt6_prices_known_models_and_cache_subdivisions_without_double_counting_input(model):
    assert pricing_known(model) and pricing_known("openai-compatible:" + model)
    assert not pricing_known(model + "-unknown-tier")
    base_input, output, cached, written = (
        (0.10e-6, 0.50e-6, 0.01e-6, 0.125e-6) if model == "gpt-6-luna"
        else (2.00e-6, 10.0e-6, 0.10e-6, 2.50e-6)
    )
    usage = {"in": 100, "out": 20, "cache_read": 40, "cache_write": 30}
    assert usd_of(model, usage) == pytest.approx(30 * base_input + 40 * cached + 30 * written + 20 * output)
    assert reserved_usd_of(model, {"in": 100, "out": 1000}) == pytest.approx(100 * written + 1000 * output)
    assert usd_of(model, {"in": 272000, "out": 100}) == pytest.approx(272000 * base_input + 100 * output)
    assert usd_of(model, {"in": 272001, "out": 100}) == pytest.approx(272001 * base_input * 2 + 100 * output * 1.5)


@pytest.mark.parametrize("read,written", [(101, 0), (70, 40), (-1, 0), (True, 0), ("40", 0)])
def test_invalid_cache_subdivisions_cannot_discount_recorded_spend(read, written):
    usage = completion_usage({"in": 100, "out": 20, "cache_read": read, "cache_write": written})
    assert usage == {"in": 100, "out": 20, "cache_write": 100}
    assert usd_of("gpt-6-luna", usage) == pytest.approx(100 * 0.125e-6 + 20 * 0.5e-6)


def test_native_cache_usage_reserves_before_io_and_settles_exact_model_charge(tmp_path):
    catalog = Catalog(str(tmp_path / "catalog.db"))
    catalog.add_book("b", title="Offline billing fixture")
    settings = _settings()
    system, user = "Source only.", "Evidence. " * 200
    estimated = estimate_input_tokens(system, user, Result)

    def handler(request):
        [pending] = catalog.get_cost_reservations("b")
        assert pending["reserved_input_tokens"] == estimated
        assert pending["reserved_output_tokens"] == 1000
        assert pending["reserved_usd"] == pytest.approx(estimated * 0.125e-6 + 1000 * 0.5e-6)
        assert json.loads(request.content)["max_completion_tokens"] == 1000
        return _response("gpt-6-luna", json.dumps({"supported": True, "text": "Grounded."}),
                         cached=40, written=30)

    result = budgeted_completion(catalog, settings, _client(handler), "b", phase="synthesis",
                                 system=system, user=user, tier="large", schema=Result)
    assert result.usage == {"in": 100, "out": 20, "cache_read": 40, "cache_write": 30}
    expected = 30 * 0.10e-6 + 40 * 0.01e-6 + 30 * 0.125e-6 + 20 * 0.5e-6
    assert result.usd == pytest.approx(expected)
    [row] = catalog.get_costs("b")
    assert row["model"] == "gpt-6-luna" and row["usd"] == pytest.approx(expected)
    assert row["input_tokens"] == 100 and row["output_tokens"] == 20
    assert catalog.get_cost_reservations("b") == []


@pytest.mark.parametrize("cap", [2048, 4096])
def test_medium_ask_review_keeps_local_reserved_output_and_actual_reasoning_charge(tmp_path, cap):
    catalog = Catalog(str(tmp_path / "catalog.db"))
    catalog.add_book("b", title="Offline billing fixture")
    system, user = "Judge only the supplied evidence.", "Evidence. " * 200
    estimated = estimate_input_tokens(system, user, AskVerdict)
    calls = []

    def handler(request):
        [reservation] = catalog.get_cost_reservations("b")
        assert reservation["reserved_input_tokens"] == estimated
        assert reservation["reserved_output_tokens"] == cap
        assert reservation["reserved_usd"] == pytest.approx(estimated * 0.125e-6 + cap * 0.5e-6)
        body = json.loads(request.content)
        calls.append(body)
        assert body["reasoning_effort"] == "medium"
        assert body["max_completion_tokens"] == cap
        return _response("gpt-6-luna", json.dumps({"references_future": False, "unsupported_claims": []}))

    result = budgeted_completion(
        catalog, _settings(cost_max_output_tokens_per_call=cap), _client(handler), "b",
        phase="judge", system=system, user=user, tier="cheap", schema=AskVerdict,
    )
    assert len(calls) == 1
    assert result.usage == {"in": 100, "out": 20}
    expected = 100 * 0.10e-6 + 20 * 0.5e-6
    assert result.usd == pytest.approx(expected)
    [row] = catalog.get_costs("b")
    assert row["model"] == "gpt-6-luna" and row["phase"] == "judge"
    assert row["input_tokens"] == 100 and row["output_tokens"] == 20
    assert row["usd"] == pytest.approx(expected)
    assert catalog.get_cost_reservations("b") == []


def test_missing_native_usage_settles_the_full_worst_case_including_cache_write_premium(tmp_path):
    catalog = Catalog(str(tmp_path / "catalog.db"))
    catalog.add_book("b", title="Offline billing fixture")
    system, user = "Source only.", "Evidence. " * 200
    estimated = estimate_input_tokens(system, user)

    def handler(request):
        data = _response("gpt-6-luna", "Grounded.").json()
        del data["usage"]
        return httpx.Response(200, json=data)

    result = budgeted_completion(catalog, _settings(), _client(handler), "b", phase="synthesis",
                                 system=system, user=user, tier="large")
    assert result.usage == {"in": estimated, "out": 1000}
    assert result.usd == pytest.approx(estimated * 0.125e-6 + 1000 * 0.5e-6)
    [row] = catalog.get_costs("b")
    assert row["usd"] == pytest.approx(result.usd)
    assert catalog.get_cost_reservations("b") == []


def test_gpt6_spend_ceiling_still_refuses_before_sdk_io(tmp_path):
    catalog = Catalog(str(tmp_path / "catalog.db"))
    catalog.add_book("b", title="Offline billing fixture")

    def forbidden(request):
        raise AssertionError("book ceiling must deny the request before provider IO")

    with pytest.raises(CostCeilingExceeded):
        budgeted_completion(catalog, _settings(cost_max_usd_per_book=1e-10), _client(forbidden),
                            "b", phase="synthesis", system="Source only.", user="Evidence.", tier="large")
    assert catalog.get_costs("b") == []
    assert catalog.get_cost_reservations("b") == []


def test_cache_discount_never_weakens_the_shared_reasoning_and_output_token_cap(tmp_path):
    catalog = Catalog(str(tmp_path / "catalog.db"))
    catalog.add_book("b", title="Offline billing fixture")

    def handler(request):
        data = _response("gpt-6-luna", "Grounded.", cached=100).json()
        data["usage"]["completion_tokens"] = 1001
        return httpx.Response(200, json=data)

    with pytest.raises(CostCeilingExceeded, match="exceeded its reserved hard limit"):
        budgeted_completion(catalog, _settings(), _client(handler), "b", phase="synthesis",
                            system="Source only.", user="Evidence. " * 200, tier="large")
    [row] = catalog.get_costs("b")
    assert row["phase"] == "synthesis-provider-limit-violation"
    assert row["input_tokens"] == 100 and row["output_tokens"] == 1001
    assert catalog.get_cost_reservations("b") == []


@pytest.mark.parametrize("schema", [None, Result])
def test_unexpected_processing_tier_fails_closed_without_fallback_or_secret_leak(schema):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        content = json.dumps({"supported": True, "text": "Grounded."}) if schema else "Grounded."
        return _response("gpt-6-luna", content, service_tier="priority")

    client = _client(handler)
    with pytest.raises(ProviderUnavailable) as error:
        client.complete("Source.", "Evidence.", schema=schema, max_output_tokens=321)
    assert error.value.kind == "unsupported_service_tier"
    assert len(requests) == 1
    assert client.provider_status()["completion"] == {"status": "degraded", "reason": "unsupported_service_tier"}
    assert "synthetic-not-a-real-key" not in str(error.value)


def test_model_change_cannot_append_to_a_preexisting_extractor_pin():
    client = LLMClient(provider="stub", allow_stub=True, env={},
                       cheap_model="gpt-4o-mini", large_model="gpt-4o")
    pinned = versioning.current_identity(client)
    client.large_model = "gpt-6-luna"
    versioning.assert_compatible_config(pinned, client)  # stateless synthesis swap remains allowed
    client.cheap_model = "gpt-6-luna"
    with pytest.raises(versioning.ModelMigrationRequired) as error:
        versioning.assert_compatible_config(pinned, client)
    assert ("extractor_model", versioning.FORCE_RE_EXTRACT) in error.value.decisions
    assert "model_migration_required" == error.value.public_detail()["code"]
    assert pinned["extractor_model"] == "stub:gpt-4o-mini"


@pytest.mark.parametrize("schema,reasoning,requested_cap,settings_cap,effective_cap", [
    (Result, "medium", 777, 1000, 777),
    (JudgeVerdict, "medium", 777, 1000, 777),
    (AskVerdict, "medium", 2048, 4096, 2048),
    (AskVerdict, "medium", 4096, 2048, 2048),
])
def test_hosted_native_cache_usage_uses_same_reserve_and_actual_price(
    schema, reasoning, requested_cap, settings_cap, effective_cap,
):
    events = []
    reservation = uuid.uuid4()

    class Repository:
        async def reserve_provider_call(self, *_args, **kwargs):
            events.append(("reserve", kwargs))
            return {"id": reservation}

        async def settle_provider_call(self, *_args, **kwargs):
            events.append(("settle", kwargs))

    def handler(request):
        assert events[0][0] == "reserve"
        events.append(("provider", json.loads(request.content)))
        value = ({"references_future": False, "unsupported_claims": []}
                 if issubclass(schema, JudgeVerdict) else {"supported": True, "text": "Grounded."})
        return _response("gpt-6-luna", json.dumps(value),
                         cached=40, written=30)

    setting = {"id": str(uuid.uuid4()), "credential_id": str(uuid.uuid4()),
               "updated_at": "2026-10-15T12:00:00+00:00", "provider": "openai-compatible", "model": "gpt-6-luna"}
    result = asyncio.run(budgeted_hosted_completion(
        Repository(), OwnerId(uuid.uuid4()), uuid.uuid4(),
        _settings(cost_max_output_tokens_per_call=settings_cap), setting, _client(handler),
        phase="judge", system="Source only.", user="Evidence. " * 200, schema=schema,
        max_output_tokens=requested_cap,
    ))
    assert [event[0] for event in events] == ["reserve", "provider", "settle"]
    assert events[1][1]["max_completion_tokens"] == effective_cap
    assert events[1][1]["reasoning_effort"] == reasoning
    assert result.usage == {"in": 100, "out": 20, "cache_read": 40, "cache_write": 30}
    expected = 30 * 0.10e-6 + 40 * 0.01e-6 + 30 * 0.125e-6 + 20 * 0.5e-6
    assert result.usd == pytest.approx(expected)
    assert float(events[-1][1]["usd"]) == pytest.approx(expected)
    reserved = events[0][1]
    assert reserved["reserved_output_tokens"] == effective_cap
    assert float(reserved["reserved_usd"]) == pytest.approx(
        reserved["reserved_input_tokens"] * 0.125e-6 + effective_cap * 0.5e-6)
