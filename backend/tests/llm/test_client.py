"""LIT-20 — the ONE provider-agnostic LLM + embedding client (productionized from
spikes/lit-6-extraction/llm.py). Re-ported with named, behaviour-relevant changes (ADR 0007 D-A5):
Pydantic models are the schema source of truth; OpenAI uses the SDK's native strict structured-output
helper; the downstream contract stays dict-of-strings via model_dump(mode="json").

The real network is faked with httpx.MockTransport so the REAL openai SDK request-build + strict-schema
transform + response parse runs against canned bytes — only the wire is faked, not the adapter.
"""
import json
import math
from enum import Enum

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from app.llm.client import LLMClient, ProviderUnavailable


# --- a representative extraction schema (str-subclass enum + extra="forbid"), the shape the stub emits.
class EntityType(str, Enum):
    character = "character"
    place = "place"


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    canonical_name: str
    type: EntityType
    aliases: list[str]
    matched_roster: bool
    state: str | None


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chapter_summary: str
    entities: list[Entity]
    relationships: list[str]
    events: list[str]
    themes: list[str]


_VALID = {
    "chapter_summary": "Ivan met Alyosha.",
    "entities": [{"canonical_name": "Ivan", "type": "character", "aliases": [],
                  "matched_roster": False, "state": None}],
    "relationships": [],
    "events": [],
    "themes": [],
}


def _chat_response(model, content, *, prompt=10, completion=5):
    return httpx.Response(200, json={
        "id": "chatcmpl-x", "object": "chat.completion", "created": 0, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                  "total_tokens": prompt + completion},
    })


