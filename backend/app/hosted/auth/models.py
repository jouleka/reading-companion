"""Typed values crossing the hosted authentication boundary."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class LoginAttempt:
    issuer: str
    code_verifier: str
    nonce: str
    return_to: str


@dataclass(frozen=True, slots=True)
class IdentityClaims:
    issuer: str
    subject: str
    display_name: str
    email: str | None
    email_verified: bool


@dataclass(frozen=True, slots=True)
class Principal:
    owner_id: uuid.UUID
    session_id: uuid.UUID
    display_name: str
    email: str | None
    csrf_digest: bytes
    expires_at: datetime
