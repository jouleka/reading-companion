"""Small, auditable primitives for OIDC and session tokens."""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import unquote, urlsplit


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def new_token() -> str:
    """Return 256 bits of entropy as an unpadded, URL-safe 43-character token."""
    return _b64url(secrets.token_bytes(32))


def digest_token(token: str) -> bytes:
    """Hash a high-entropy opaque token before persistence."""
    return hashlib.sha256(token.encode("ascii", errors="strict")).digest()


def pkce_challenge(verifier: str) -> str:
    """RFC 7636 S256 code challenge."""
    return _b64url(hashlib.sha256(verifier.encode("ascii", errors="strict")).digest())


def safe_return_to(candidate: str | None) -> str:
    """Accept only an application-relative path; reject scheme-relative and backslash tricks."""
    if not candidate or len(candidate) > 2048 or not candidate.startswith("/"):
        return "/"
    decoded = unquote(candidate)
    if (
        decoded.startswith("//")
        or "\\" in decoded
        or any(ord(ch) < 32 for ch in decoded)
    ):
        return "/"
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return "/"
    return candidate