def _mock(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _stub():
    return LLMClient(provider="stub", allow_stub=True)


# ---------------------------------------------------------------- stub backend

def test_complete_text_returns_str_and_usage():
    obj, usage = _stub().complete("sys", "Ivan met Alyosha", tier="large")
    assert isinstance(obj, str)
    assert set(usage) >= {"in", "out"}


def test_complete_structured_returns_dict_obeying_contract():
    obj, usage = _stub().complete("sys", "Ivan met Alyosha in Skotoprigonyevsk",
                                  tier="cheap", schema=Extraction)
    assert isinstance(obj, dict)
    Extraction.model_validate(obj)                       # the dict round-trips through the schema
    for e in obj["entities"]:
        # dict-of-strings contract: enum dumped to a plain str (mode="json"), never an Enum member
        assert isinstance(e["type"], str) and not isinstance(e["type"], Enum)


def test_embed_returns_unit_vectors():
    vecs, usage = _stub().embed(["hello world", "foo"])
    assert len(vecs) == 2 and len(vecs[0]) == 256
    assert math.isclose(math.sqrt(sum(x * x for x in vecs[0])), 1.0, abs_tol=1e-9)


def test_stub_embeddings_preserve_unicode_text_as_embedding_material():
    vecs, _usage = _stub().embed(["Алёша вернулся", "阿廖沙回来了"])
    assert all(any(value != 0.0 for value in vector) for vector in vecs)
    assert vecs[0] != vecs[1]


def test_embed_identity_is_the_stub():
    assert _stub().embed_identity() == "stub:lexical-stub-256"


def test_version_surface():
    v = _stub().version
    assert set(v) == {"provider", "cheap", "large", "embed"}
    assert v["provider"] == "stub" and v["embed"] == "stub:lexical-stub-256"


def test_extractor_version():
    assert _stub().extractor_version() == "stub:stub-cheap"


def test_embed_canary_is_a_vector():
    can = _stub().embed_canary()
    assert isinstance(can, list) and len(can) == 256


def test_stub_default_denies_without_allow_stub():
    # ADR 0007 D-A7 / P2-14: a stub completion provider is silent garbage -> default-deny.
    with pytest.raises(RuntimeError):
        LLMClient(provider="stub", allow_stub=False)


def test_autodetect_resolves_to_stub_with_no_keys():
    c = LLMClient(env={}, allow_stub=True)
    assert c.provider == "stub"


# ---------------------------------------------------------- openai-compatible (native OpenAI)

def _openai(handler, **kw):
    return LLMClient(provider="openai-compatible", api_key="sk-test",
                     base_url="https://api.openai.com/v1", http_client=_mock(handler),
                     allow_stub=False, **kw)


def test_openai_native_structured_uses_strict_parse():
    seen = {}

    def handler(request):
        body = json.loads(request.content)
        seen["rf"] = body.get("response_format")
        return _chat_response(body["model"], json.dumps(_VALID))

    obj, usage = _openai(handler).complete("sys", "user", tier="cheap", schema=Extraction)
    # the SDK's native parse injects a strict json_schema response_format
    assert seen["rf"]["type"] == "json_schema"
    assert seen["rf"]["json_schema"]["strict"] is True
    assert seen["rf"]["json_schema"]["schema"]["additionalProperties"] is False
    assert obj["entities"][0]["canonical_name"] == "Ivan"
    assert isinstance(obj["entities"][0]["type"], str) and not isinstance(obj["entities"][0]["type"], Enum)
    assert usage["in"] == 10 and usage["out"] == 5


def test_openai_refusal_fails_closed():
    # a model refusal -> message.content null, .parsed None -> raise, never a fabricated empty extraction
    def handler(request):
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": "gpt-4o-mini",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                                                 "refusal": "I can't help with that"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    with pytest.raises(ValueError):
        _openai(handler).complete("sys", "user", tier="cheap", schema=Extraction)


def test_openai_text_completion():
    obj, usage = _openai(lambda r: _chat_response("gpt-4o", "a short recap")).complete(
        "sys", "user", tier="large")
    assert obj == "a short recap"
    assert usage["in"] == 10 and usage["out"] == 5


def test_openai_authentication_failure_is_classified_without_leaking_provider_details():
    def handler(request):
        return httpx.Response(401, json={"error": {
            "message": "Incorrect API key provided: sk-secret-material",
            "type": "invalid_request_error",
            "code": "invalid_api_key",
        }})

    with pytest.raises(ProviderUnavailable) as exc:
        _openai(handler, max_retries=0).complete("sys", "user", tier="large")

    assert exc.value.kind == "authentication"
    assert exc.value.service == "completion"
    assert "secret" not in str(exc.value).lower()
    assert "key" not in str(exc.value).lower()


def test_provider_probe_records_degraded_authentication_without_raising_or_spending_tokens():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(401, json={"error": {
            "message": "bad credential", "type": "invalid_request_error", "code": "invalid_api_key",
        }})

    client = _openai(handler, max_retries=0)
    assert client.probe() is False
    assert seen == [("GET", "/v1/models")]
    assert client.provider_status()["completion"] == {
        "status": "degraded", "reason": "authentication"
    }


def test_openai_completion_sends_the_output_ceiling():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return _chat_response("gpt-4o", "bounded")

    _openai(handler).complete("sys", "user", tier="large", max_output_tokens=321)
    assert seen["max_completion_tokens"] == 321


def test_openai_uses_the_tier_model():
    seen = {}

    def handler(request):
        seen["model"] = json.loads(request.content)["model"]
        return _chat_response(seen["model"], "x")

    _openai(handler, cheap_model="my-cheap", large_model="my-large").complete("s", "u", tier="cheap")
    assert seen["model"] == "my-cheap"


def _embed_client(handler):
    return LLMClient(provider="openai-compatible", api_key="sk", base_url="https://api.openai.com/v1",
                     embed_provider="openai-compatible", embed_key="sk",
                     embed_base_url="https://api.openai.com/v1", embed_http_client=_mock(handler),
                     allow_stub=False)


def test_openai_embeddings_distinct_vectors_and_usage():
    def handler(request):
        body = json.loads(request.content)
        # distinct vector per input so a text<->vector mix-up would be detectable
        data = [{"object": "embedding", "index": i, "embedding": [float(i), 0.5]}
                for i, _ in enumerate(body["input"])]
        return httpx.Response(200, json={"object": "list", "data": data, "model": body["model"],
                                         "usage": {"prompt_tokens": 3, "total_tokens": 7}})

    c = _embed_client(handler)
    vecs, usage = c.embed(["a", "b"])
    assert vecs == [[0.0, 0.5], [1.0, 0.5]]
    assert usage["in"] == 7
    # a real, configured embedder reports its FULL identity, not the stub
    assert c.embed_identity() == "openai-compatible@https://api.openai.com/v1:text-embedding-3-small"


def test_openai_embeddings_realign_out_of_order_data_by_index():
    # an OpenAI-compatible proxy returns data in REVERSED array order but with correct index fields;
    # embed() must realign so input[i] -> the vector whose index is i (else silent KNN corruption).
    def handler(request):
        body = json.loads(request.content)
        n = len(body["input"])
        data = [{"object": "embedding", "index": n - 1 - i, "embedding": [float(n - 1 - i)]}
                for i in range(n)]                        # wire order reversed vs index
        return httpx.Response(200, json={"object": "list", "data": data, "model": body["model"],
                                         "usage": {"prompt_tokens": 1, "total_tokens": 1}})

    vecs, _ = _embed_client(handler).embed(["first", "second", "third"])
    assert vecs == [[0.0], [1.0], [2.0]]                  # input i -> vector [i], not wire order


def test_configured_embedder_unreachable_falls_back_to_honest_stub(capsys):
    # embed_provider configured non-stub but NO reachable key -> lexical stub, HONEST identity (never
    # the configured name), warning fires exactly once. This is the "embed() never lies" integrity gate.
    c = LLMClient(provider="stub", allow_stub=True, embed_provider="openai-compatible",
                  embed_model="text-embedding-3-small", env={})
    v1, _ = c.embed(["a"])
    v2, _ = c.embed(["b"])
    assert len(v1[0]) == 256 and len(v2[0]) == 256
    assert c.embed_identity() == "stub:lexical-stub-256"          # NOT 'text-embedding-3-small'
    assert capsys.readouterr().err.count("FALLING BACK") == 1     # warn-once


def test_openai_embeddings_without_index_uses_wire_order():
    # a compatible proxy that OMITS `index` (-> SDK leaves it None) must NOT crash the realign-sort;
    # fall back to wire order rather than `None < None` (pass-2 regression).
    def handler(request):
        body = json.loads(request.content)
        data = [{"object": "embedding", "embedding": [float(i)]} for i, _ in enumerate(body["input"])]
        return httpx.Response(200, json={"object": "list", "data": data, "model": body["model"],
                                         "usage": {"prompt_tokens": 1, "total_tokens": 1}})

    vecs, _ = _embed_client(handler).embed(["a", "b", "c"])
    assert vecs == [[0.0], [1.0], [2.0]]


def test_real_completion_key_implies_real_same_endpoint_embedder():
    # a lone OPENAI_API_KEY must yield a REAL same-endpoint embedder, never a SILENT lexical stub
    # while completion runs on a real model (pass-2: embed_provider inference).
    c = LLMClient(env={"OPENAI_API_KEY": "sk-real"}, allow_stub=False)
    assert c.provider == "openai-compatible" and c.embed_provider == "openai-compatible"
    assert c.embed_identity() == "openai-compatible@https://api.openai.com/v1:text-embedding-3-small"


def test_foreign_embed_endpoint_does_not_inherit_openai_key():
    # OPENAI_API_KEY must NOT be sent to a DIFFERENT EMBED_BASE_URL host (D19 independence / key-leak
    # guard, pass-2). With no EMBED_API_KEY for that host, embeddings fall to the honest stub.
    c = LLMClient(env={"OPENAI_API_KEY": "sk-secret", "EMBED_BASE_URL": "https://other.example/v1"},
                  allow_stub=False)
    assert c.embed_key is None                                    # the OpenAI key was NOT routed there
    assert c.embed_identity() == "stub:lexical-stub-256"


# ----------------------------------------- generic openai-compatible (non-OpenAI base_url): backstop

def _generic(handler, **kw):
    return LLMClient(provider="openai-compatible", api_key="sk",
                     base_url="https://router.example/v1", http_client=_mock(handler),
                     allow_stub=False, **kw)


def test_generic_provider_sends_strict_schema_and_validates():
    seen = {}

    def handler(request):
        body = json.loads(request.content)
        seen["rf"] = body.get("response_format")
        return _chat_response(body["model"], json.dumps(_VALID))

    obj, _ = _generic(handler).complete("s", "u", tier="cheap", schema=Extraction)
    assert seen["rf"]["json_schema"]["schema"]["additionalProperties"] is False
    assert obj["chapter_summary"] == "Ivan met Alyosha."
    assert not isinstance(obj["entities"][0]["type"], Enum)


def test_generic_degrades_to_json_object_when_strict_rejected():
    # a provider that 400s on strict json_schema must DEGRADE to json_object (not escape the backstop)
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body["response_format"]["type"])
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": {
                "message": "response_format json_schema is not supported",
                "type": "invalid_request_error", "code": None}})
        # json_object path: the schema is injected into the prompt
        assert any("JSON Schema" in m["content"] for m in body["messages"] if m["role"] == "system")
        return _chat_response(body["model"], json.dumps(_VALID))

    obj, _ = _generic(handler).complete("s", "u", tier="cheap", schema=Extraction)
    assert calls == ["json_schema", "json_object"]         # tried strict, then degraded
    assert obj["chapter_summary"] == "Ivan met Alyosha."


