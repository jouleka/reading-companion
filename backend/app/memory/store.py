"""Per-book connection manager — the SOLE owner of each book's memory.db connection (ADR 0007 D-A2).

The DAL's per-connection `_engaged` authorizer flag is only safe while a single caller holds it, so
the Store serializes ALL access to one book under a per-book `threading.Lock` held for the WHOLE
operation: `with store.book(book_id) as mem: ...` acquires the lock, yields the (cached, sole-owned)
MemoryDB, and releases on exit. Reads on different books run concurrently; same-book ops serialize.
The connection is opened `check_same_thread=False` (so it can move between threadpool threads) which
makes the per-book lock — not sqlite3's thread guard — the load-bearing serializer.

Eviction (LIT-33): handles are an access-ordered bounded LRU. A registry entry's reference count covers
both active holders and blocked waiters, so eviction can close only an idle handle. The cache may
transiently exceed its target when every candidate is leased; it converges on session release without
ever closing a live connection. The registry lock still makes lookup-or-open single-owner.
"""
import os
import threading
from collections import OrderedDict
from contextlib import contextmanager

from . import dal


class _LockEntry:
    def __init__(self):
        self.lock = threading.Lock()
        self.refcount = 0


class Store:
    def __init__(self, data_dir: str, trace: bool = False, max_handles: int = 16,
                 schema_version_callback=None, vector_backend: str = "vec0"):
        if isinstance(max_handles, bool) or not isinstance(max_handles, int) or max_handles < 1:
            raise ValueError("max_handles must be a positive integer")
        if vector_backend not in {"vec0", "bruteforce"}:
            raise ValueError("vector_backend must be 'vec0' or 'bruteforce'")
        self._data_dir = data_dir
        self._trace = trace
        self._max_handles = max_handles
        self._schema_version_callback = schema_version_callback
        self._vector_backend = vector_backend
        self._handles: OrderedDict[str, dal.MemoryDB] = OrderedDict()
        self._locks: dict[str, _LockEntry] = {}
        self._registry_lock = threading.Lock()   # guards lock/handle lookup-or-open (no double-open)

    def _path(self, book_id: str) -> str:
        return os.path.join(self._data_dir, "books", book_id, "memory.db")

    def _lock_for(self, book_id: str) -> threading.Lock:
        with self._registry_lock:
            entry = self._locks.get(book_id)
            if entry is None:
                entry = self._locks[book_id] = _LockEntry()
            return entry.lock

    def _lease(self, book_id: str) -> _LockEntry:
        with self._registry_lock:
            entry = self._locks.get(book_id)
            if entry is None:
                entry = self._locks[book_id] = _LockEntry()
            entry.refcount += 1
            return entry

    def _evict_idle_locked(self):
        while len(self._handles) > self._max_handles:
            victim = next(
                (book_id for book_id in self._handles
                 if self._locks[book_id].refcount == 0),
                None,
            )
            if victim is None:
                return
            mem = self._handles.pop(victim)
            self._locks.pop(victim, None)
            mem.close()

    def _release(self, book_id: str, entry: _LockEntry):
        with self._registry_lock:
            entry.refcount -= 1
            if entry.refcount < 0:  # pragma: no cover - internal invariant
                raise RuntimeError("negative Store lease count")
            self._evict_idle_locked()
            if entry.refcount == 0 and book_id not in self._handles:
                if self._locks.get(book_id) is entry:
                    del self._locks[book_id]

    def _handle(self, book_id: str, meta: dict | None, create: bool) -> dal.MemoryDB:
        # Guarded so two concurrent misses for one book resolve to exactly ONE MemoryDB instance
        # (one connection per book — the authorizer's whole guarantee rests on it).
        with self._registry_lock:
            mem = self._handles.get(book_id)
            if mem is None:
                path = self._path(book_id)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                mem = dal.MemoryDB(path, book_id, meta=meta, create=create, trace=self._trace,
                                   vector_backend=self._vector_backend)
                if self._schema_version_callback is not None:
                    try:
                        self._schema_version_callback(book_id, dal.SCHEMA_VERSION)
                    except Exception:
                        mem.close()
                        raise
                self._handles[book_id] = mem
            self._handles.move_to_end(book_id)
            self._evict_idle_locked()
            return mem

    @contextmanager
    def book(self, book_id: str, meta: dict | None = None):
        """Yield the sole-owned MemoryDB for `book_id` with the per-book lock held for the whole block.
        `meta` (title/author/source...) is used only when creating the book on first open. The handle's
        `_active_owner` is pinned to this thread for the block and cleared on exit, so an escaped
        view/handle used off-lock (a different thread, or after the block) fails LOUD rather than racing
        the sole connection (ADR 0007 D-A2). Do NOT retain the yielded handle/view past the block, and
        do NOT nest `book()` on the same book in one thread (the per-book lock is non-reentrant — it
        would deadlock; nested same-book sessions are not a supported access pattern)."""
        entry = self._lease(book_id)
        try:
            with entry.lock:
                mem = self._handle(book_id, meta, create=True)
                mem._active_owner = threading.get_ident()
                try:
                    yield mem
                finally:
                    mem._active_owner = None
        finally:
            self._release(book_id, entry)

    def evict(self, book_id: str) -> None:
        """Close one book handle after all holders/waiters ahead of this eviction have left."""
        entry = self._lease(book_id)
        try:
            with entry.lock:
                with self._registry_lock:
                    mem = self._handles.pop(book_id, None)
                if mem is not None:
                    mem.close()
        finally:
            self._release(book_id, entry)

    def close(self) -> None:
        """Flush/evict all currently-open book connections (NOT a terminal close — a later book() call
        re-opens a fresh handle on a cache miss). Acquires each per-book lock first so it serializes
        against any in-flight session (no use-after-close)."""
        with self._registry_lock:
            book_ids = list(self._handles)
        for book_id in book_ids:
            self.evict(book_id)

    def stats(self):
        """Small in-process operational snapshot; does not expose paths or book contents."""
        with self._registry_lock:
            return {
                "handles": len(self._handles),
                "handle_limit": self._max_handles,
                "leases": sum(entry.refcount for entry in self._locks.values()),
            }

    # --- diagnostics / tests -------------------------------------------------
    def _open_raw_as(self, book_id: str, claimed_book_id: str) -> dal.MemoryDB:
        """Open the existing file for `book_id` under a DIFFERENT claimed id — must raise ValueError
        (the no-silent-fail-open-to-empty identity guard). For tests/diagnostics only."""
        return dal.MemoryDB(self._path(book_id), claimed_book_id, create=False, trace=self._trace,
                            vector_backend=self._vector_backend)
