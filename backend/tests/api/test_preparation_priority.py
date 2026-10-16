"""Reading intent preempts paid work; resumable plans never bypass premise/answer clearance."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

from app.answer_bank import ANSWER_VERSION, AnswerBank
from app.preparation_plan import valid_saved_plan
from app.reader_prepare import (
    GuardedCache, PreparationCatalog, PreparationStale, PreparationSuperseded,
    preparation_key, prepare,
)
from app.recap_warm import RecapPrewarmer
from test_views import env as env
from test_reader_preparation import capture, payload
from test_preparation_plan import audit, question


def saved_plan(*, reviewed=False):
    return {"reviewed": reviewed, "facts_hash": "a"*64,
            "questions": [{"id": 1, **question()}]}


def start(api, app):
    app.state.settings.reader_prepare_delay_seconds = 0
    async def run():
        app.state.recap_warmer = RecapPrewarmer(app)
    api.portal.call(run)
    return app.state.recap_warmer


def stop(api, app):
    api.portal.call(app.state.recap_warmer.close)
    app.state.recap_warmer = None


def test_foreground_displaces_full_startup_queue_and_join_waits_for_replacement(env, monkeypatch):
    api, bid, app, _ = env
    calls, entered, release = [], threading.Event(), threading.Event()
    def work(_app, scope):
        calls.append(scope.book_id)
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr("app.recap_warm.prepare", work)
    warmer = start(api, app)
    for i in range(8):
        app.state.catalog.add_book(f"queued-{i}", title="Queued")
        app.state.catalog.set_position(f"queued-{i}", "x", 1)
        app.state.catalog.set_ingest_progress(f"queued-{i}", 1)
    async def fill_and_focus():
        for i in range(8):
            warmer._schedule((f"queued-{i}", app.state.catalog.get_book(f"queued-{i}")["incarnation"]))
        assert warmer.queue.full()
        waiter = asyncio.create_task(warmer.queue.join())
        warmer._schedule((bid, app.state.catalog.get_book(bid)["incarnation"]), foreground=True)
        await asyncio.sleep(0)
        assert not waiter.done()
        return waiter
    waiter = api.portal.call(fill_and_focus)
    try:
        assert entered.wait(2)
        assert calls == [bid] and len(warmer.pending) == 1
        assert not waiter.done()
    finally:
        release.set()
        api.portal.call(warmer.queue.join)
        stop(api, app)
    assert waiter.done()


@pytest.mark.parametrize("return_while_active", [False, True])
def test_book_switch_cancels_next_call_and_return_resumes_once(env, monkeypatch, return_while_active):
    api, bid, app, _ = env
    other = "unread-book"
    app.state.catalog.add_book(other, title="Unread")
    entered, release, calls = threading.Event(), threading.Event(), []
    def work(_app, scope):
        calls.append(scope)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
            scope.check(app)  # A second call would fail here, after the first settles.
    monkeypatch.setattr("app.recap_warm.prepare", work)
    warmer = start(api, app)
    inc = app.state.catalog.get_book(bid)["incarnation"]
    warmer.notify(bid, inc, foreground=True)
    assert entered.wait(2)
    async def focus(book):
        warmer._schedule((book, app.state.catalog.get_book(book)["incarnation"]), foreground=True)
    try:
        api.portal.call(focus, other)
        assert calls[0].cancelled.is_set()
        warmer.notify(bid, inc)  # Background ingest cannot steal focus.
        if return_while_active:
            api.portal.call(focus, bid)
        release.set()
        api.portal.call(warmer.queue.join)
        if not return_while_active:
            assert len(calls) == 1 and (bid, inc) not in warmer.completed
            api.portal.call(focus, bid)
            api.portal.call(warmer.queue.join)
        assert len(calls) == 2 and not calls[1].cancelled.is_set()
        for _ in range(10):
            warmer.notify(bid, inc, foreground=True)
        api.portal.call(warmer.queue.join)
        assert len(calls) == 2
    finally:
        release.set()
        stop(api, app)


def test_advance_before_ingest_stops_paid_reservation_atomically_but_retains_old_reviewed_answer(env):
    _, bid, app, _ = env
    scope = capture(app, bid)
    app.state.catalog.set_position(bid, "new-chapter", 3)
    kwargs = dict(phase="reader-prepare:questions", model="test", input_tokens=1, output_tokens=1,
                  usd=.01, max_input_tokens=100, max_output_tokens=100, max_usd=1)
    with pytest.raises(RuntimeError, match="scope is no longer current"):
        app.state.catalog.reserve_cost(bid, **kwargs,
            expected_reading_scope=(scope.incarnation, scope.epoch, scope.frontier))
    with pytest.raises(PreparationSuperseded):
        PreparationCatalog(app, scope).reserve_cost(bid, **kwargs)
    key = (*scope.answer_key, "Where was the meeting?")
    GuardedCache(app.state.reader_client.answers, app, scope).set(key, payload())
    assert app.state.reader_client.answers.get(key)
    assert not app.state.catalog.get_cost_reservations(bid)


def test_unready_frontier_waits_for_ingest_and_same_frontier_backward_pages_coalesce(env, monkeypatch):
    api, bid, app, _ = env
    calls = []
    monkeypatch.setattr("app.recap_warm.prepare", lambda _app, scope: calls.append(scope.frontier))
    warmer = start(api, app)
    inc = app.state.catalog.get_book(bid)["incarnation"]
    try:
        app.state.catalog.set_position(bid, "advanced", 3)
        warmer.notify(bid, inc, foreground=True)
        api.portal.call(warmer.queue.join)
        assert calls == []
        # Ingestion now supplies the grounded durable source for chapter 3.
        app.state.worker.enqueue(bid, 3)
        warmer.notify(bid, inc)
        api.portal.call(warmer.queue.join)
        assert calls == [3]
        for _ in range(10):
            app.state.catalog.set_position(bid, "back-page", 1)
            warmer.notify(bid, inc, foreground=True)
        api.portal.call(warmer.queue.join)
        assert calls == [3]
    finally:
        stop(api, app)


@pytest.mark.parametrize("pause_after", ["PreparedQuestions", "PreparationReview", "answer"])
def test_paused_plan_resumes_after_restart_without_repaying_cleared_stages(env, monkeypatch, tmp_path, pause_after):
    api, bid, app, _ = env
    settings, client = app.state.settings, app.state.reader_client
    settings.reader_prepare_enabled = settings.reader_answer_cache = True
    path = tmp_path / "answers.json"
    client.answers = AnswerBank(persist_path=path)
    scope = replace(capture(app, bid), cancelled=threading.Event())
    calls, paid_answers = [], []
    def complete(*a, **kw):
        name = kw["schema"].__name__
        calls.append(name)
        value = {"questions": [question("Who is Berenice?", "character"),
                             question("Where did Berenice meet Aldric?")]}
        if name == "PreparationReview":
            value = audit(json.loads(kw["user"])["questions"])
        if name == pause_after:
            scope.cancelled.set()
        return SimpleNamespace(value=value)
    def answer(book_id, body, catalog, store, settings, copy_client, worker):
        key = (*scope.answer_key, body.question)
        cached = copy_client.answers.get(key)
        if cached:
            return cached
        paid_answers.append(body.question)
        if pause_after == "answer":
            scope.cancelled.set()
        copy_client.answers.set(key, payload())
        return payload()
    monkeypatch.setattr("app.reader_prepare.budgeted_completion", complete)
    monkeypatch.setattr("app.api.views.ask_the_book", answer)
    with pytest.raises(PreparationSuperseded):
        prepare(app, scope)
    key = preparation_key(scope, settings)
    assert not client.answers.prepared(key)
    assert client.answers.plans[key]["reviewed"] is (pause_after != "PreparedQuestions")
    assert api.get(f"/api/books/{bid}/prepared-questions").json()["questions"] == []
    client.answers = AnswerBank(persist_path=path)
    prepare(app, capture(app, bid))
    assert calls == ["PreparedQuestions", "PreparationReview"]
    assert len(paid_answers) == 2  # The in-flight cleared answer is reused, never regenerated.
    assert client.answers.prepared(key) and not client.answers.plans
    assert len(api.get(f"/api/books/{bid}/prepared-questions").json()["questions"]) == 2


def test_pending_plan_still_requires_full_independent_review(env, monkeypatch):
    _, bid, app, _ = env
    app.state.settings.reader_prepare_enabled = app.state.settings.reader_answer_cache = True
    scope = replace(capture(app, bid), cancelled=threading.Event())
    calls = []
    def complete(*a, **kw):
        calls.append(kw["schema"].__name__)
        if kw["schema"].__name__ == "PreparedQuestions":
            scope.cancelled.set()
            return SimpleNamespace(value={"questions": [question("Why did Corvus kill Berenice?")]})
        return SimpleNamespace(value=audit(json.loads(kw["user"])["questions"], rejected=[1]))
    monkeypatch.setattr("app.reader_prepare.budgeted_completion", complete)
    monkeypatch.setattr("app.api.views.ask_the_book", lambda *a: pytest.fail("rejected premise reached Ask"))
    with pytest.raises(PreparationSuperseded):
        prepare(app, scope)
    prepare(app, capture(app, bid))
    assert calls == ["PreparedQuestions", "PreparationReview"]
    assert not app.state.reader_client.answers.suggestions(scope.answer_key)


@pytest.mark.parametrize("mutation", ["reset", "delete", "correction", "shutdown"])
def test_cancelled_inflight_result_still_rejects_source_changes(env, mutation):
    api, bid, app, _ = env
    scope = replace(capture(app, bid), cancelled=threading.Event())
    scope.cancelled.set()
    if mutation == "reset":
        api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 0})
    elif mutation == "delete":
        api.delete(f"/api/books/{bid}")
    elif mutation == "correction":
        with app.state.store.book(bid) as mem:
            mem.add_entity("Corrected identity", "character", 2)
    else:
        app.state.recap_warmer = SimpleNamespace(closed=True)
    try:
        with pytest.raises(PreparationStale):
            GuardedCache(app.state.reader_client.answers, app, scope).set(
                (*scope.answer_key, "Where did they meet?"), payload())
    finally:
        app.state.recap_warmer = None


@pytest.mark.parametrize("fault", ["boolean_id", "forward_parent", "duplicate", "unknown", "stage", "hash"])
def test_checkpoint_shape_fails_closed(fault):
    value = saved_plan()
    if fault == "boolean_id":
        value["questions"][0]["id"] = True
    if fault == "forward_parent":
        value["questions"][0].update(kind="follow_up", follow_up_to=2)
    if fault == "duplicate":
        value["questions"].append({**value["questions"][0], "id": 2})
    if fault == "unknown":
        value["questions"][0]["approved"] = True
    if fault == "stage":
        value["reviewed"] = "true"
    if fault == "hash":
        value["facts_hash"] = "future"
    assert not valid_saved_plan(value)
    with pytest.raises(ValueError, match="invalid preparation checkpoint"):
        AnswerBank().set_plan(("b", "i", "s"), value)


def test_checkpoint_bounds_copy_hash_invalidation_and_legacy_reload(tmp_path):
    path = tmp_path / "answers.json"
    bank = AnswerBank(max_entries=2, persist_path=path)
    plan = saved_plan()
    key = ("b", "i", "s")
    bank.set_plan(key, plan)
    plan["questions"].clear()
    assert bank.plan(key, "a"*64)["questions"]
    assert bank.plan(key, "b"*64) is None
    returned = bank.plan(key, "a"*64)
    returned["questions"].clear()
    assert bank.plan(key, "a"*64)["questions"]
    bank.set_plan(("b", "i", "new"), saved_plan(reviewed=True))
    assert key not in bank.plans
    bank.set_plan(("c", "i", "s"), saved_plan())
    bank.set_plan(("d", "i", "s"), saved_plan())
    assert len(AnswerBank(max_entries=2, persist_path=path).plans) == 2
    bank.invalidate_book("d")
    assert not any(k[0] == "d" for k in AnswerBank(max_entries=2, persist_path=path).plans)
    # An older checkpoint without staged plans remains readable after upgrade.
    rows = [[['b', 'i', 's', 'Where did they meet?'],
             {"reviewed_question": "Where did they meet?", "payload": payload(), "suggested": True}]]
    prepared = [["b", "i", "s"]]
    legacy = {"version": ANSWER_VERSION, "rows": rows, "prepared": prepared,
              "sha256": hashlib.sha256(bank._encoded([rows, prepared])).hexdigest()}
    path.write_bytes(bank._encoded(legacy))
    restored = AnswerBank(max_entries=2, persist_path=path)
    assert restored.prepared(key) and restored.suggestions(key) and not restored.plans
    corrupt = deepcopy(legacy)
    corrupt["plans"] = [[list(key), saved_plan(reviewed=True)]]
    path.write_bytes(bank._encoded(corrupt))
    assert not AnswerBank(max_entries=2, persist_path=path).rows


@pytest.mark.parametrize("mismatch", ["facts_hash", "basis_ids"])
def test_resumed_plan_must_match_exact_current_fact_pack(env, monkeypatch, mismatch):
    from app.preparation_plan import preparation_facts
    _, bid, app, _ = env
    settings, bank = app.state.settings, app.state.reader_client.answers
    settings.reader_prepare_enabled = settings.reader_answer_cache = True
    scope = capture(app, bid)
    with app.state.store.book(bid) as mem:
        facts = preparation_facts(mem, scope.frontier,
            byte_limit=min(18000, settings.cost_max_input_tokens_per_call//2))
    plan = saved_plan(reviewed=True)
    plan["facts_hash"] = hashlib.sha256(bank._encoded(facts)).hexdigest()
    if mismatch == "facts_hash":
        plan["facts_hash"] = "b"*64
    else:
        plan["questions"][0]["basis_ids"] = [999]
    bank.set_plan(preparation_key(scope, settings), plan)
    calls = []
    def complete(*args, **kwargs):
        calls.append(kwargs["schema"].__name__)
        if calls[-1] == "PreparedQuestions":
            return SimpleNamespace(value={"questions": [question("Who is Berenice?", "character")]})
        return SimpleNamespace(value=audit(json.loads(kwargs["user"])["questions"], rejected=[1]))
    monkeypatch.setattr("app.reader_prepare.budgeted_completion", complete)
    monkeypatch.setattr("app.api.views.ask_the_book", lambda *a: pytest.fail("invalid checkpoint reached Ask"))
    prepare(app, scope)
    assert calls == ["PreparedQuestions", "PreparationReview"]
    assert not bank.suggestions(scope.answer_key)


def test_shutdown_settles_inflight_reservation_without_starting_more_work(env, monkeypatch):
    api, bid, app, _ = env
    entered, release = threading.Event(), threading.Event()
    def work(_app, scope):
        catalog = PreparationCatalog(app, scope)
        reservation = catalog.reserve_cost(bid, phase="questions", model="test", input_tokens=1,
            output_tokens=1, usd=.01, max_input_tokens=10**8, max_output_tokens=10**8, max_usd=10)
        entered.set()
        assert release.wait(5)
        catalog.settle_cost(bid, reservation)
        scope.check(app)
    monkeypatch.setattr("app.recap_warm.prepare", work)
    warmer = start(api, app)
    warmer.notify(bid, app.state.catalog.get_book(bid)["incarnation"])
    assert entered.wait(2)
    async def close():
        task = asyncio.create_task(warmer.close())
        await asyncio.sleep(0)
        assert warmer.closed and warmer.active_cancel.is_set()
        assert not task.done()
        release.set()
        await task
        await warmer.queue.join()
    try:
        api.portal.call(close)
        assert not app.state.catalog.get_cost_reservations(bid)
        assert app.state.catalog.get_costs(bid)[-1]["usd"] == .01
        assert not warmer.pending and not warmer.completed
    finally:
        release.set()
        app.state.recap_warmer = None


def test_checkpoint_file_cap_drops_plans_before_reviewed_answers(tmp_path):
    bank = AnswerBank()
    value = payload()
    value["citations"] = [{**value["citations"][0], "id": i, "excerpt": "a"*6000} for i in range(1, 7)]
    for i in range(70):
        bank.set(("b", "i", "s", f"Where was meeting {i}?"), value)
    bank.path = tmp_path / "answers.json"
    bank.set_plan(("b", "i", "s"), saved_plan(reviewed=True))
    assert bank.path.stat().st_size <= 2*1024*1024
    restored = AnswerBank(persist_path=bank.path)
    assert restored.rows and not restored.plans