def test_generic_invalid_output_reasks_with_correction_then_fails_closed():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body["messages"])
        bad = dict(_VALID, smuggled="future spoiler")     # violates extra="forbid"
        return _chat_response("m", json.dumps(bad))

    with pytest.raises(ValueError):
        _generic(handler).complete("s", "u", tier="cheap", schema=Extraction)
    assert len(calls) == 2                                 # initial attempt + exactly one re-ask
    # the re-ask is a real CORRECTION (grows the message list + adds a corrective user turn), not a
    # blind resend — an identical-resend regression would otherwise pass the call-count alone.
    assert len(calls[1]) > len(calls[0])
    assert any("valid JSON" in m["content"] for m in calls[1] if m["role"] == "user")


# ------------------------------------------------------------------------ anthropic (raw httpx)

def _anthropic(handler, **kw):
    return LLMClient(provider="anthropic", anthropic_key="ak", http_client=_mock(handler),
                     allow_stub=False, **kw)


def test_anthropic_structured_forces_a_strict_tool():
    seen = {}

    def handler(request):
        assert request.url.path == "/v1/messages"
        body = json.loads(request.content)
        seen["schema"] = body["tools"][0]["input_schema"]
        seen["choice"] = body["tool_choice"]
        return httpx.Response(200, json={
            "content": [{"type": "tool_use", "name": "emit", "input": _VALID}],
            "usage": {"input_tokens": 7, "output_tokens": 4}})

    obj, usage = _anthropic(handler).complete("sys", "user", tier="cheap", schema=Extraction)
    assert seen["schema"]["additionalProperties"] is False          # strict transform applied
    assert seen["choice"] == {"type": "tool", "name": "emit"}
    assert obj["entities"][0]["canonical_name"] == "Ivan"
    assert not isinstance(obj["entities"][0]["type"], Enum)
    assert usage["in"] == 7 and usage["out"] == 4


