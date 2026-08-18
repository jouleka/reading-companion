"""Provider/model policy and zero-token validation classifications for LIT-46."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from app.hosted.provider_settings import (
    ProviderSettingPolicyError,
    ProviderValidator,
    allowed_origins,
    default_settings_payload,
    validate_setting_policy,
)

ORIGINS = allowed_origins("https://api.openai.com,https://api.anthropic.com")


def test_recommendations_are_explicit_unpersisted_and_explain_offline_costs() -> None:
    payload = default_settings_payload()
    assert payload["recommendations_persisted"] is False
    assert set(payload["recommendations"]) == {"extraction", "synthesis", "embedding", "judge"}
    assert "offline" in payload["offline_behavior"].casefold()
    assert "billed" in payload["cost_ownership"].casefold()


def test_provider_policy_rejects_ssrf_cross_provider_and_implicit_offline() -> None:
    credential_id = uuid.uuid4()
    with pytest.raises(ProviderSettingPolicyError, match="approved"):
        validate_setting_policy(
            capability="extraction",
            provider="openai-compatible",
            model="gpt-4o-mini",
            credential_id=credential_id,
            base_url="https://127.0.0.1/latest/meta-data",
            origins=ORIGINS,
        )
    with pytest.raises(ProviderSettingPolicyError, match="embeddings"):
        validate_setting_policy(
            capability="embedding",
            provider="anthropic",
            model="claude-sonnet-4-6",
            credential_id=credential_id,
            base_url=None,
            origins=ORIGINS,
        )
    with pytest.raises(ProviderSettingPolicyError, match="offline model"):
        validate_setting_policy(
            capability="judge",
            provider="offline",
            model="stub",
            credential_id=None,
            base_url=None,
            origins=ORIGINS,
        )


def test_validation_distinguishes_credentials_models_network_and_success() -> None:
    seen_secrets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("authorization", "")
        seen_secrets.append(authorization)
        if request.url.host == "network.example":
            raise httpx.ConnectError("synthetic network failure", request=request)
        if authorization == "Bearer invalid-key":
            return httpx.Response(401, json={"error": "secret-bearing provider detail"})
        return httpx.Response(200, json={"data": [{"id": "available-model"}]})

    validator = ProviderValidator(transport=httpx.MockTransport(handler))

    def check(model: str, secret: str, base_url: str = "https://api.openai.com/v1"):
        return asyncio.run(
            validator.validate(
                {
                    "provider": "openai-compatible",
                    "model": model,
                    "base_url": base_url,
                },
                secret,
            )
        )

    assert check("available-model", "valid-key").code == "ok"
    assert check("available-model", "invalid-key").code == "invalid_credentials"
    assert check("missing-model", "valid-key").code == "unavailable_model"
    assert check("available-model", "valid-key", "https://network.example/v1").code == "network_error"
    rendered = repr(
        [
            check("available-model", "invalid-key"),
            check("missing-model", "valid-key"),
        ]
    )
    assert "invalid-key" not in rendered and "secret-bearing" not in rendered
    assert seen_secrets


def test_offline_validation_performs_no_network_request() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("offline validation must not perform network I/O")

    result = asyncio.run(
        ProviderValidator(transport=httpx.MockTransport(handler)).validate(
            {"provider": "offline", "model": "offline", "base_url": None}, None
        )
    )
    assert (result.status, result.code) == ("offline", "offline")
