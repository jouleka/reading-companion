"""Tenant-scoped hosted cache/lock/runtime lifecycle contract (LIT-42)."""

from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace

import pytest
from starlette.responses import Response

from app.config import Settings
from app.hosted.runtime import (
    CacheNamespace,
    HostedRuntimeRegistry,
    ResourceKind,
    RuntimeClosedError,
    TenantCacheKey,
    TenantResourceKey,
)
from app.hosted.tenant.api import get_book
from app.hosted.tenant.models import OwnerId
from app.main import create_app


def _resource(owner: uuid.UUID, resource: uuid.UUID) -> TenantResourceKey:
    return TenantResourceKey(OwnerId(owner), ResourceKind.BOOK, resource)


def test_cache_identity_cannot_cross_owners_and_lru_is_bounded() -> None:
    async def scenario():
        registry = HostedRuntimeRegistry(cache_max_entries=2, lock_max_entries=2)
        shared_book = uuid.uuid4()
        owner_a = uuid.uuid4()
        owner_b = uuid.uuid4()
        key_a = TenantCacheKey(
            _resource(owner_a, shared_book), CacheNamespace.BOOK_METADATA, "v1"
        )
        key_b = TenantCacheKey(
            _resource(owner_b, shared_book), CacheNamespace.BOOK_METADATA, "v1"
        )
        await registry.cache_set(key_a, {"title": "A private title"})
        assert await registry.cache_get(key_b) is None
        await registry.cache_set(key_b, {"title": "B private title"})
        assert await registry.cache_get(key_a) == {"title": "A private title"}

        key_c = TenantCacheKey(
            _resource(owner_a, uuid.uuid4()), CacheNamespace.BOOK_METADATA, "v1"
        )
        await registry.cache_set(key_c, {"title": "C"})
        assert await registry.cache_get(key_b) is None
        stats = await registry.stats()
        assert stats["cache_entries"] == 2
        assert "A private title" not in repr(stats)
        await registry.close(timeout=0.1)

    asyncio.run(scenario())


def test_same_owner_book_serializes_but_same_uuid_for_another_owner_does_not() -> None:
    async def scenario():
        registry = HostedRuntimeRegistry(cache_max_entries=2, lock_max_entries=2)
        book = uuid.uuid4()
        key_a = _resource(uuid.uuid4(), book)
        key_b = _resource(uuid.uuid4(), book)
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        same_entered = asyncio.Event()
        other_entered = asyncio.Event()

        async def first():
            async with registry.serialized(key_a):
                first_entered.set()
                await release_first.wait()

        async def same():
            await first_entered.wait()
            async with registry.serialized(key_a):
                same_entered.set()

        async def other():
            await first_entered.wait()
            async with registry.serialized(key_b):
                other_entered.set()

        tasks = [asyncio.create_task(item()) for item in (first, same, other)]
        await asyncio.wait_for(first_entered.wait(), 0.2)
        await asyncio.wait_for(other_entered.wait(), 0.2)
        assert not same_entered.is_set()
        release_first.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 0.2)
        assert same_entered.is_set()
        await registry.close(timeout=0.1)

    asyncio.run(scenario())


def test_lock_eviction_is_bounded_after_temporary_active_overflow() -> None:
    async def scenario():
        registry = HostedRuntimeRegistry(cache_max_entries=1, lock_max_entries=1)
        key_a = _resource(uuid.uuid4(), uuid.uuid4())
        key_b = _resource(uuid.uuid4(), uuid.uuid4())
        async with registry.serialized(key_a):
            async with registry.serialized(key_b):
                stats = await registry.stats()
                assert stats["lock_entries"] == 2
                assert stats["active_lock_leases"] == 2
        stats = await registry.stats()
        assert stats["lock_entries"] <= 1
        assert stats["lock_evictions"] >= 1
        await registry.close(timeout=0.1)

    asyncio.run(scenario())


def test_shutdown_is_bounded_rejects_new_work_and_drains_without_keys_in_metrics() -> None:
    async def scenario():
        registry = HostedRuntimeRegistry(cache_max_entries=2, lock_max_entries=2)
        owner = uuid.uuid4()
        book = uuid.uuid4()
        key = _resource(owner, book)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def active():
            async with registry.serialized(key):
                entered.set()
                await release.wait()

        task = asyncio.create_task(active())
        await entered.wait()
        started = time.monotonic()
        assert not await registry.close(timeout=0.01)
        assert time.monotonic() - started < 0.2
        with pytest.raises(RuntimeClosedError):
            async with registry.serialized(key):
                pass
        release.set()
        await task
        stats = await registry.stats()
        assert stats["state"] == "closed"
        rendered = repr(stats)
        assert str(owner) not in rendered and str(book) not in rendered

    asyncio.run(scenario())