def test_anthropic_missing_tool_use_fails_closed():
    # a 200 with no tool_use block -> raise (never a fabricated empty extraction)
    def handler(request):
        return httpx.Response(200, json={"content": [{"type": "text", "text": "no tool here"}],
                                         "usage": {"input_tokens": 1, "output_tokens": 1}})

    with pytest.raises(ValueError):
        _anthropic(handler).complete("sys", "user", tier="cheap", schema=Extraction)


def test_anthropic_retries_on_503_then_succeeds():
    calls = {"n": 0}
    sleeps = []

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"type": "error",
                                             "error": {"type": "overloaded_error", "message": "busy"}})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}],
                                         "usage": {"input_tokens": 1, "output_tokens": 1}})

    c = LLMClient(provider="anthropic", anthropic_key="ak", http_client=_mock(handler),
                  allow_stub=False, max_retries=3, sleep=sleeps.append)
    obj, _ = c.complete("sys", "user", tier="large")
    assert obj == "ok"
    assert calls["n"] == 3 and len(sleeps) == 2           # two backoffs before the success


def test_anthropic_raises_after_exhausting_retries():
    def handler(request):
        return httpx.Response(500, json={"type": "error",
                                         "error": {"type": "api_error", "message": "boom"}})

    c = LLMClient(provider="anthropic", anthropic_key="ak", http_client=_mock(handler),
                  allow_stub=False, max_retries=2, sleep=lambda s: None)
    with pytest.raises(ProviderUnavailable) as exc:
        c.complete("sys", "user", tier="large")
    assert exc.value.kind == "service"


def test_anthropic_text_completion():
    def handler(request):
        return httpx.Response(200, json={"content": [{"type": "text", "text": "recap text"}],
                                         "usage": {"input_tokens": 1, "output_tokens": 2}})

    obj, usage = _anthropic(handler).complete("sys", "user", tier="large")
    assert obj == "recap text"
    assert usage["in"] == 1 and usage["out"] == 2


def test_anthropic_detected_from_key_takes_priority():
    c = LLMClient(env={"ANTHROPIC_API_KEY": "ak"}, allow_stub=False, http_client=_mock(
        lambda r: httpx.Response(200, json={"content": [{"type": "text", "text": "x"}],
                                            "usage": {"input_tokens": 1, "output_tokens": 1}})))
    assert c.provider == "anthropic"
