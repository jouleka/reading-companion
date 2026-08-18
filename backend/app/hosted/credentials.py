"""Envelope-encrypted, owner-bound hosted provider credentials (LIT-45)."""

from __future__ import annotations

import base64
import binascii
import argparse
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.hosted.tenant.models import OwnerId

ALGORITHM = "AES-256-GCM/AES-256-GCM"
_PROVIDER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_KEY_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_MAX_SECRET_BYTES = 16 * 1024
_WRAP_NONCE_BYTES = 12


class CredentialConfigurationError(RuntimeError):
    """Raised without secret-bearing values when the deployer keyring is invalid."""


class CredentialUnavailableError(RuntimeError):
    """A credential is missing, disabled, deleted, corrupt, or cannot be unwrapped."""


@dataclass(frozen=True, slots=True)
class EncryptedCredential:
    owner_id: uuid.UUID
    credential_id: uuid.UUID
    provider: str
    masked_label: str
    ciphertext: bytes = field(repr=False)
    encrypted_data_key: bytes = field(repr=False)
    encryption_algorithm: str
    key_version: str
    nonce: bytes = field(repr=False)


class ResolvedCredential:
    """Short-lived plaintext buffer with redacted repr and deterministic best-effort zeroing."""

    __slots__ = ("_value",)

    def __init__(self, value: bytes) -> None:
        self._value = bytearray(value)

    def __repr__(self) -> str:
        return "ResolvedCredential([REDACTED])"

    def get_secret_value(self) -> str:
        if not self._value:
            raise CredentialUnavailableError("credential plaintext is no longer available")
        return self._value.decode("utf-8")

    def close(self) -> None:
        for index in range(len(self._value)):
            self._value[index] = 0
        self._value.clear()

    def __enter__(self) -> ResolvedCredential:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@dataclass(slots=True)