def test_bare_or_malformed_keys_are_rejected() -> None:
    owner = OwnerId(uuid.uuid4())
    with pytest.raises(TypeError):
        TenantResourceKey(owner.value, ResourceKind.BOOK, uuid.uuid4())
    with pytest.raises(TypeError):
        TenantResourceKey(OwnerId("not-a-uuid"), ResourceKind.BOOK, uuid.uuid4())
    resource = TenantResourceKey(owner, ResourceKind.BOOK, uuid.uuid4())
    with pytest.raises(TypeError):
        TenantCacheKey(resource, "free-form-content", "v1")


def test_book_route_cache_remains_owner_scoped() -> None:
    class Repository:
        def __init__(self) -> None:
            self.calls: list[OwnerId] = []

        async def get_book(self, owner_id: OwnerId, book_id: uuid.UUID):
            self.calls.append(owner_id)
            return {"id": book_id, "title": f"private-{owner_id.value}"}

    async def scenario():
        registry = HostedRuntimeRegistry(cache_max_entries=2, lock_max_entries=2)
        repository = Repository()
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(tenant_repository=repository, hosted_runtime=registry)
            )
        )
        book = uuid.uuid4()
        owner_a = OwnerId(uuid.uuid4())
        owner_b = OwnerId(uuid.uuid4())

        first_a = await get_book(book, request, Response(), owner_a)
        second_a = await get_book(book, request, Response(), owner_a)
        first_b = await get_book(book, request, Response(), owner_b)

        assert first_a == second_a
        assert first_a["title"] != first_b["title"]
        assert repository.calls == [owner_a, owner_b]
        await registry.close(timeout=0.1)

    asyncio.run(scenario())


def test_hosted_app_lifespan_owns_and_closes_runtime_registry() -> None:
    class CheckedRepository:
        async def check_runtime_role(self) -> None:
            return None

    class RecordingRuntime(HostedRuntimeRegistry):
        close_timeout: float | None = None

        async def close(self, *, timeout: float) -> bool:
            self.close_timeout = timeout
            return await super().close(timeout=timeout)

    async def scenario():
        runtime = RecordingRuntime(cache_max_entries=2, lock_max_entries=2)
        storage = SimpleNamespace(closed=False)
        storage.close = lambda: setattr(storage, "closed", True)
        settings = Settings(
            _env_file=None,
            deployment_mode="hosted",
            hosted_auth_dsn="postgresql://unused.invalid/litlet",
            hosted_tenant_dsn="postgresql://unused.invalid/litlet",
            oidc_issuer="https://idp.example",
            oidc_client_id="litlet",
            oidc_client_secret="not-a-real-secret",
            oidc_redirect_uri="https://reader.example/api/auth/callback",
            hosted_credential_master_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            hosted_credential_key_version="test-v1",
            hosted_runtime_shutdown_timeout_seconds=0.25,
        )
        app = create_app(
            settings,
            auth_repository=CheckedRepository(),
            tenant_repository=CheckedRepository(),
            oidc_client=object(),
            hosted_runtime=runtime,
            object_storage=storage,
        )
        async with app.router.lifespan_context(app):
            assert app.state.hosted_runtime is runtime
            assert (await runtime.stats())["state"] == "open"

        assert runtime.close_timeout == 0.25
        assert (await runtime.stats())["state"] == "closed"
        assert storage.closed

    asyncio.run(scenario())


def test_shutdown_timeout_log_contains_only_aggregate_count(caplog) -> None:
    class CheckedRepository:
        async def check_runtime_role(self) -> None:
            return None

    async def scenario():
        runtime = HostedRuntimeRegistry(cache_max_entries=2, lock_max_entries=2)
        storage = SimpleNamespace(closed=False)
        storage.close = lambda: setattr(storage, "closed", True)
        settings = Settings(
            _env_file=None,
            deployment_mode="hosted",
            hosted_auth_dsn="postgresql://unused.invalid/litlet",
            hosted_tenant_dsn="postgresql://unused.invalid/litlet",
            oidc_issuer="https://idp.example",
            oidc_client_id="litlet",
            oidc_client_secret="not-a-real-secret",
            oidc_redirect_uri="https://reader.example/api/auth/callback",
            hosted_credential_master_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            hosted_credential_key_version="test-v1",
            hosted_runtime_shutdown_timeout_seconds=0.01,
        )
        app = create_app(
            settings,
            auth_repository=CheckedRepository(),
            tenant_repository=CheckedRepository(),
            oidc_client=object(),
            hosted_runtime=runtime,
            object_storage=storage,
        )
        owner = uuid.uuid4()
        book = uuid.uuid4()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def active():
            async with runtime.serialized(_resource(owner, book)):
                entered.set()
                await release.wait()

        async with app.router.lifespan_context(app):
            task = asyncio.create_task(active())
            await entered.wait()
        release.set()
        await task
        assert not storage.closed
        storage.close()
        return owner, book

    owner, book = asyncio.run(scenario())
    messages = [record.getMessage() for record in caplog.records if record.name == "app.main"]
    assert messages == ["hosted runtime shutdown timed out with 1 active operations"]
    assert str(owner) not in messages[0]
    assert str(book) not in messages[0]
    assert "not-a-real-secret" not in messages[0]
