"""Strongly typed identifiers crossing the hosted tenant boundary."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OwnerId:
    value: uuid.UUID


class MissingTenantResourceError(LookupError):
    pass


class StalePositionEpochError(RuntimeError):
    pass


class FuturePositionVersionError(RuntimeError):
    pass


class InvalidPositionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SourceObjectRecord:
    book_id: uuid.UUID
    book_incarnation: uuid.UUID
    object_id: uuid.UUID
    provider: str
    media_type: str
    byte_size: int
    sha256: str
    encryption: str