class ResolvedProviderCredential:
    provider: str
    secret: ResolvedCredential = field(repr=False)

    def __repr__(self) -> str:
        return f"ResolvedProviderCredential(provider={self.provider!r}, secret=[REDACTED])"

    def get_secret_value(self) -> str:
        return self.secret.get_secret_value()

    def close(self) -> None:
        self.secret.close()

    def __enter__(self) -> ResolvedProviderCredential:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def normalize_provider(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("provider must be a string")
    provider = value.strip().casefold()
    if _PROVIDER_RE.fullmatch(provider) is None:
        raise ValueError("provider must use a bounded lowercase identifier")
    return provider


def _secret_bytes(value: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError("credential secret must be a string")
    encoded = value.encode("utf-8")
    if not encoded or len(encoded) > _MAX_SECRET_BYTES or value != value.strip():
        raise ValueError("credential secret must be non-empty, trimmed, and at most 16 KiB")
    if "\x00" in value or any(character in value for character in "\r\n"):
        raise ValueError("credential secret cannot contain control line breaks")
    return encoded


def masked_label(secret: str) -> str:
    encoded = _secret_bytes(secret)
    # A suffix is useful for replacement selection, while never returning enough to authenticate.
    suffix = encoded[-4:].decode("utf-8", errors="replace")
    return f"••••{suffix}"


def _aad(owner_id: uuid.UUID, credential_id: uuid.UUID, provider: str) -> bytes:
    return (
        f"litlet/provider-credential/v1\0{owner_id}\0{credential_id}\0{provider}"
    ).encode("ascii")


def _decode_key(value: str) -> bytes:
    try:
        key = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CredentialConfigurationError("credential master keys must be base64-encoded") from exc
    if len(key) != 32:
        raise CredentialConfigurationError("credential master keys must decode to exactly 32 bytes")
    return key


class CredentialCipher:
    """AES-GCM envelope encryption with tenant/resource AAD and versioned master keys."""

    def __init__(self, keys: Mapping[str, bytes], *, active_version: str) -> None:
        if _KEY_VERSION_RE.fullmatch(active_version) is None:
            raise CredentialConfigurationError("credential key version is invalid")
        copied: dict[str, bytes] = {}
        for version, key in keys.items():
            if _KEY_VERSION_RE.fullmatch(version) is None or len(key) != 32:
                raise CredentialConfigurationError("credential keyring is invalid")
            copied[version] = bytes(key)
        if active_version not in copied:
            raise CredentialConfigurationError("active credential key version is unavailable")
        self._keys = copied
        self.active_version = active_version

    def encrypt(
        self,
        owner_id: OwnerId,
        credential_id: uuid.UUID,
        provider: str,
        secret: str,
    ) -> EncryptedCredential:
        provider = normalize_provider(provider)
        plaintext = _secret_bytes(secret)
        data_key = bytearray(os.urandom(32))
        secret_nonce = os.urandom(12)
        wrap_nonce = os.urandom(_WRAP_NONCE_BYTES)
        aad = _aad(owner_id.value, credential_id, provider)
        try:
            ciphertext = AESGCM(bytes(data_key)).encrypt(secret_nonce, plaintext, aad + b"\0secret")
            wrapped = AESGCM(self._keys[self.active_version]).encrypt(
                wrap_nonce,
                bytes(data_key),
                aad + b"\0dek\0" + self.active_version.encode("ascii"),
            )
        finally:
            for index in range(len(data_key)):
                data_key[index] = 0
        return EncryptedCredential(
            owner_id=owner_id.value,
            credential_id=credential_id,
            provider=provider,
            masked_label=masked_label(secret),
            ciphertext=ciphertext,
            encrypted_data_key=wrap_nonce + wrapped,
            encryption_algorithm=ALGORITHM,
            key_version=self.active_version,
            nonce=secret_nonce,
        )

    def decrypt(self, record: EncryptedCredential) -> ResolvedCredential:
        if (
            record.encryption_algorithm != ALGORITHM
            or len(record.nonce) != 12
            or len(record.encrypted_data_key) != 60
        ):
            raise CredentialUnavailableError("credential envelope is invalid")
        key = self._keys.get(record.key_version)
        if key is None:
            raise CredentialUnavailableError("credential key version is unavailable")
        try:
            provider = normalize_provider(record.provider)
        except ValueError as exc:
            raise CredentialUnavailableError("credential envelope is invalid") from exc
        aad = _aad(record.owner_id, record.credential_id, provider)
        wrap_nonce = record.encrypted_data_key[:_WRAP_NONCE_BYTES]
        wrapped = record.encrypted_data_key[_WRAP_NONCE_BYTES:]
        data_key = bytearray()
        try:
            data_key.extend(
                AESGCM(key).decrypt(
                    wrap_nonce,
                    wrapped,
                    aad + b"\0dek\0" + record.key_version.encode("ascii"),
                )
            )
            plaintext = AESGCM(bytes(data_key)).decrypt(
                record.nonce, record.ciphertext, aad + b"\0secret"
            )
        except (InvalidTag, ValueError, UnicodeError) as exc:
            raise CredentialUnavailableError("credential envelope cannot be decrypted") from exc
        finally:
            for index in range(len(data_key)):
                data_key[index] = 0
        return ResolvedCredential(plaintext)

    def rewrap(self, record: EncryptedCredential) -> EncryptedCredential:
        """Rotate only the envelope key; provider plaintext is never materialized."""
        if record.key_version == self.active_version:
            return record
        old_key = self._keys.get(record.key_version)
        if old_key is None:
            raise CredentialUnavailableError("credential key version is unavailable")
        try:
            provider = normalize_provider(record.provider)
        except ValueError as exc:
            raise CredentialUnavailableError("credential envelope is invalid") from exc
        aad = _aad(record.owner_id, record.credential_id, provider)
        data_key = bytearray()
        try:
            data_key.extend(
                AESGCM(old_key).decrypt(
                    record.encrypted_data_key[:_WRAP_NONCE_BYTES],
                    record.encrypted_data_key[_WRAP_NONCE_BYTES:],
                    aad + b"\0dek\0" + record.key_version.encode("ascii"),
                )
            )
            nonce = os.urandom(_WRAP_NONCE_BYTES)
            wrapped = AESGCM(self._keys[self.active_version]).encrypt(
                nonce,
                bytes(data_key),
                aad + b"\0dek\0" + self.active_version.encode("ascii"),
            )
        except (InvalidTag, ValueError) as exc:
            raise CredentialUnavailableError("credential envelope cannot be rewrapped") from exc
        finally:
            for index in range(len(data_key)):
                data_key[index] = 0
        return EncryptedCredential(
            owner_id=record.owner_id,
            credential_id=record.credential_id,
            provider=record.provider,
            masked_label=record.masked_label,
            ciphertext=record.ciphertext,
            encrypted_data_key=nonce + wrapped,
            encryption_algorithm=record.encryption_algorithm,
            key_version=self.active_version,
            nonce=record.nonce,
        )


def build_credential_cipher(settings: object) -> CredentialCipher:
    active_secret = getattr(settings, "hosted_credential_master_key", None)
    active_version = getattr(settings, "hosted_credential_key_version", None)
    if active_secret is None or not active_version:
        raise CredentialConfigurationError(
            "hosted credentials require a master key and active key version"
        )
    keys = {active_version: _decode_key(active_secret.get_secret_value())}
    previous_secret = getattr(settings, "hosted_credential_previous_master_keys", None)
    if previous_secret is not None:
        try:
            previous = json.loads(previous_secret.get_secret_value())
        except (json.JSONDecodeError, TypeError) as exc:
            raise CredentialConfigurationError(
                "previous credential master keys must be a JSON object"
            ) from exc
        if not isinstance(previous, dict) or not all(
            isinstance(version, str) and isinstance(value, str)
            for version, value in previous.items()
        ):
            raise CredentialConfigurationError(
                "previous credential master keys must be a JSON object"
            )
        for version, value in previous.items():
            if version == active_version:
                raise CredentialConfigurationError("active credential key is duplicated")
            keys[version] = _decode_key(value)
    return CredentialCipher(keys, active_version=active_version)


def build_credential_cipher_from_environment(env: Mapping[str, str]) -> CredentialCipher:
    """Build the same keyring for the standalone worker without loading web/OIDC settings."""
    active = env.get("HOSTED_CREDENTIAL_MASTER_KEY")
    version = env.get("HOSTED_CREDENTIAL_KEY_VERSION")
    if not active or not version:
        raise CredentialConfigurationError(
            "hosted credentials require a master key and active key version"
        )
    keys = {version: _decode_key(active)}
    previous_value = env.get("HOSTED_CREDENTIAL_PREVIOUS_MASTER_KEYS")
    if previous_value:
        try:
            previous = json.loads(previous_value)
        except json.JSONDecodeError as exc:
            raise CredentialConfigurationError(
                "previous credential master keys must be a JSON object"
            ) from exc
        if not isinstance(previous, dict) or not all(
            isinstance(item_version, str) and isinstance(value, str)
            for item_version, value in previous.items()
        ):
            raise CredentialConfigurationError(
                "previous credential master keys must be a JSON object"
            )
        for item_version, value in previous.items():
            if item_version == version:
                raise CredentialConfigurationError("active credential key is duplicated")
            keys[item_version] = _decode_key(value)
    return CredentialCipher(keys, active_version=version)


def rewrap_credentials(dsn: str, cipher: CredentialCipher, *, batch_size: int = 100) -> int:
    """Rewrap live envelopes to the active master key without decrypting provider secrets."""
    if batch_size < 1 or batch_size > 1000:
        raise ValueError("credential rewrap batch size must be between 1 and 1000")
    import psycopg
    from psycopg.rows import dict_row

    changed = 0
    while True:
        with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.transaction():
            rows = conn.execute(
                """
                SELECT owner_id,id,provider,masked_label,ciphertext,encrypted_data_key,
                       encryption_algorithm,key_version,nonce
                FROM public.provider_credentials
                WHERE deleted_at IS NULL AND key_version<>%s
                ORDER BY owner_id,id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (cipher.active_version, batch_size),
            ).fetchall()
            if not rows:
                return changed
            for row in rows:
                current = EncryptedCredential(
                    owner_id=row["owner_id"],
                    credential_id=row["id"],
                    provider=row["provider"],
                    masked_label=row["masked_label"],
                    ciphertext=bytes(row["ciphertext"]),
                    encrypted_data_key=bytes(row["encrypted_data_key"]),
                    encryption_algorithm=row["encryption_algorithm"],
                    key_version=row["key_version"],
                    nonce=bytes(row["nonce"]),
                )
                rotated = cipher.rewrap(current)
                result = conn.execute(
                    """
                    UPDATE public.provider_credentials
                    SET encrypted_data_key=%s,key_version=%s
                    WHERE owner_id=%s AND id=%s AND key_version=%s AND deleted_at IS NULL
                    """,
                    (
                        rotated.encrypted_data_key,
                        rotated.key_version,
                        rotated.owner_id,
                        rotated.credential_id,
                        current.key_version,
                    ),
                )
                changed += result.rowcount


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rewrap hosted provider-credential envelopes to the active master key"
    )
    parser.add_argument("--dsn-env", default="DATABASE_URL")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args(argv)
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        parser.error(f"environment variable {args.dsn_env!r} is not set")
    cipher = build_credential_cipher_from_environment(os.environ)
    rewrap_credentials(dsn, cipher, batch_size=args.batch_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
