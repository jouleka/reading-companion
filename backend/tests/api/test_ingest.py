"""Module E / the ingestion worker + status route (ADR 0007 D-A3 + LIT-7): PUT /position
validates/ingests through the bookmark; the worker uses Module C with the LLM OUTSIDE the
per-book lock, gated on segmentation flags + the manifest cross-check; append-once makes a re-run a
no-op. Tests run the worker on an INLINE executor (deterministic, same-thread) with the stub client."""
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _epub import epub_ncx, three_chapter_book
from app.config import Settings
from app.ingest.extraction.chapter_text import content_hash_of
from app.ingest.worker import _MemoEmbed
from app.main import create_app
from app.memory import migrations


class InlineExecutor:
    """Deterministic same-thread executor for tests (a submitted task runs to completion inline)."""

    def submit(self, fn, *a, **kw):
        fn(*a, **kw)

    def shutdown(self, wait=True):
        pass


@pytest.fixture
def env(tmp_path):
    settings = Settings(_env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"))
    app = create_app(settings, ingest_executor=InlineExecutor())
    with TestClient(app) as c:
        bid = c.post("/api/books",
                     files={"file": ("b.epub", three_chapter_book(), "application/epub+zip")}
                     ).json()["book_id"]
        yield c, settings, bid, app


def test_sealed_embedding_memo_fails_closed_on_an_unexpected_lock_phase_miss(env):
    _, _, _, app = env
    memo = _MemoEmbed(app.state.client)
    expected = memo(["warmed"])
    memo.seal()
    assert memo(["warmed"]) == expected
    with pytest.raises(RuntimeError, match="cache miss"):
        memo(["not warmed"])


def test_segmentation_cache_is_lru_bounded_by_incarnation(tmp_path):
    settings = Settings(
        _env_file=None,
        allow_stub=True,
        data_dir=str(tmp_path / "data"),
        segmentation_cache_max_entries=1,
    )
    app = create_app(settings, ingest_executor=InlineExecutor())
    second = epub_ncx([
        ("x.xhtml", "Chapter I", "Chapter I", "Daria crossed the bridge. " * 12),
        ("y.xhtml", "Chapter II", "Chapter II", "Emil followed Daria. " * 12),
    ])
    with TestClient(app) as c:
        first_id = c.post(
            "/api/books",
            files={"file": ("first.epub", three_chapter_book(), "application/epub+zip")},
        ).json()["book_id"]
        second_id = c.post(
            "/api/books", files={"file": ("second.epub", second, "application/epub+zip")}
        ).json()["book_id"]
        first_inc = app.state.catalog.get_book(first_id)["incarnation"]
        second_inc = app.state.catalog.get_book(second_id)["incarnation"]
        app.state.worker._segmented(first_id, first_inc)
        app.state.worker._segmented(second_id, second_inc)
        assert list(app.state.worker._segcache) == [(second_id, second_inc)]
        assert app.state.worker.segmentation_cache_size() == 1


def _mlen(settings, bid, i):
    with open(os.path.join(settings.data_dir, "books", bid, "atoms.json"), encoding="utf-8") as f:
        return json.load(f)["atoms"][i]["char_len"]


def test_interrupted_extraction_reservation_blocks_repaying_until_reconciled(env):
    c, settings, bid, app = env
    catalog = app.state.catalog
    catalog.reserve_cost(
        bid, phase="extraction", model="m", input_tokens=10, output_tokens=2, usd=0.01,
        max_input_tokens=100_000, max_output_tokens=100_000, max_usd=5,
        chapter_ordinal=1,
    )
    client, original = app.state.client, app.state.client.complete
    called = []

    def should_not_run(*args, **kwargs):
        called.append(1)
        return original(*args, **kwargs)

    client.complete = should_not_run
    try:
        c.put(
            f"/api/books/{bid}/position",
            json={"cfi": "x", "offset": _mlen(settings, bid, 0) + 5},
        )
    finally:
        client.complete = original
    status = app.state.worker.status(bid)
    assert status["status"] == "error" and "outstanding extraction cost reservations" in status["error"]
    assert called == []


def test_new_book_catalog_schema_matches_current_memory_schema(env):
    _, _, bid, app = env
    assert app.state.catalog.get_book(bid)["schema_version"] == migrations.CURRENT_VERSION


def test_completing_a_chapter_triggers_its_ingestion(env):
    c, settings, bid, _ = env
    ch1 = _mlen(settings, bid, 0)
    c.put(f"/api/books/{bid}/position", json={"cfi": "x", "offset": ch1 + 5})
    st = c.get(f"/api/books/{bid}/ingest").json()
    assert st["ingest_progress"] == 1 and st["status"] in ("idle", "done")
    # the chapter's facts are in the store, bookmark-bounded (ch2/3 NOT ingested yet)
    pos = c.get(f"/api/books/{bid}/position").json()
    assert pos["ingest_progress"] == 1


def test_ingestion_is_bookmark_bounded(env):
    # only chapters <= the bookmark are ingested — nothing runs ahead of the reader
    c, settings, bid, _ = env
    ch1 = _mlen(settings, bid, 0)
    c.put(f"/api/books/{bid}/position", json={"cfi": "x", "offset": ch1 + 5})
    assert c.get(f"/api/books/{bid}/ingest").json()["ingest_progress"] == 1   # not 2, not 3


def test_full_read_ingests_everything_and_a_rerun_is_a_noop(env):
    c, settings, bid, app = env
    total = sum(_mlen(settings, bid, i) for i in range(3))
    c.put(f"/api/books/{bid}/position", json={"cfi": "x", "offset": total})
    assert c.get(f"/api/books/{bid}/ingest").json()["ingest_progress"] == 3
    # crash-resume/idempotency (LIT-7 basics): re-enqueue everything -> append-once skips, no dupes
    with app.state.store.book(bid) as mem:
        n_entities = len([r for r in mem._audit_all("entities") if r["retracted_at"] is None])
    c.put(f"/api/books/{bid}/position", json={"cfi": "x", "offset": total})
    with app.state.store.book(bid) as mem:
        again = len([r for r in mem._audit_all("entities") if r["retracted_at"] is None])
    assert again == n_entities


def test_new_pass_reuses_durable_receipts_without_provider_calls_or_new_cost(env):
    c, settings, bid, app = env
    total = sum(_mlen(settings, bid, i) for i in range(3))
    first = c.put(
        f"/api/books/{bid}/position",
        json={"cfi": "first-pass", "offset": total, "position_epoch": 0},
    )
    assert first.status_code == 200
    costs_before = list(app.state.catalog.get_costs(bid))
    with app.state.store.book(bid) as mem:
        receipts_before = list(mem._audit_all("ingested_chapters"))

    calls = []
    client = app.state.client
    original_complete, original_embed = client.complete, client.embed

    def unexpected_complete(*args, **kwargs):
        calls.append("complete")
        return original_complete(*args, **kwargs)

    def unexpected_embed(*args, **kwargs):
        calls.append("embed")
        return original_embed(*args, **kwargs)

    client.complete, client.embed = unexpected_complete, unexpected_embed
    try:
        reset = c.post(
            f"/api/books/{bid}/position/reset", json={"position_epoch": 0}
        )
        assert reset.status_code == 200
        assert reset.json()["ingest_progress"] == 3
        reread = c.put(
            f"/api/books/{bid}/position",
            json={"cfi": "second-pass", "offset": total, "position_epoch": 1},
        )
        assert reread.status_code == 200
    finally:
        client.complete, client.embed = original_complete, original_embed

    with app.state.store.book(bid) as mem:
        assert list(mem._audit_all("ingested_chapters")) == receipts_before
    assert app.state.catalog.get_costs(bid) == costs_before
    assert calls == []


def test_delete_reimport_reconciles_receipts_despite_process_cache(env):
    c, settings, bid, app = env
    ch1 = _mlen(settings, bid, 0)
    total = sum(_mlen(settings, bid, i) for i in range(3))
    source = (Path(settings.data_dir) / "books" / bid / "source.epub").read_bytes()
    c.put(f"/api/books/{bid}/position", json={"cfi": "before-delete", "offset": total})
    assert app.state.catalog.get_state(bid)["ingest_progress"] == 3

    assert c.delete(f"/api/books/{bid}").status_code == 204
    reimported = c.post(
        "/api/books",
        files={"file": ("b.epub", source, "application/epub+zip")},
    )
    assert reimported.status_code == 201 and reimported.json()["book_id"] == bid
    assert app.state.catalog.get_state(bid)["ingest_progress"] == 0
    fresh_status = c.get(f"/api/books/{bid}/ingest").json()
    assert fresh_status["status"] == "idle"
    assert fresh_status["error"] is None and fresh_status["flags"] == []

    c.put(f"/api/books/{bid}/position", json={"cfi": "after-reimport", "offset": ch1 + 5})
    assert app.state.catalog.get_state(bid)["ingest_progress"] == 1
    assert len(app.state.catalog.get_costs(bid)) == 1


def test_inflight_old_incarnation_cannot_process_past_the_new_target(env):
    import threading

    c, settings, bid, app = env
    source = (Path(settings.data_dir) / "books" / bid / "source.epub").read_bytes()
    total = sum(_mlen(settings, bid, i) for i in range(3))
    started, release = threading.Event(), threading.Event()
    client, original_complete = app.state.client, app.state.client.complete

    class Threaded:
        def submit(self, fn, *args):
            threading.Thread(target=fn, args=args, daemon=True).start()

        def shutdown(self, wait=True):
            pass

    def blocked_complete(*args, **kwargs):
        started.set()
        release.wait(5)
        return original_complete(*args, **kwargs)

    app.state.worker._executor = Threaded()
    client.complete = blocked_complete
    try:
        c.put(f"/api/books/{bid}/position", json={"cfi": "old", "offset": total})
        assert started.wait(5)
        assert c.delete(f"/api/books/{bid}").status_code == 204
        reimported = c.post(
            "/api/books",
            files={"file": ("b.epub", source, "application/epub+zip")},
        )
        assert reimported.status_code == 201 and reimported.json()["book_id"] == bid
        c.put(
            f"/api/books/{bid}/position",
            json={"cfi": "new", "offset": _mlen(settings, bid, 0) + 5},
        )
        release.set()
        for _ in range(100):
            if app.state.worker.status(bid)["status"] == "done":
                break
            threading.Event().wait(0.05)
    finally:
        release.set()
        client.complete = original_complete

    final_status = app.state.worker.status(bid)
    assert final_status["status"] == "done", final_status
    assert app.state.catalog.get_state(bid)["ingest_progress"] == 1
    assert app.state.worker.validated_frontier(bid) == 1


def test_enqueue_catalog_observation_is_ordered_with_delete_and_reimport(env, monkeypatch):
    """An enqueue that has observed an incarnation must publish its state before deletion can cross
    the lifecycle boundary; it must never resume later and replace the re-import's newer state."""
    import threading

    c, settings, bid, app = env
    source = (Path(settings.data_dir) / "books" / bid / "source.epub").read_bytes()
    observed, release = threading.Event(), threading.Event()
    enqueue_thread_id = {"value": None}
    original_get_book = app.state.catalog.get_book

    class Delayed:
        def __init__(self):
            self.tasks = []

        def submit(self, fn, *args):
            self.tasks.append((fn, args))

        def shutdown(self, wait=True):
            pass

    def paused_get_book(book_id):
        book = original_get_book(book_id)
        if threading.get_ident() == enqueue_thread_id["value"] and not observed.is_set():
            observed.set()
            release.wait(5)
        return book

    app.state.worker._executor = Delayed()
    monkeypatch.setattr(app.state.catalog, "get_book", paused_get_book)

    def old_enqueue():
        enqueue_thread_id["value"] = threading.get_ident()
        app.state.worker.enqueue(bid, 3)

    old = threading.Thread(target=old_enqueue, daemon=True)
    old.start()
    assert observed.wait(5)

    deleted = {}
    deleting = threading.Thread(
        target=lambda: deleted.setdefault("response", c.delete(f"/api/books/{bid}")), daemon=True
    )
    deleting.start()
    threading.Event().wait(0.1)
    assert deleting.is_alive(), "delete crossed an enqueue that had observed the old incarnation"

    release.set()
    old.join(5)
    deleting.join(5)
    assert deleted["response"].status_code == 204
    reimported = c.post(
        "/api/books", files={"file": ("b.epub", source, "application/epub+zip")}
    )
    assert reimported.status_code == 201 and reimported.json()["book_id"] == bid
    app.state.worker.enqueue(bid, 1)

    current = original_get_book(bid)["incarnation"]
    assert app.state.worker._state[bid]["incarnation"] == current
    assert app.state.worker._state[bid]["target"] == 1


@pytest.mark.parametrize("pause_point", ["before_set_position", "before_enqueue"])
def test_position_update_is_atomic_with_delete_reimport(env, monkeypatch, pause_point):
    """A position request belongs wholly to the incarnation that it validated.

    Exercise both route race windows: before the catalog write, and after that write has committed but
    before enqueue observes the catalog. Deletion must not cross either window, so the stale request
    can neither mutate nor enqueue work against the replacement incarnation.
    """
    import threading

    c, settings, bid, app = env
    source = (Path(settings.data_dir) / "books" / bid / "source.epub").read_bytes()
    total = sum(_mlen(settings, bid, i) for i in range(3))
    paused, release = threading.Event(), threading.Event()
    delete_probe_armed, delete_crossed = threading.Event(), threading.Event()
    original_get_book = app.state.catalog.get_book
    original_set_position = app.state.catalog.set_position
    original_enqueue = app.state.worker.enqueue

    class Delayed:
        def __init__(self):
            self.tasks = []

        def submit(self, fn, *args):
            self.tasks.append((fn, args))

        def shutdown(self, wait=True):
            pass

    def probed_get_book(book_id):
        if delete_probe_armed.is_set():
            delete_crossed.set()
        return original_get_book(book_id)

    def paused_set_position(book_id, cfi, bookmark, *, expected_epoch=0):
        if pause_point == "before_set_position":
            paused.set()
            assert release.wait(5)
        return original_set_position(book_id, cfi, bookmark, expected_epoch=expected_epoch)

    def paused_enqueue(book_id, target):
        if pause_point == "before_enqueue":
            state = app.state.catalog.get_state(book_id)
            assert state["cfi"] == "stale" and state["bookmark"] == 3
            paused.set()
            assert release.wait(5)
        return original_enqueue(book_id, target)

    app.state.worker._executor = Delayed()
    monkeypatch.setattr(app.state.catalog, "get_book", probed_get_book)
    monkeypatch.setattr(app.state.catalog, "set_position", paused_set_position)
    monkeypatch.setattr(app.state.worker, "enqueue", paused_enqueue)

    old_result = {}

    def stale_position():
        old_result["response"] = c.put(
            f"/api/books/{bid}/position", json={"cfi": "stale", "offset": total}
        )

    old = threading.Thread(target=stale_position, daemon=True)
    old.start()
    assert paused.wait(5)
    delete_probe_armed.set()

    replacement = {}

    def replace():
        replacement["delete"] = c.delete(f"/api/books/{bid}")
        replacement["import"] = c.post(
            "/api/books", files={"file": ("b.epub", source, "application/epub+zip")}
        )

    replacing = threading.Thread(target=replace, daemon=True)
    replacing.start()
    crossed = delete_crossed.wait(0.2)
    if crossed:
        # Keep a RED failure from stranding the two request threads during fixture shutdown.
        release.set()
        old.join(5)
        replacing.join(5)
    assert not crossed, "delete crossed the paused position lifecycle lease"
    assert replacing.is_alive()

    release.set()
    old.join(5)
    replacing.join(5)
    assert old_result["response"].status_code == 200
    assert replacement["delete"].status_code == 204
    assert replacement["import"].status_code == 201
    assert replacement["import"].json()["book_id"] == bid
    state = app.state.catalog.get_state(bid)
    assert (state["bookmark"], state["cfi"], state["ingest_progress"]) == (0, None, 0)
    assert app.state.catalog.get_costs(bid) == []
    assert app.state.worker.status(bid) == {"status": "idle", "error": None, "flags": []}


@pytest.mark.parametrize(
    ("path", "assert_old"),
    [
        ("graph", lambda body: body["characters"]),
        ("position", lambda body: body["bookmark"] == 3 and body["ingest_progress"] == 3),
        ("ingest", lambda body: body["ingest_progress"] == 3 and body["status"] == "done"),
    ],
    ids=["shared-view-graph", "position", "ingest-status"],
)
def test_get_request_lifecycle_blocks_delete_reimport_after_catalog_read(
    env, monkeypatch, path, assert_old
):
    """A GET that acquired old catalog state must finish against that incarnation.

    Graph represents every companion view: all view routes share the router-level lifecycle dependency.
    Position and ingest status exercise their separate route dependencies.  Pause immediately after the
    old state read, race deletion, and require the replacement to retain no old durable or catalog data.
    """
    import threading

    c, settings, bid, app = env
    source = (Path(settings.data_dir) / "books" / bid / "source.epub").read_bytes()
    total = sum(_mlen(settings, bid, i) for i in range(3))
    assert c.put(
        f"/api/books/{bid}/position", json={"cfi": "old-incarnation", "offset": total}
    ).status_code == 200
    assert app.state.catalog.get_costs(bid), "old incarnation must carry ingestion cost"

    acquired, release = threading.Event(), threading.Event()
    pause_armed = threading.Event()
    delete_entered, delete_crossed = threading.Event(), threading.Event()
    original_get_state = app.state.catalog.get_state
    original_remove_book = app.state.catalog.remove_book

    def paused_get_state(book_id):
        state = original_get_state(book_id)
        if pause_armed.is_set() and not acquired.is_set():
            acquired.set()
            assert release.wait(5)
        return state

    def probed_remove_book(book_id):
        delete_entered.set()
        result = original_remove_book(book_id)
        delete_crossed.set()
        return result

    monkeypatch.setattr(app.state.catalog, "get_state", paused_get_state)
    monkeypatch.setattr(app.state.catalog, "remove_book", probed_remove_book)
    old_result = {}

    def old_get():
        old_result["response"] = c.get(f"/api/books/{bid}/{path}")

    pause_armed.set()
    old = threading.Thread(target=old_get, daemon=True)
    old.start()
    assert acquired.wait(5)
    assert not any(
        thread.name.startswith("book-lifecycle-") for thread in threading.enumerate()
    ), "GET lifecycle backpressure must use AnyIO bounded pool, not one raw thread per request"

    replacement = {}

    def replace():
        replacement["delete"] = c.delete(f"/api/books/{bid}")
        replacement["import"] = c.post(
            "/api/books", files={"file": ("b.epub", source, "application/epub+zip")}
        )

    replacing = threading.Thread(target=replace, daemon=True)
    replacing.start()
    crossed = delete_entered.wait(0.2) or delete_crossed.is_set()
    if crossed:
        # Keep the expected RED run from stranding request threads during fixture shutdown.
        release.set()
        old.join(5)
        replacing.join(5)
    assert not crossed, f"delete crossed the paused GET /{path} lifecycle lease"
    assert replacing.is_alive()

    release.set()
    old.join(5)
    replacing.join(5)
    assert not old.is_alive() and not replacing.is_alive()
    assert old_result["response"].status_code == 200
    assert assert_old(old_result["response"].json())
    assert replacement["delete"].status_code == 204
    assert replacement["import"].status_code == 201
    assert replacement["import"].json()["book_id"] == bid

    state = original_get_state(bid)
    assert (state["bookmark"], state["cfi"], state["ingest_progress"]) == (0, None, 0)
    assert app.state.catalog.get_costs(bid) == []
    assert app.state.worker.status(bid) == {"status": "idle", "error": None, "flags": []}
    assert c.get(f"/api/books/{bid}/graph").json()["characters"] == []


def test_request_gate_is_released_when_a_route_raises(env):
    from fastapi import Depends

    from app.deps import book_lifecycle

    c, _, bid, app = env
    baseline = len(app.state.book_request_locks)

    @app.get("/_test/gate-error/{book_id}", dependencies=[Depends(book_lifecycle)])
    def gate_error(book_id: str):
        raise RuntimeError(f"boom: {book_id}")

    with pytest.raises(RuntimeError, match="boom"):
        c.get(f"/_test/gate-error/{bid}")

    # Dependency cleanup ran on the app loop and left the gate immediately reusable.
    assert len(app.state.book_request_locks) == baseline
    assert c.get(f"/api/books/{bid}/position").status_code == 200
    assert len(app.state.book_request_locks) == baseline


def test_queued_request_gate_is_event_loop_native_cancellable_and_reusable(env):
    import asyncio
    import threading

    import anyio.to_thread
    import httpx
    from fastapi import Depends

    from app.deps import _BookRequestLockEntry, book_lifecycle

    c, _, bid, app = env
    route_runs = 0

    @app.get("/_test/gate-queue/{book_id}", dependencies=[Depends(book_lifecycle)])
    def gate_queue(book_id: str):
        nonlocal route_runs
        route_runs += 1
        return {"book_id": book_id}

    @app.get("/_test/unrelated")
    def unrelated():
        return {"ok": True}

    async def exercise():
        loop = asyncio.get_running_loop()
        loop_errors = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        limiter = anyio.to_thread.current_default_thread_limiter()
        original_tokens = limiter.total_tokens
        limiter.total_tokens = 2
        baseline = len(app.state.book_request_locks)
        entry = _BookRequestLockEntry(asyncio.Lock(), 1)
        app.state.book_request_locks[bid] = entry
        lock = entry.lock
        await lock.acquire()
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                queued = [
                    asyncio.create_task(client.get(f"/_test/gate-queue/{bid}"))
                    for _ in range(24)
                ]
                for _ in range(100):
                    if entry.refcount == 1 + len(queued):
                        break
                    await asyncio.sleep(0.01)
                assert entry.refcount == 1 + len(queued)
                assert app.state.book_request_locks.get(bid) is entry
                assert route_runs == 0
                assert limiter.borrowed_tokens == 0
                before = {thread.ident for thread in threading.enumerate()}

                # If gate waiters occupied AnyIO's two worker tokens, this unrelated sync route would
                # starve. Gate queuing itself must not create any raw per-request threads either.
                response = await asyncio.wait_for(client.get("/_test/unrelated"), timeout=1)
                assert response.json() == {"ok": True}
                after = {thread.ident for thread in threading.enumerate()}
                assert len(after - before) <= 1  # at most the unrelated route's bounded-pool worker

                for request in queued:
                    request.cancel()
                results = await asyncio.gather(*queued, return_exceptions=True)
                assert all(isinstance(result, asyncio.CancelledError) for result in results)
                assert entry.refcount == 1
                assert app.state.book_request_locks.get(bid) is entry
                assert route_runs == 0
                assert limiter.borrowed_tokens == 0
                lock.release()
                entry.refcount -= 1
                if entry.refcount == 0 and app.state.book_request_locks.get(bid) is entry:
                    del app.state.book_request_locks[bid]

                # Cancellation removed every waiter and did not poison the lock.
                response = await client.get(f"/_test/gate-queue/{bid}")
                assert response.status_code == 200
                assert route_runs == 1
            await asyncio.sleep(0)
        finally:
            if lock.locked():
                lock.release()
            if app.state.book_request_locks.get(bid) is entry:
                del app.state.book_request_locks[bid]
            limiter.total_tokens = original_tokens
            loop.set_exception_handler(previous_handler)
        assert not loop_errors
        assert len(app.state.book_request_locks) == baseline

    # Execute on TestClient's lifespan/event loop: request locks are deliberately app-loop scoped.
    c.portal.call(exercise)
    assert not any(t.name.startswith("book-lifecycle-") for t in threading.enumerate())


def test_delete_waits_for_the_commit_lifecycle_lease(env, monkeypatch):
    import threading
    import app.ingest.worker as worker_mod

    c, settings, bid, app = env
    started, release = threading.Event(), threading.Event()
    original_ingest = worker_mod.ingest_chapter

    class Threaded:
        def submit(self, fn, *args):
            threading.Thread(target=fn, args=args, daemon=True).start()

        def shutdown(self, wait=True):
            pass

    def blocked_ingest(*args, **kwargs):
        started.set()
        release.wait(5)
        return original_ingest(*args, **kwargs)

    app.state.worker._executor = Threaded()
    monkeypatch.setattr(worker_mod, "ingest_chapter", blocked_ingest)
    c.put(
        f"/api/books/{bid}/position",
        json={"cfi": "commit", "offset": _mlen(settings, bid, 0) + 5},
    )
    assert started.wait(5)

    result = {}
    deleting = threading.Event()

    def delete():
        deleting.set()
        result["response"] = c.delete(f"/api/books/{bid}")

    thread = threading.Thread(target=delete, daemon=True)
    thread.start()
    assert deleting.wait(5)
    threading.Event().wait(0.1)
    assert thread.is_alive(), "delete crossed the lifecycle lease while memory commit was active"
    assert app.state.catalog.get_book(bid) is not None
    release.set()
    thread.join(5)

    assert not thread.is_alive()
    assert result["response"].status_code == 204
    assert app.state.catalog.get_book(bid) is None


def test_validated_target_and_receipt_replay_ignore_stale_ahead_progress(env):
    c, settings, bid, app = env
    c.put(
        f"/api/books/{bid}/position",
        json={"cfi": "receipt", "offset": _mlen(settings, bid, 0) + 5},
    )
    catalog = app.state.catalog
    with catalog._lock:
        catalog._conn.execute("DELETE FROM cost_ledger WHERE book_id=?", (bid,))
        catalog._conn.commit()
    catalog.set_ingest_progress(bid, 99)

    assert app.state.worker._ingest_upto(bid, 1) == 1
    assert len(catalog.get_costs(bid)) == 1


def test_target_past_the_manifest_fails_instead_of_hot_looping(env):
    _, _, bid, app = env
    app.state.worker.enqueue(bid, 99)
    status = app.state.worker.status(bid)
    assert status["status"] == "error"
    assert "exceeds final chapter ordinal" in (status["error"] or "")


def test_llm_runs_outside_the_per_book_lock(env):
    # D-A3: the extraction call must never happen while the per-book lock is held
    c, settings, bid, app = env
    store, client = app.state.store, app.state.client
    orig = client.complete
    seen = []

    def probed(*a, **kw):
        seen.append(store._lock_for(bid).locked())
        return orig(*a, **kw)

    client.complete = probed
    try:
        c.put(f"/api/books/{bid}/position",
              json={"cfi": "x", "offset": _mlen(settings, bid, 0) + 5})
    finally:
        client.complete = orig
    assert seen, "the worker must have called the LLM"
    assert not any(seen), "client.complete ran while the per-book lock was held (D-A3 violation)"


def test_gating_flag_blocks_ingestion(env, monkeypatch):
    # the COVERAGE-GAP / ANCHOR-RESOLUTION-FAILURE class must BLOCK ingestion (ADR 0007 routed gate)
    import app.ingest.worker as worker_mod
    c, settings, bid, app = env
    orig = worker_mod.segment_for_ingest

    def flagged(epub, book_id):
        result, chapters = orig(epub, book_id)
        import dataclasses
        return dataclasses.replace(result, flags=result.flags + ("COVERAGE GAP: file x.xhtml yielded no atom",)), chapters

    monkeypatch.setattr(worker_mod, "segment_for_ingest", flagged)
    c.put(f"/api/books/{bid}/position", json={"cfi": "x", "offset": _mlen(settings, bid, 0) + 5})
    st = c.get(f"/api/books/{bid}/ingest").json()
    assert st["status"] == "blocked"
    assert st["ingest_progress"] == 0                          # nothing was ingested past the gate
    assert any("COVERAGE GAP" in f for f in st["flags"])


def test_worker_segmentation_divergence_from_manifest_blocks(env):
    """D-A10 worker-side: if the stored source.epub now segments differently than the import-time
    manifest (a code/atom-set drift), ingestion BLOCKS rather than stamping facts under a different
    numbering."""
    import hashlib
    c, settings, bid, _ = env
    path = os.path.join(settings.data_dir, "books", bid, "atoms.json")
    with open(path, encoding="utf-8") as f:
        m = json.load(f)
    m["atoms"][0]["char_len"] += 7                             # self-CONSISTENT tamper:
    blob = "|".join(f"{a['ordinal']}:{a['key']}:{a['char_len']}" for a in m["atoms"])
    m["atom_set_version"] = hashlib.sha256(blob.encode()).hexdigest()[:16]   # version recomputed
    with open(path, "w", encoding="utf-8") as f:
        json.dump(m, f)
    c.put(f"/api/books/{bid}/position", json={"cfi": "x", "offset": m["atoms"][0]["char_len"] + 5})
    st = c.get(f"/api/books/{bid}/ingest").json()
    assert st["status"] == "blocked" and st["ingest_progress"] == 0


def test_worker_accepts_declared_language_when_legacy_manifest_never_recorded_one(env):
    """A pre-LIT-23 manifest has no language claim to disagree with the unchanged source EPUB."""
    c, settings, _bid, app = env
    legacy_epub = epub_ncx(
        [("c1.xhtml", "Chapter I", "Chapter I", "Aldric arrived in the valley. " * 12)],
        language="en",
    )
    bid = c.post(
        "/api/books",
        files={"file": ("legacy.epub", legacy_epub, "application/epub+zip")},
    ).json()["book_id"]
    path = os.path.join(settings.data_dir, "books", bid, "atoms.json")
    with open(path, encoding="utf-8") as f:
        manifest = json.load(f)
    manifest.pop("content_language")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    with app.state.store.book(bid) as mem:
        mem.set_content_language("und")

    response = c.put(
        f"/api/books/{bid}/position",
        json={"cfi": "legacy-language", "offset": manifest["atoms"][0]["char_len"] + 5},
    )

    assert response.status_code == 200
    status = c.get(f"/api/books/{bid}/ingest").json()
    assert status["status"] == "done" and status["flags"] == []


def test_worker_still_blocks_an_explicit_content_language_disagreement(env):
    c, settings, bid, app = env
    path = os.path.join(settings.data_dir, "books", bid, "atoms.json")
    with open(path, encoding="utf-8") as f:
        manifest = json.load(f)
    manifest["content_language"] = "ru"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    with app.state.store.book(bid) as mem:
        mem.set_content_language("ru")

    response = c.put(
        f"/api/books/{bid}/position",
        json={"cfi": "explicit-mismatch", "offset": manifest["atoms"][0]["char_len"] + 5},
    )

    assert response.status_code == 200
    status = c.get(f"/api/books/{bid}/ingest").json()
    assert status["status"] == "blocked"
    assert any("different content language" in flag for flag in status["flags"])


def test_ingest_status_of_unknown_book_404s(env):
    c, _, _, _ = env
    assert c.get("/api/books/nope/ingest").status_code == 404


def test_no_embed_under_the_per_book_lock_even_on_the_resolve_path(env, monkeypatch):
    """Module E review pass-1 (concurrency lens, BLOCKER): the chunk embed + layer-4 resolution embeds
    ran INSIDE the commit-phase lock (ingest_chapter's defaults). All embeds must be precomputed
    outside; owner-aware probe: no client.embed call may run while THIS thread owns the book session."""
    import threading
    c, settings, bid, app = env
    store, client = app.state.store, app.state.client
    monkeypatch.setattr(client, "embed_identity",
                        lambda: "openai-compatible@https://x:fake-real-embedder")   # engage layer 4
    under_lock = []
    orig = client.embed

    def probed(texts):
        h = store._handles.get(bid)
        under_lock.append(bool(h is not None and h._active_owner == threading.get_ident()))
        return orig(texts)

    client.embed = probed
    try:
        c.put(f"/api/books/{bid}/position", json={"cfi": "x", "offset": _mlen(settings, bid, 0) + 5})
    finally:
        client.embed = orig
    assert c.get(f"/api/books/{bid}/ingest").json()["ingest_progress"] == 1
    assert under_lock, "the worker must have embedded (chunk + resolution warm)"
    assert not any(under_lock), \
        f"{sum(under_lock)}/{len(under_lock)} embed calls ran while this thread owned the book session"


def test_catalog_finalization_runs_after_the_per_book_lock_is_released(env, monkeypatch):
    c, settings, bid, app = env
    catalog, store = app.state.catalog, app.state.store
    original_finalize = catalog.finalize_ingest
    lock_states = []

    def probed_finalize(book_id, ordinal, *, cost=None, incarnation=None):
        lock_states.append(store._lock_for(book_id).locked())
        return original_finalize(book_id, ordinal, cost=cost, incarnation=incarnation)

    monkeypatch.setattr(catalog, "finalize_ingest", probed_finalize)
    c.put(
        f"/api/books/{bid}/position",
        json={"cfi": "x", "offset": _mlen(settings, bid, 0) + 5},
    )

    assert app.state.worker.status(bid)["status"] == "done"
    assert lock_states and not any(lock_states)


def test_catalog_progress_without_v2_marker_fails_closed(env):
    c, settings, bid, app = env
    chapter = app.state.worker._segmented(bid)[1][0]
    text = chapter.get("text", "") or ""
    content_hash = chapter.get("content_hash") or content_hash_of(text)
    with app.state.store.book(bid) as mem:
        mem.add_chapter(
            chapter["key"],
            chapter["ordinal"],
            href=chapter.get("href", ""),
            content_hash=content_hash,
        )
        mem.add_entity("Legacy Partial", "character", revealed_at=chapter["ordinal"])
    app.state.catalog.set_ingest_progress(bid, chapter["ordinal"])

    response = c.put(
        f"/api/books/{bid}/position",
        json={"cfi": "legacy-progress", "offset": _mlen(settings, bid, 0)},
    )
    assert response.status_code == 200
    status = c.get(f"/api/books/{bid}/ingest").json()
    assert status["status"] == "error"
    assert status["ingest_progress"] == 0
    assert c.get(f"/api/books/{bid}/position").json()["ingest_progress"] == 0
    assert "completion marker" in status["error"]
    graph = c.get(f"/api/books/{bid}/graph", params={"bookmark": chapter["ordinal"]})
    assert graph.status_code == 200
    assert graph.json()["as_of_chapter"] == 0
    assert graph.json()["characters"] == []
    with app.state.store.book(bid) as mem:
        assert mem.chapter_completion(
            chapter["key"], chapter["ordinal"], content_hash
        ) is None


def test_later_marker_corruption_lowers_the_published_frontier(env):
    c, settings, bid, app = env
    total = sum(_mlen(settings, bid, i) for i in range(3))
    c.put(f"/api/books/{bid}/position", json={"cfi": "validated", "offset": total})
    assert c.get(f"/api/books/{bid}/ingest").json()["ingest_progress"] == 3

    chapter2 = app.state.worker._segmented(bid)[1][1]
    with app.state.store.book(bid) as mem:
        with mem._writer():
            mem._conn.execute("DELETE FROM ingested_chapters WHERE chapter_key=?", (chapter2["key"],))

    c.put(f"/api/books/{bid}/position", json={"cfi": "revalidated", "offset": total})
    status = c.get(f"/api/books/{bid}/ingest").json()
    assert status["status"] == "error"
    assert status["ingest_progress"] == 1
    graph = c.get(f"/api/books/{bid}/graph", params={"bookmark": 3}).json()
    assert graph["as_of_chapter"] == 1


def test_get_only_marker_corruption_immediately_clamps_derived_reads(env):
    c, settings, bid, app = env
    total = sum(_mlen(settings, bid, i) for i in range(3))
    c.put(f"/api/books/{bid}/position", json={"cfi": "validated", "offset": total})
    assert c.get(f"/api/books/{bid}/ingest").json()["ingest_progress"] == 3

    chapter2 = app.state.worker._segmented(bid)[1][1]
    with app.state.store.book(bid) as mem:
        with mem._writer():
            mem._conn.execute("DELETE FROM ingested_chapters WHERE chapter_key=?", (chapter2["key"],))

    assert c.get(f"/api/books/{bid}/ingest").json()["ingest_progress"] == 1
    assert c.get(f"/api/books/{bid}/position").json()["ingest_progress"] == 1
    graph = c.get(f"/api/books/{bid}/graph", params={"bookmark": 3}).json()
    assert graph["as_of_chapter"] == 1


def test_process_restart_rehydrates_frontier_from_intact_markers_without_put(tmp_path):
    settings = Settings(_env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"))
    first = create_app(settings, ingest_executor=InlineExecutor())
    with TestClient(first) as c:
        bid = c.post(
            "/api/books",
            files={"file": ("b.epub", three_chapter_book(), "application/epub+zip")},
        ).json()["book_id"]
        c.put(
            f"/api/books/{bid}/position",
            json={"cfi": "before-restart", "offset": _mlen(settings, bid, 0) + 5},
        )
        assert c.get(f"/api/books/{bid}/ingest").json()["ingest_progress"] == 1

    restarted = create_app(settings, ingest_executor=InlineExecutor())
    with TestClient(restarted) as c:
        assert restarted.state.worker._state == {}
        assert c.get(f"/api/books/{bid}/ingest").json()["ingest_progress"] == 1
        assert c.get(f"/api/books/{bid}/position").json()["ingest_progress"] == 1
        assert c.get(f"/api/books/{bid}/graph").json()["as_of_chapter"] == 1


def test_enqueue_invalidates_frontier_before_delayed_revalidation_runs(env):
    c, settings, bid, app = env
    offset = _mlen(settings, bid, 0) + 5
    c.put(f"/api/books/{bid}/position", json={"cfi": "validated", "offset": offset})
    assert app.state.worker.validated_frontier(bid) == 1

    chapter = app.state.worker._segmented(bid)[1][0]
    with app.state.store.book(bid) as mem:
        with mem._writer():
            mem._conn.execute("DELETE FROM ingested_chapters WHERE chapter_key=?", (chapter["key"],))

    class Delayed:
        def __init__(self):
            self.task = None

        def submit(self, fn, *args):
            self.task = (fn, args)

        def shutdown(self, wait=True):
            pass

    delayed = Delayed()
    app.state.worker._executor = delayed
    c.put(f"/api/books/{bid}/position", json={"cfi": "queued", "offset": offset})

    assert app.state.worker.validated_frontier(bid) == 0
    assert c.get(f"/api/books/{bid}/ingest").json()["ingest_progress"] == 0
    assert c.get(f"/api/books/{bid}/graph", params={"bookmark": 1}).json()["as_of_chapter"] == 0
    assert delayed.task is not None
    delayed.task[0](*delayed.task[1])
    assert app.state.worker.status(bid)["status"] == "error"
    assert app.state.worker.validated_frontier(bid) == 0


def test_worker_error_path_resumes_and_clears_the_stale_error(env):
    """Pass-1 (concurrency lens): an extractor failure surfaces as status=error; the NEXT position
    report re-enqueues, resumes from the high-water, and a successful finish clears the stale error."""
    c, settings, bid, app = env
    client = app.state.client
    orig = client.complete
    poisoned = {"armed": True}

    def flaky(system, user, tier="cheap", schema=None):
        if poisoned["armed"] and "Chapter II" in user:
            poisoned["armed"] = False
            raise RuntimeError("simulated extractor outage")
        return orig(system, user, tier=tier, schema=schema)

    client.complete = flaky
    try:
        total = sum(_mlen(settings, bid, i) for i in range(3))
        c.put(f"/api/books/{bid}/position", json={"cfi": "x", "offset": total})
        st = c.get(f"/api/books/{bid}/ingest").json()
        assert st["status"] == "error" and st["ingest_progress"] == 1
        assert "simulated extractor outage" in (st["error"] or "")
        c.put(f"/api/books/{bid}/position", json={"cfi": "x", "offset": total})   # heal
        st2 = c.get(f"/api/books/{bid}/ingest").json()
    finally:
        client.complete = orig
    assert st2["status"] == "done" and st2["ingest_progress"] == 3
    assert st2["error"] is None, "a successful resume must clear the stale error string"


def test_malformed_extraction_retries_with_backoff_writes_nothing_then_resumes(env):
    c, settings, bid, app = env
    client = app.state.client
    original_complete = client.complete
    attempts = []
    delays = []

    def malformed(*args, **kwargs):
        attempts.append(1)
        return {}, {"in": 1, "out": 1}

    client.complete = malformed
    app.state.worker._sleep = delays.append
    try:
        c.put(
            f"/api/books/{bid}/position",
            json={"cfi": "x", "offset": _mlen(settings, bid, 0) + 5},
        )
    finally:
        client.complete = original_complete

    status = c.get(f"/api/books/{bid}/ingest").json()
    assert status["status"] == "error" and status["ingest_progress"] == 0
    assert len(attempts) == 2
    assert delays == [0.25]
    assert "malformed extraction" in (status["error"] or "").lower()
    with app.state.store.book(bid) as mem:
        for table in ("chapters", "raw_chapters", "entities", "edges", "events", "chunks"):
            assert mem._audit_all(table) == [], table

    c.put(
        f"/api/books/{bid}/position",
        json={"cfi": "x", "offset": _mlen(settings, bid, 0) + 5},
    )
    healed = c.get(f"/api/books/{bid}/ingest").json()
    assert healed["status"] == "done" and healed["ingest_progress"] == 1
    with app.state.store.book(bid) as mem:
        assert len(mem._audit_all("chapters")) == 1
        assert len(mem._audit_all("raw_chapters")) == 1


def test_malformed_first_response_then_valid_retry_writes_exactly_once(env, monkeypatch):
    c, settings, bid, app = env
    client = app.state.client
    original_complete = client.complete
    calls = {"count": 0}
    delays = []

    def invalid_then_valid(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return {"chapter_summary": 17}, {"in": 1, "out": 1}
        with app.state.store.book(bid) as mem:
            assert mem._audit_all("chapters") == []
        return original_complete(*args, **kwargs)

    monkeypatch.setattr(client, "complete", invalid_then_valid)
    monkeypatch.setattr(app.state.worker, "_sleep", delays.append)
    c.put(
        f"/api/books/{bid}/position",
        json={"cfi": "x", "offset": _mlen(settings, bid, 0) + 5},
    )

    status = c.get(f"/api/books/{bid}/ingest").json()
    assert status["status"] == "done" and status["ingest_progress"] == 1
    assert calls["count"] == 2 and delays == [0.25]
    with app.state.store.book(bid) as mem:
        assert len(mem._audit_all("chapters")) == 1
        assert len(mem._audit_all("ingested_chapters")) == 1


def test_catalog_progress_failure_after_memory_commit_resumes_without_duplicates(env, monkeypatch):
    c, settings, bid, app = env
    catalog = app.state.catalog
    original_finalize = catalog.finalize_ingest
    armed = {"value": True}

    def fail_once(book_id, ordinal, *, cost=None, incarnation=None):
        if armed["value"]:
            armed["value"] = False
            raise RuntimeError("fault injected after memory commit")
        return original_finalize(book_id, ordinal, cost=cost, incarnation=incarnation)

    monkeypatch.setattr(catalog, "finalize_ingest", fail_once)
    c.put(
        f"/api/books/{bid}/position",
        json={"cfi": "x", "offset": _mlen(settings, bid, 0) + 5},
    )
    monkeypatch.setattr(catalog, "finalize_ingest", original_finalize)

    failed = c.get(f"/api/books/{bid}/ingest").json()
    assert failed["status"] == "error" and failed["ingest_progress"] == 0
    assert "fault injected after memory commit" in (failed["error"] or "")
    with app.state.store.book(bid) as mem:
        before = {
            table: len(mem._audit_all(table))
            for table in ("chapters", "raw_chapters", "entities", "events", "chunks")
        }
    assert before["chapters"] == 1 and before["raw_chapters"] == 1
    assert len(catalog.get_costs(bid)) == 0

    import app.ingest.worker as worker_mod

    def unavailable_identity(_client):
        raise RuntimeError("identity provider unavailable")

    monkeypatch.setattr(worker_mod.versioning, "current_identity", unavailable_identity)

    c.put(
        f"/api/books/{bid}/position",
        json={"cfi": "x", "offset": _mlen(settings, bid, 0) + 5},
    )
    healed = c.get(f"/api/books/{bid}/ingest").json()
    assert healed["status"] == "done" and healed["ingest_progress"] == 1
    with app.state.store.book(bid) as mem:
        after = {
            table: len(mem._audit_all(table))
            for table in ("chapters", "raw_chapters", "entities", "events", "chunks")
        }
    assert after == before
    assert len(catalog.get_costs(bid)) == 1


def test_raised_target_during_the_exit_window_is_never_lost():
    """Pass-1 (concurrency lens, HIGH — the lost wakeup): an enqueue that lands while the worker task
    is deciding to exit must either be looped over or resubmitted — never dropped with status=done.
    Driven deterministically: _ingest_upto blocks at a barrier; the target is raised DURING the call;
    the loop must re-check and ingest to the raised target."""
    import threading
    from app.ingest.worker import IngestWorker

    class W(IngestWorker):
        def __init__(self):
            super().__init__(store=None, catalog=None, client=None, settings=None,
                             executor=_Threaded())
            self.calls = []
            self.mid_call = threading.Event()
            self.release = threading.Event()

        def _ingest_upto(self, book_id, target, incarnation=None, generation=None):
            self.calls.append(target)
            if len(self.calls) == 1:
                self.mid_call.set()
                self.release.wait(5)                    # hold while the target is raised
            return target

    class _Threaded:
        def submit(self, fn, *a):
            threading.Thread(target=fn, args=a, daemon=True).start()

        def shutdown(self, wait=True):
            pass

    w = W()
    w.enqueue("b", 1)
    assert w.mid_call.wait(5)
    w.enqueue("b", 3)                                    # raised while the first call is in flight
    w.release.set()
    deadline = threading.Event()
    for _ in range(100):
        if w.status("b")["status"] == "done" and w.calls and w.calls[-1] == 3:
            break
        deadline.wait(0.05)
    assert w.calls[-1] == 3, f"raised target lost: calls={w.calls}, status={w.status('b')}"
    assert w.status("b")["status"] == "done"


def test_raised_target_while_blocked_is_rechecked():
    import threading
    from app.ingest.worker import IngestWorker

    class Threaded:
        def submit(self, fn, *args):
            threading.Thread(target=fn, args=args, daemon=True).start()

        def shutdown(self, wait=True):
            pass

    class W(IngestWorker):
        def __init__(self):
            super().__init__(store=None, catalog=None, client=None, settings=None, executor=Threaded())
            self.calls = []
            self.blocking = threading.Event()
            self.release = threading.Event()

        def _ingest_upto(self, book_id, target, incarnation=None, generation=None):
            self.calls.append(target)
            if len(self.calls) == 1:
                self.blocking.set()
                self.release.wait(5)
                return None
            return target

    w = W()
    w.enqueue("b", 1)
    assert w.blocking.wait(5)
    w.enqueue("b", 3)
    w.release.set()
    for _ in range(100):
        if w.status("b")["status"] == "done":
            break
        threading.Event().wait(0.05)

    assert w.calls == [1, 3]
    assert w.status("b")["status"] == "done"


def test_raised_target_during_a_failing_generation_is_resubmitted():
    import threading
    from app.ingest.worker import IngestWorker

    class Threaded:
        def submit(self, fn, *args):
            threading.Thread(target=fn, args=args, daemon=True).start()

        def shutdown(self, wait=True):
            pass

    class W(IngestWorker):
        def __init__(self):
            super().__init__(store=None, catalog=None, client=None, settings=None, executor=Threaded())
            self.calls = []
            self.mid_call = threading.Event()
            self.release = threading.Event()

        def _ingest_upto(self, book_id, target, incarnation=None, generation=None):
            self.calls.append(target)
            if len(self.calls) == 1:
                self.mid_call.set()
                self.release.wait(5)
                raise RuntimeError("first generation failed")
            return target

    w = W()
    w.enqueue("b", 1)
    assert w.mid_call.wait(5)
    w.enqueue("b", 3)
    w.release.set()
    deadline = threading.Event()
    for _ in range(100):
        if w.status("b")["status"] == "done":
            break
        deadline.wait(0.05)
    assert w.calls == [1, 3]
    assert w.status("b")["status"] == "done"


def test_a_raising_submit_does_not_wedge_the_book():
    """Pass-1 (concurrency lens, MEDIUM): if executor.submit raises (shutdown window), the book must
    surface status=error and remain enqueueable — never stuck 'running' forever."""
    from app.ingest.worker import IngestWorker

    class Broken:
        def submit(self, fn, *a):
            raise RuntimeError("cannot schedule new futures after shutdown")

        def shutdown(self, wait=True):
            pass

    w = IngestWorker(store=None, catalog=None, client=None, settings=None, executor=Broken())
    w.enqueue("b", 2)
    st = w.status("b")
    assert st["status"] == "error" and "submit" in (st["error"] or "").lower()
    ran = []

    class Inline:
        def submit(self, fn, *a):
            ran.append(a)

        def shutdown(self, wait=True):
            pass

    w._executor = Inline()
    w.enqueue("b", 2)                                    # recovers: not wedged in _running
    assert ran, "a later enqueue must be able to submit again"


def test_enqueue_during_failed_initial_submit_is_rescheduled():
    import threading
    from app.ingest.worker import IngestWorker

    class Executor:
        def __init__(self):
            self.calls = 0
            self.submitting = threading.Event()
            self.release = threading.Event()

        def submit(self, fn, *args):
            self.calls += 1
            if self.calls == 1:
                self.submitting.set()
                self.release.wait(5)
                raise RuntimeError("first submit failed")
            fn(*args)

        def shutdown(self, wait=True):
            pass

    class W(IngestWorker):
        def __init__(self, executor):
            super().__init__(store=None, catalog=None, client=None, settings=None, executor=executor)
            self.calls = []

        def _ingest_upto(self, book_id, target, incarnation=None, generation=None):
            self.calls.append(target)
            return target

    executor = Executor()
    w = W(executor)
    first = threading.Thread(target=w.enqueue, args=("b", 1), daemon=True)
    first.start()
    assert executor.submitting.wait(5)
    w.enqueue("b", 3)
    executor.release.set()
    first.join(5)

    assert executor.calls == 2
    assert w.calls == [3]
    assert w.status("b")["status"] == "done"


def test_enqueue_during_failed_resubmit_is_rescheduled():
    import threading
    from app.ingest.worker import IngestWorker

    class Executor:
        def __init__(self):
            self.calls = 0
            self.resubmitting = threading.Event()
            self.release = threading.Event()

        def submit(self, fn, *args):
            self.calls += 1
            if self.calls == 2:
                self.resubmitting.set()
                self.release.wait(5)
                raise RuntimeError("resubmit failed")
            threading.Thread(target=fn, args=args, daemon=True).start()

        def shutdown(self, wait=True):
            pass

    class W(IngestWorker):
        def __init__(self, executor):
            super().__init__(store=None, catalog=None, client=None, settings=None, executor=executor)
            self.calls = []
            self.failing = threading.Event()
            self.release = threading.Event()

        def _ingest_upto(self, book_id, target, incarnation=None, generation=None):
            self.calls.append(target)
            if len(self.calls) == 1:
                self.failing.set()
                self.release.wait(5)
                raise RuntimeError("generation failed")
            return target

    executor = Executor()
    w = W(executor)
    w.enqueue("b", 1)
    assert w.failing.wait(5)
    w.enqueue("b", 2)
    w.release.set()
    assert executor.resubmitting.wait(5)
    w.enqueue("b", 3)
    executor.release.set()
    for _ in range(100):
        if w.status("b")["status"] == "done":
            break
        threading.Event().wait(0.05)

    assert executor.calls == 3
    assert w.calls == [1, 3]
    assert w.status("b")["status"] == "done"


def test_unknown_protected_requests_do_not_grow_lock_registries(env):
    c, _, _, app = env
    request_baseline = len(app.state.book_request_locks)
    lifecycle_baseline = len(app.state.worker._lifecycle_locks)

    for i in range(80):
        assert c.get(f"/api/books/missing-get-{i}/position").status_code == 404
    for i in range(80):
        assert c.delete(f"/api/books/missing-delete-{i}").status_code == 404

    assert len(app.state.book_request_locks) == request_baseline
    assert len(app.state.worker._lifecycle_locks) == lifecycle_baseline


def test_lifecycle_registry_keeps_one_entry_for_holder_and_waiters(env):
    import threading

    _, _, bid, app = env
    worker = app.state.worker
    baseline = len(worker._lifecycle_locks)
    holder_entered = threading.Event()
    release_holder = threading.Event()
    waiter_started = [threading.Event() for _ in range(8)]
    observed_locks = []
    errors = []

    def holder():
        with worker.book_lifecycle(bid):
            with worker._mu:
                observed_locks.append(id(worker._lifecycle_locks[bid].lock))
            holder_entered.set()
            release_holder.wait(5)

    def waiter(started, fail=False):
        started.set()
        try:
            with worker.book_lifecycle(bid):
                with worker._mu:
                    observed_locks.append(id(worker._lifecycle_locks[bid].lock))
                if fail:
                    raise RuntimeError("waiter boom")
        except RuntimeError as exc:
            errors.append(str(exc))

    holding = threading.Thread(target=holder, daemon=True)
    holding.start()
    assert holder_entered.wait(5)
    waiters = [
        threading.Thread(target=waiter, args=(started, i == 0), daemon=True)
        for i, started in enumerate(waiter_started)
    ]
    for thread in waiters:
        thread.start()
    assert all(started.wait(5) for started in waiter_started)

    for _ in range(100):
        with worker._mu:
            entry = worker._lifecycle_locks.get(bid)
            if entry is not None and entry.refcount == 1 + len(waiters):
                break
        threading.Event().wait(0.01)
    assert entry is not None and entry.refcount == 1 + len(waiters)
    expected_lock = id(entry.lock)
    assert holding.is_alive()

    release_holder.set()
    holding.join(5)
    for thread in waiters:
        thread.join(5)
    assert not holding.is_alive() and not any(thread.is_alive() for thread in waiters)
    assert errors == ["waiter boom"]
    assert observed_locks == [expected_lock] * (1 + len(waiters))
    assert len(worker._lifecycle_locks) == baseline
