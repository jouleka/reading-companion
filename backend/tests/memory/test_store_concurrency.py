"""Concurrency proof for the per-book Store (ADR 0007 D-A2). The whole spoiler-safety-under-concurrency
argument rests on: (1) same-book access is mutually exclusive (the per-book lock is held for the WHOLE
operation, so a writer's `_engaged=True` is never observed by a concurrent reader), and (2)
`check_same_thread=False` + the lock survive real multi-threaded contention without crash or lost writes.
"""
import threading
import time

import pytest

from app.memory.store import Store


def test_same_book_access_is_mutually_exclusive(tmp_path):
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")):
        pass  # create
    state = {"in": 0, "max": 0}
    state_lock = threading.Lock()   # guards the instrumentation counters ONLY (not the store)
    errors = []

    def worker():
        try:
            with store.book("b"):                     # acquires the per-book lock for the whole block
                with state_lock:
                    state["in"] += 1
                    state["max"] = max(state["max"], state["in"])
                time.sleep(0.005)                     # widen the window a non-serialized impl would lose on
                with state_lock:
                    state["in"] -= 1
        except Exception as e:                        # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert state["max"] == 1, f"same-book section entered concurrently (max={state['max']}) — lock failed"


def test_concurrent_writes_no_loss_or_crash(tmp_path):
    """check_same_thread=False + the per-book lock must let many threads write one book with no
    ProgrammingError / 'database is locked' and no lost writes."""
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")):
        pass
    errors = []

    def writer(i):
        try:
            with store.book("b") as mem:
                mem.add_chapter(f"b:c{i}.xhtml", revealed_at=i + 1, href=f"c{i}.xhtml", content_hash=f"h{i}")
                mem.add_entity(f"Char{i}", "character", revealed_at=i + 1)
        except Exception as e:                        # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    with store.book("b") as mem:
        names = {r["canonical_name"] for r in mem.view(100).characters()}
    assert names == {f"Char{i}" for i in range(20)}, "concurrent writes were lost"


def test_different_books_use_independent_locks(tmp_path):
    """Reads/writes on DIFFERENT books are not serialized against each other (per-file isolation)."""
    store = Store(data_dir=str(tmp_path))
    for bid in ("a", "b"):
        with store.book(bid, meta=dict(title=bid)):
            pass
    assert store._lock_for("a") is not store._lock_for("b")


def test_escaped_handle_use_after_block_raises(tmp_path):
    """ADR 0007 D-A2: a view/handle retained PAST its store.book() block must fail LOUD (off-session),
    never silently read the sole connection off-lock."""
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")) as mem:
        mem.add_entity("X", "character", revealed_at=1)
        escaped_view = mem.view(1)
        escaped_handle = mem
    with pytest.raises(RuntimeError):
        escaped_view.characters()                       # off-session read fails loud
    with pytest.raises(RuntimeError):
        escaped_handle.view(1).characters()


def test_escaped_view_off_lock_raises_not_hangs(tmp_path):
    """Reproduces the reviewer's deadlock scenario: an escaped view read on thread R concurrent with an
    in-session write on thread W. The _active_owner guard makes R fail LOUD before touching the
    connection, so neither thread hangs (vs the connection-mutex deadlock without the guard)."""
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")) as mem:
        mem.add_entity("seed", "character", revealed_at=1)
    with store.book("b") as mem:
        escaped = mem.view(1)                            # created in-session, used off-session below
    errors = {}

    def writer():
        try:
            with store.book("b") as m:
                for i in range(50):
                    m.add_entity(f"E{i}", "character", revealed_at=2)
        except Exception as e:                           # pragma: no cover
            errors["w"] = repr(e)

    def reader():
        try:
            for _ in range(50):
                escaped.characters()                     # off-lock -> must raise, not block
        except RuntimeError:
            errors["r"] = "raised"

    tw, tr = threading.Thread(target=writer), threading.Thread(target=reader)
    tw.start()
    tr.start()
    tw.join(timeout=15)
    tr.join(timeout=15)
    assert not tw.is_alive() and not tr.is_alive(), "deadlock: a thread did not finish"
    assert errors.get("r") == "raised", "escaped off-lock read should fail loud"
    assert "w" not in errors, errors


def test_handle_lru_is_bounded_and_reopens_evicted_books(tmp_path):
    store = Store(data_dir=str(tmp_path), max_handles=2)
    for book_id in ("a", "b"):
        with store.book(book_id, meta={"title": book_id}) as mem:
            mem.add_entity(book_id.upper(), "character", revealed_at=1)

    with store.book("a"):
        pass  # make a most-recently used; b is now the eviction candidate
    with store.book("c", meta={"title": "c"}):
        pass

    assert list(store._handles) == ["a", "c"]
    assert len(store._locks) == 2
    with store.book("b") as reopened:
        assert [row["canonical_name"] for row in reopened.view(1).characters()] == ["B"]
    assert len(store._handles) == 2
    assert len(store._locks) == 2
    assert store.stats() == {"handles": 2, "handle_limit": 2, "leases": 0}


def test_active_handle_is_never_evicted_and_cache_converges_after_release(tmp_path):
    store = Store(data_dir=str(tmp_path), max_handles=1)
    with store.book("a", meta={"title": "a"}) as active:
        active.add_entity("A", "character", revealed_at=1)
        with store.book("b", meta={"title": "b"}):
            pass
        # The cache may transiently exceed its target while every candidate is leased, but a remains
        # usable and owned for the entire session. Releasing b evicts an idle handle, never active a.
        assert active.view(1).characters()[0]["canonical_name"] == "A"
        assert "a" in store._handles
    assert len(store._handles) == 1
    assert len(store._locks) == 1


def test_explicit_evict_waits_for_an_active_session(tmp_path):
    store = Store(data_dir=str(tmp_path), max_handles=2)
    entered = threading.Event()
    release = threading.Event()
    evicted = threading.Event()

    def holder():
        with store.book("a", meta={"title": "a"}) as mem:
            mem.add_entity("A", "character", revealed_at=1)
            entered.set()
            release.wait(timeout=5)

    def evicter():
        store.evict("a")
        evicted.set()

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=evicter)
    t1.start()
    assert entered.wait(timeout=5)
    t2.start()
    assert not evicted.wait(timeout=0.05), "eviction closed a leased handle"
    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert evicted.is_set()
    assert "a" not in store._handles and "a" not in store._locks
