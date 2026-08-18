"""Owner-selected hosted provider/model policy and zero-token compatibility checks (LIT-46)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx

Capability = Literal["extraction", "synthesis", "embedding", "judge"]
Provider = Literal["openai-compatible", "anthropic", "offline"]

CAPABILITIES: tuple[Capability, ...] = ("extraction", "synthesis", "embedding", "judge")
PROVIDERS: tuple[Provider, ...] = ("openai-compatible", "anthropic", "offline")
DEFAULTS: dict[str, dict[str, str | None]] = {
    "extraction": {
        "provider": "openai-compatible",
        "model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1",
    },
    "synthesis": {
        "provider": "openai-compatible",
        "model": "gpt-4o",
        "base_url": "https://api.openai.com/v1",
    },
    "embedding": {
        "provider": "openai-compatible",
        "model": "text-embedding-3-small",
        "base_url": "https://api.openai.com/v1",
    },
    "judge": {
        "provider": "openai-compatible",
        "model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1",
    },
}
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")


class ProviderSettingPolicyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ValidationResult:
    status: Literal["ready", "offline", "invalid"]
    code: Literal[
        "ok", "offline", "invalid_credentials", "unavailable_model", "network_error", "service_error"
    ]


def default_settings_payload() -> dict:
    return {
        "capabilities": list(CAPABILITIES),
        "providers": list(PROVIDERS),
        "recommendations": {key: dict(value) for key, value in DEFAULTS.items()},
        "recommendations_persisted": False,
        "offline_behavior": (
            "Without a validated provider, new AI processing stays offline; books and already-built "
            "Codex memory remain available."
        ),
        "cost_ownership": (
            "Provider usage is billed to the account behind the selected credential; the service "
            "does not absorb those provider charges."
        ),
    }


def normalize_model(value: str) -> str:
    if not isinstance(value, str):
        raise ProviderSettingPolicyError("model must be a string")
    model = value.strip()
    if _MODEL_RE.fullmatch(model) is None:
        raise ProviderSettingPolicyError("model must use a bounded provider identifier")
    return model


def allowed_origins(value: str) -> frozenset[str]:
    origins: set[str] = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        parsed = urlsplit(item)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (parsed.path not in {"", "/"})
        ):
            raise ProviderSettingPolicyError("provider allow-list must contain HTTPS origins")
        origins.add(urlunsplit(("https", parsed.netloc.casefold(), "", "", "")))
    if not origins:
        raise ProviderSettingPolicyError("provider allow-list cannot be empty")
    return frozenset(origins)


def normalize_base_url(provider: Provider, value: str | None, origins: frozenset[str]) -> str | None:
    if provider == "offline":
        if value not in {None, ""}:
            raise ProviderSettingPolicyError("offline settings cannot use a base URL")
        return None
    default = "https://api.anthropic.com/v1" if provider == "anthropic" else "https://api.openai.com/v1"
    candidate = (value or default).strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderSettingPolicyError("provider base URL must be an approved HTTPS URL")
    origin = urlunsplit(("https", parsed.netloc.casefold(), "", "", ""))
    if origin not in origins:
        raise ProviderSettingPolicyError("provider base URL origin is not approved")
    return urlunsplit(("https", parsed.netloc.casefold(), parsed.path.rstrip("/"), "", ""))


def validate_setting_policy(
    *,
    capability: str,
    provider: str,
    model: str,
    credential_id: object,
    base_url: str | None,
    origins: frozenset[str],
) -> tuple[Capability, Provider, str, str | None]:
    if capability not in CAPABILITIES:
        raise ProviderSettingPolicyError("unsupported provider capability")
    if provider not in PROVIDERS:
        raise ProviderSettingPolicyError("unsupported provider")
    typed_capability: Capability = capability  # type: ignore[assignment]
    typed_provider: Provider = provider  # type: ignore[assignment]
    if typed_provider == "anthropic" and typed_capability == "embedding":
        raise ProviderSettingPolicyError("Anthropic does not provide embeddings")
    if typed_provider == "offline":
        if credential_id is not None:
            raise ProviderSettingPolicyError("offline settings cannot select a credential")
        if model != "offline":
            raise ProviderSettingPolicyError("offline settings must use the offline model marker")
        return typed_capability, typed_provider, "offline", normalize_base_url(
            typed_provider, base_url, origins
        )
    if credential_id is None:
        raise ProviderSettingPolicyError("a provider credential is required")
    return (
        typed_capability,
        typed_provider,
        normalize_model(model),
        normalize_base_url(typed_provider, base_url, origins),
    )


class ProviderValidator:
    """Zero-token model-list checks with fixed, secret-free outcome classes."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._transport = transport

    async def validate(self, setting: Mapping[str, object], secret: str | None) -> ValidationResult:
        provider = setting["provider"]
        if provider == "offline":
            return ValidationResult("offline", "offline")
        if not secret:
            return ValidationResult("invalid", "invalid_credentials")
        base_url = str(setting["base_url"]).rstrip("/")
        model = str(setting["model"])
        if provider == "anthropic":
            url = f"{base_url}/models/{model}"
            headers = {
                "x-api-key": secret,
                "anthropic-version": "2023-06-01",
            }
        else:
            url = f"{base_url}/models"
            headers = {"Authorization": f"Bearer {secret}"}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.get(url, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError):
            return ValidationResult("invalid", "network_error")
        if response.status_code in {401, 403}:
            return ValidationResult("invalid", "invalid_credentials")
        if response.status_code == 404:
            return ValidationResult("invalid", "unavailable_model")
        if response.status_code < 200 or response.status_code >= 300:
            return ValidationResult("invalid", "service_error")
        if provider == "openai-compatible":
            try:
                payload = response.json()
                models = {
                    item.get("id")
                    for item in payload.get("data", [])
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                }
            except (ValueError, AttributeError):
                return ValidationResult("invalid", "service_error")
            if model not in models:
                return ValidationResult("invalid", "unavailable_model")
        return ValidationResult("ready", "ok")
