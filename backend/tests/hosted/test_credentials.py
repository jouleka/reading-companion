"""Envelope encryption, rotation, redaction, and leak regressions for LIT-45."""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import replace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.config import Settings
from app.hosted.credentials import (
    CredentialCipher,
    CredentialConfigurationError,
    CredentialUnavailableError,
    build_credential_cipher,
)
from app.hosted.tenant.models import OwnerId
from app.main import _CredentialBodyLimit


def _cipher(*, active: str = "v1") -> CredentialCipher:
    return CredentialCipher(
        {"v1": b"1" * 32, "v2": b"2" * 32}, active_version=active
    )


def test_envelope_round_trip_is_random_owner_bound_and_redacted() -> None:
    owner = OwnerId(uuid.uuid4())
    credential_id = uuid.uuid4()
    secret = "sk-private-canary-7H3k"
    first = _cipher().encrypt(owner, credential_id, "openai-compatible", secret)
    second = _cipher().encrypt(owner, credential_id, "openai-compatible", secret)

    assert first.ciphertext != second.ciphertext
    assert first.encrypted_data_key != second.encrypted_data_key
    assert secret.encode() not in first.ciphertext + first.encrypted_data_key
    assert secret not in repr(first)
    with _cipher().decrypt(first) as resolved:
        assert resolved.get_secret_value() == secret
        assert secret not in repr(resolved)
    with pytest.raises(CredentialUnavailableError):
        resolved.get_secret_value()

    wrong_owner = replace(first, owner_id=uuid.uuid4())
    with pytest.raises(CredentialUnavailableError, match="cannot be decrypted"):
        _cipher().decrypt(wrong_owner)


def test_master_key_rotation_rewraps_only_dek_and_preserves_secret_ciphertext() -> None:
    owner = OwnerId(uuid.uuid4())
    original = _cipher(active="v1").encrypt(owner, uuid.uuid4(), "anthropic", "secret-v1")
    rotated = _cipher(active="v2").rewrap(original)
    assert rotated.key_version == "v2"
    assert rotated.ciphertext == original.ciphertext
    assert rotated.nonce == original.nonce
    assert rotated.encrypted_data_key != original.encrypted_data_key
    with _cipher(active="v2").decrypt(rotated) as resolved:
        assert resolved.get_secret_value() == "secret-v1"


@pytest.mark.parametrize("value", ["", " leading", "trailing ", "line\nbreak", "x" * 16385])
def test_secret_policy_errors_never_include_the_submitted_value(value: str) -> None:
    with pytest.raises(ValueError) as raised:
        _cipher().encrypt(OwnerId(uuid.uuid4()), uuid.uuid4(), "anthropic", value)
    if value:
        assert value not in str(raised.value)


def test_settings_keyring_is_secret_and_fails_closed() -> None:
    active = base64.b64encode(b"a" * 32).decode("ascii")
    previous = base64.b64encode(b"b" * 32).decode("ascii")
    settings = Settings(
        _env_file=None,
        deployment_mode="hosted",
        hosted_auth_dsn="postgresql://auth@db/litlet",
        hosted_tenant_dsn="postgresql://tenant@db/litlet",
        oidc_issuer="https://idp.example",
        oidc_client_id="litlet",
        oidc_client_secret="oidc-secret",
        oidc_redirect_uri="https://reader.example/api/auth/callback",
        hosted_credential_master_key=active,
        hosted_credential_key_version="v2",
        hosted_credential_previous_master_keys=json.dumps({"v1": previous}),
    )
    cipher = build_credential_cipher(settings)
    assert cipher.active_version == "v2"
    assert active not in repr(settings) and previous not in repr(settings)

    broken = settings.model_copy(
        update={"hosted_credential_master_key": type(settings.hosted_credential_master_key)("bad")}
    )
    with pytest.raises(CredentialConfigurationError, match="base64"):
        build_credential_cipher(broken)


def test_credential_request_body_is_bounded_before_application_parsing() -> None:
    app = FastAPI()

    @app.put("/api/credentials/{credential_id}")
    async def consume(credential_id: uuid.UUID, request: Request):
        return {"size": len(await request.body()), "id": str(credential_id)}

    app.add_middleware(_CredentialBodyLimit, max_body_bytes=16)
    with TestClient(app) as client:
        credential_id = uuid.uuid4()
        accepted = client.put(
            f"/api/credentials/{credential_id}", content=b"x" * 16
        )
        rejected = client.put(
            f"/api/credentials/{credential_id}", content=b"private-canary-over-limit"
        )
    assert accepted.json()["size"] == 16
    assert rejected.status_code == 413
    assert "private-canary" not in rejected.text
