"""Bounded, tenant-scoped process runtime state for hosted mode (LIT-42)."""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator

from app.hosted.tenant.models import OwnerId


class ResourceKind(str, Enum):
    BOOK = "book"
    CREDENTIAL = "credential"
    LIBRARY = "library"
    PROVIDER_SETTINGS = "provider-settings"
    COSTS = "costs"
    JOB = "job"


class CacheNamespace(str, Enum):
    BOOK_METADATA = "book-metadata"


@dataclass(frozen=True, slots=True)
class TenantResourceKey:
    """A lock identity that cannot omit either tenant or resource identity."""

    owner_id: OwnerId
    kind: ResourceKind
    resource_id: uuid.UUID

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, OwnerId):
            raise TypeError("owner_id must be an OwnerId")
        if not isinstance(self.owner_id.value, uuid.UUID):
            raise TypeError("owner_id value must be a UUID")
        if not isinstance(self.kind, ResourceKind):
            raise TypeError("kind must be a ResourceKind")
        if not isinstance(self.resource_id, uuid.UUID):
            raise TypeError("resource_id must be a UUID")


@dataclass(frozen=True, slots=True)
class TenantCacheKey:
    """A cache identity with a closed namespace and explicit data generation."""

    resource: TenantResourceKey
    namespace: CacheNamespace
    generation: str

    def __post_init__(self) -> None:
        if not isinstance(self.resource, TenantResourceKey):
            raise TypeError("resource must be a TenantResourceKey")
        if not isinstance(self.namespace, CacheNamespace):
            raise TypeError("namespace must be a CacheNamespace")
        if not isinstance(self.generation, str) or not self.generation or len(self.generation) > 64:
            raise ValueError("generation must be a non-empty string of at most 64 characters")


class RuntimeClosedError(RuntimeError):
    """Raised when new runtime work is attempted during or after shutdown."""


@dataclass(slots=True)
class _LockEntry:
    lock: asyncio.Lock
    leases: int = 0


class HostedRuntimeRegistry:
    """Bounded LRU cache and per-tenant resource locks owned by one app lifespan."""

    def __init__(self, *, cache_max_entries: int, lock_max_entries: int) -> None:
        if cache_max_entries < 1 or lock_max_entries < 1:
            raise ValueError("runtime registry bounds must be positive")
        self._cache_max_entries = cache_max_entries
        self._lock_max_entries = lock_max_entries
        self._cache: OrderedDict[TenantCacheKey, object] = OrderedDict()
        self._locks: OrderedDict[TenantResourceKey, _LockEntry] = OrderedDict()
        self._guard = asyncio.Lock()
        self._drained = asyncio.Event()
        self._drained.set()
        self._state = "open"
        self._active_lock_leases = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._cache_evictions = 0
        self._lock_evictions = 0

    def _ensure_open(self) -> None:
        if self._state != "open":
            raise RuntimeClosedError("hosted runtime is shutting down")

    async def cache_get(self, key: TenantCacheKey) -> object | None:
        if not isinstance(key, TenantCacheKey):
            raise TypeError("cache key must be a TenantCacheKey")
        async with self._guard:
            self._ensure_open()
            if key not in self._cache:
                self._cache_misses += 1
                return None
            self._cache_hits += 1
            value = self._cache.pop(key)
            self._cache[key] = value
            return deepcopy(value)

    async def cache_set(self, key: TenantCacheKey, value: object) -> None:
        if not isinstance(key, TenantCacheKey):
            raise TypeError("cache key must be a TenantCacheKey")
        async with self._guard:
            self._ensure_open()
            self._cache.pop(key, None)
            self._cache[key] = deepcopy(value)
            while len(self._cache) > self._cache_max_entries:
                self._cache.popitem(last=False)
                self._cache_evictions += 1

    async def cache_invalidate_resource(self, resource: TenantResourceKey) -> int:
        if not isinstance(resource, TenantResourceKey):
            raise TypeError("resource must be a TenantResourceKey")
        async with self._guard:
            self._ensure_open()
            keys = [key for key in self._cache if key.resource == resource]
            for key in keys:
                del self._cache[key]
            return len(keys)

    @asynccontextmanager
    async def serialized(self, key: TenantResourceKey) -> AsyncIterator[None]:
        if not isinstance(key, TenantResourceKey):
            raise TypeError("lock key must be a TenantResourceKey")
        async with self._guard:
            self._ensure_open()
            entry = self._locks.pop(key, None)
            if entry is None:
                entry = _LockEntry(asyncio.Lock())
            self._locks[key] = entry
            entry.leases += 1
            self._active_lock_leases += 1
            self._drained.clear()
            self._trim_idle_locks()

        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            async with self._guard:
                entry.leases -= 1
                self._active_lock_leases -= 1
                self._trim_idle_locks()
                if self._active_lock_leases == 0:
                    if self._state == "closing":
                        self._state = "closed"
                        self._cache.clear()
                        self._locks.clear()
                    self._drained.set()

    def _trim_idle_locks(self) -> None:
        while len(self._locks) > self._lock_max_entries:
            idle_key = next((key for key, entry in self._locks.items() if entry.leases == 0), None)
            if idle_key is None:
                return
            del self._locks[idle_key]
            self._lock_evictions += 1

    async def close(self, *, timeout: float) -> bool:
        """Reject new work, clear content, and wait at most ``timeout`` for lock leases."""
        if timeout < 0:
            raise ValueError("shutdown timeout cannot be negative")
        async with self._guard:
            if self._state == "closed":
                return True
            self._state = "closing"
            self._cache.clear()
            if self._active_lock_leases == 0:
                self._state = "closed"
                self._locks.clear()
                self._drained.set()
                return True

        try:
            await asyncio.wait_for(self._drained.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    async def stats(self) -> dict[str, int | str]:
        """Return aggregate-only metrics; keys and cached content are never labels or values."""
        async with self._guard:
            return {
                "state": self._state,
                "cache_entries": len(self._cache),
                "lock_entries": len(self._locks),
                "active_lock_leases": self._active_lock_leases,
                "cache_hits": self._cache_hits,
                "cache_misses": self._cache_misses,
                "cache_evictions": self._cache_evictions,
                "lock_evictions": self._lock_evictions,
            }
