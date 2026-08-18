"""FastAPI dependencies (ADR 0007 D-A11): the per-process singletons — Store (sole memory.db owner),
Catalog (the one shared writer), LLMClient — built once in the lifespan and read off ``app.state``.
Routes NEVER construct these; the Store's sole-owner guarantee rests on there being exactly one."""
import asyncio
import os

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
    }


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_catalog(request: Request) -> Catalog:
    return request.app.state.catalog


def get_client(request: Request) -> LLMClient:
    return request.app.state.client


def get_worker(request: Request):
    return request.app.state.worker


class _BookRequestLockEntry:
    """One event-loop-confined per-book request lock and all of its holders/waiters."""

    def __init__(self, lock=None, refcount=0):
        self.lock = lock or asyncio.Lock()
        self.refcount = refcount


async def book_lifecycle(book_id: str, request: Request):
    """Serialize one book's protected requests without occupying a worker thread.

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
        async with entry.lock:
            yield
    finally:
        entry.refcount -= 1
        if entry.refcount == 0 and locks.get(book_id) is entry:
            del locks[book_id]
