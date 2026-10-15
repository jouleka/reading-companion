"""FastAPI dependencies (ADR 0007 D-A11): the per-process singletons — Store (sole memory.db owner),
Catalog (the one shared writer), LLMClient — built once in the lifespan and read off ``app.state``.
Routes NEVER construct these; the Store's sole-owner guarantee rests on there being exactly one."""
import asyncio
import os
from copy import copy

from fastapi import Request

from app.catalog.catalog import Catalog
from app.config import Settings
from app.llm.client import LLMClient
from app.memory import migrations
from app.memory.store import Store


def build_state(settings: Settings) -> dict:
    """Construct the service singletons. RAISES (refusing startup) if the LLM resolves to the stub
    without ALLOW_STUB — the D-A7 fail-loud predicate lives in the client's default-deny."""
    os.makedirs(settings.data_dir, exist_ok=True)
    client = LLMClient(env=settings.llm_env(), allow_stub=settings.allow_stub)
    if settings.jev_enabled:
        from app.llm.jev import JevClient, JevUnavailable, windows_credential

        if client.provider == "stub":
            raise JevUnavailable("Jev requires a configured generative provider")
        key = (settings.typesafe_api_key.get_secret_value() if settings.typesafe_api_key else
               windows_credential() if settings.jev_windows_credential else None)
        if not key:
            raise JevUnavailable("Configure a server-side Jev credential")
        client.jev = JevClient(api_key=key, model=settings.jev_model,
                               timeout=settings.jev_timeout_seconds)
    reader = client
    if settings.reader_fast_mode:
        if (client.provider != "openai-compatible" or not client._is_native_openai
                or any(client._model_for(t) not in {"gpt-6-luna", "gpt-6.1-sol"}
                       for t in ("cheap", "large"))):
            raise ValueError("Reader Fast mode requires native OpenAI GPT-6 Luna or GPT-6.1 Sol")
        reader = copy(client)
        reader._oai = client._oai.with_options(max_retries=0)
        reader.service_tier = "fast"
    from app.lifecycle_cache import RecapRegistry
    reader.answers = RecapRegistry(max_entries=128, max_failures=1, max_flights=8)
    catalog = Catalog(os.path.join(settings.data_dir, "catalog.db"),
                      schema_version_default=migrations.CURRENT_VERSION)
    return {
        "settings": settings,
        "store": Store(
            data_dir=settings.data_dir,
            max_handles=settings.store_max_handles,
            vector_backend=settings.vector_backend,
            schema_version_callback=catalog.set_schema_version,
        ),
        "catalog": catalog,
        "client": client,
        "reader_client": reader,
    }


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_catalog(request: Request) -> Catalog:
    return request.app.state.catalog


def get_client(request: Request) -> LLMClient:
    return request.app.state.client


def get_reader_client(request: Request) -> LLMClient:
    return request.app.state.reader_client


def get_worker(request: Request):
    return request.app.state.worker


class _BookRequestLockEntry:
    """One event-loop-confined per-book request lock and all of its holders/waiters."""

    def __init__(self, lock=None, refcount=0):
        self.lock = lock or asyncio.Lock()
        self.refcount = refcount
        self.readers = 0
        self.readers_done = asyncio.Event()
        self.readers_done.set()


async def book_lifecycle(book_id: str, request: Request):
    """Keep mutations exclusive while allowing answers and recap preparation to share reads.

    The lock registry is created by the app lifespan and is intentionally scoped to that app's one
    event loop. Tests must acquire these locks on the TestClient lifespan loop rather than carrying
    them across clients/loops. Cancellation while queued is handled natively by ``asyncio.Lock``.
    """
    locks = request.app.state.book_request_locks
    entry = locks.get(book_id)
    if entry is None:
        entry = locks[book_id] = _BookRequestLockEntry()
    entry.refcount += 1
    try:
        path = getattr(getattr(request, "url", None), "path", "")
        shared_read = (getattr(request, "recap_preparation", False)
                       or (request.method == "POST" and path.endswith("/ask"))
                       or (request.method == "GET" and path.endswith("/catch-me-up")))
        if shared_read:
            async with entry.lock:
                entry.readers += 1
                entry.readers_done.clear()
            try:
                yield
            finally:
                entry.readers -= 1
                if entry.readers == 0:
                    entry.readers_done.set()
        else:
            # Holding the entry lock prevents new readers from passing a queued mutation.
            async with entry.lock:
                await entry.readers_done.wait()
                yield
    finally:
        entry.refcount -= 1
        if entry.refcount == 0 and locks.get(book_id) is entry:
            del locks[book_id]
