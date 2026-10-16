"""Prepared full answers must remain reviewed, frontier-bound, durable and nonblocking."""
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from test_views import env as env
from app.answer_bank import AnswerBank, answer_scope, valid_answer
from app.ingest.manifest import load_manifest
from app.llm import jev_tasks
from app.llm.jev import JevUnavailable
from app.reader_prepare import (
    GuardedCache, PreparationCatalog, PreparationScope, PreparationStale, preparation_key, prepare,
)
from app.recap_warm import RecapPrewarmer


def payload():
    return {"as_of_chapter": 2, "insufficient_evidence": False,
            "claims": [{"text": "Berenice met Aldric at the forge.", "citation_ids": [1]}],
            "citations": [{"id": 1, "ordinal": 2, "chapter_key": "two", "href": "two.xhtml",
                           "title": "Two", "excerpt": "Berenice met Aldric at the forge."}]}


def test_bank_restart_bounds_origin_and_corruption(tmp_path):
    path = tmp_path / "bank.json"
    bank = AnswerBank(max_entries=2, persist_path=path)
    scope = ("book", "incarnation", "snapshot")
    bank.set((*scope, "Where did Berenice meet Aldric?"), payload())
    bank.set((*scope, "Where was their meeting?"), payload(),
             reviewed_question="Where did Berenice meet Aldric?")
    bank.mark_prepared(scope)
    restarted = AnswerBank(max_entries=2, persist_path=path)
    assert restarted.prepared(scope)
    candidates = restarted.candidates(scope, "meeting")
    assert len(candidates) == 1
    assert candidates[0]["reviewed_question"] == "Where did Berenice meet Aldric?"
    candidates[0]["payload"]["claims"].clear()
    assert restarted.get((*scope, "Where was their meeting?"))["claims"]
    assert not restarted.candidates(("book", "incarnation", "other"), "meeting")
    restarted.invalidate_book("book")
    assert not AnswerBank(max_entries=2, persist_path=path).prepared(scope)
    path.write_text('{"version": "reviewed-answer-v2", "rows": [null]}')
    assert not AnswerBank(persist_path=path).rows


@pytest.mark.parametrize("fault", ["future", "missing", "uncited", "empty", "insufficient"])
def test_bank_rejects_invalid_answers(fault):
    value = payload()
    if fault == "future":
        value["citations"][0]["ordinal"] = 3
    if fault == "missing":
        del value["citations"][0]["excerpt"]
    if fault == "uncited":
        value["claims"][0]["citation_ids"] = [99]
    if fault == "empty":
        value["claims"] = []
    if fault == "insufficient":
        value["insufficient_evidence"] = True
    assert not valid_answer(value)
    bank = AnswerBank()
    bank.set(("b", "i", "s", "question"), value)
    assert not bank.rows


@pytest.mark.parametrize("fault", [None, "question", "coverage", "confidence", "probability", "missing"])
def test_both_independent_reuse_decisions_must_pass(fault):
    candidate = {"reviewed_question": "Where did Berenice meet Aldric?", "payload": payload()}
    before = copy.deepcopy(candidate)
    def run(state, questions):
        assert state["reader_question"] == "Where was Berenice's meeting with Aldric?"
        assert set(questions) == {"q0", "a0"}
        assert "candidates[0].complete_answer" in questions["a0"]["instructions"]
        answers = {k: {"choice": "yes", "probabilities": {"yes": 1.0}, "confidence": 1.0}
                   for k in questions}
        if fault in ("question", "coverage"):
            answers["q0" if fault == "question" else "a0"]["choice"] = "uncertain"
        if fault == "confidence":
            answers["a0"]["confidence"] = 0.89
        if fault == "probability":
            answers["q0"]["probabilities"]["yes"] = 0.98
        if fault == "missing":
            answers.pop("q0")
        return answers
    if fault:
        with pytest.raises(JevUnavailable):
            jev_tasks.reuse_answer("Where was Berenice's meeting with Aldric?", [candidate],
                                  run=run, settings=SimpleNamespace(
                                      reader_reuse_min_probability=.99, reader_reuse_min_confidence=.9))
    else:
        assert jev_tasks.reuse_answer("Where was Berenice's meeting with Aldric?", [candidate],
            run=run, settings=SimpleNamespace(reader_reuse_min_probability=.99,
                                             reader_reuse_min_confidence=.9)) is candidate
    assert candidate == before


def capture(app, bid):
    return PreparationScope.capture(app, bid, app.state.catalog.get_book(bid)["incarnation"])


@pytest.mark.parametrize("mutation", ["reset", "delete", "correction"])
def test_inflight_preparation_never_publishes_stale_answers(env, mutation):
    api, bid, app, _ = env
    scope = capture(app, bid)
    bank = GuardedCache(app.state.reader_client.answers, app, scope)
    if mutation == "reset":
        assert api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 0}).status_code == 200
    elif mutation == "delete":
        assert api.delete(f"/api/books/{bid}").status_code == 204
    else:
        with app.state.store.book(bid) as mem:
            mem.add_entity("Newly corrected identity", "character", revealed_at=2)
    with pytest.raises(PreparationStale):
        bank.set((*scope.answer_key, "Where did they meet?"), payload())
    assert not app.state.reader_client.answers.get((*scope.answer_key, "Where did they meet?"))


def test_preparation_budget_includes_pending_calls_and_checks_reset_atomically(env):
    api, bid, app, _ = env
    scope = capture(app, bid)
    catalog = PreparationCatalog(app, scope)
    app.state.settings.reader_prepare_max_usd_per_book = .10
    kwargs = dict(phase="questions", model="gpt-6-luna", input_tokens=10, output_tokens=10,
                  usd=.06, max_input_tokens=10**8, max_output_tokens=10**8, max_usd=10)
    reservation = catalog.reserve_cost(bid, **kwargs)
    with pytest.raises(RuntimeError, match="preparation cost ceiling"):
        catalog.reserve_cost(bid, **kwargs)
    catalog.settle_cost(bid, reservation, phase="questions-failed-reserved")
    assert app.state.catalog.get_costs(bid)[-1]["phase"] == "reader-prepare:questions-failed-reserved"
    with pytest.raises(RuntimeError, match="preparation cost ceiling"):
        catalog.reserve_cost(bid, **kwargs)
    api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 0})
    # Exercise the SQL recheck itself, without the proxy's earlier optimistic check.
    with pytest.raises(RuntimeError, match="scope is no longer current"):
        app.state.catalog.reserve_cost(bid, **{**kwargs, "phase": "reader-prepare:questions"},
            expected_reading_scope=(scope.incarnation, scope.epoch, scope.frontier),
            max_preparation_usd=.10)


def test_background_model_io_does_not_block_position_or_reset_and_coalesces(env, monkeypatch):
    api, bid, app, atoms = env
    entered, release = threading.Event(), threading.Event()
    scopes = []
    def blocked_prepare(_app, scope):
        scopes.append(scope)
        entered.set()
        assert release.wait(5)
        scope.check(app, snapshot=True)
    monkeypatch.setattr("app.recap_warm.prepare", blocked_prepare)
    app.state.settings.reader_prepare_delay_seconds = 0
    async def start():
        app.state.recap_warmer = RecapPrewarmer(app)
    api.portal.call(start)
    warmer = app.state.recap_warmer
    incarnation = app.state.catalog.get_book(bid)["incarnation"]
    warmer.notify(bid, incarnation)
    assert entered.wait(2)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            position = pool.submit(api.put, f"/api/books/{bid}/position", json={
                "cfi": "page-turn", "offset": sum(a["char_len"] for a in atoms[:2])+2,
                "position_epoch": 0}).result(timeout=1)
            assert position.status_code == 200
            for _ in range(30):
                warmer.notify(bid, incarnation)
            reset = pool.submit(api.post, f"/api/books/{bid}/position/reset",
                                json={"position_epoch": 0}).result(timeout=1)
            assert reset.status_code == 200
    finally:
        release.set()
        api.portal.call(warmer.queue.join)
        api.portal.call(warmer.close)
        app.state.recap_warmer = None
    assert len(scopes) == 1


def test_preparation_retries_once_for_new_frontier_arriving_during_active_work(env, monkeypatch):
    api, bid, app, _ = env
    entered, release = threading.Event(), threading.Event()
    calls = []
    def work(_app, scope):
        calls.append(scope.frontier)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
    monkeypatch.setattr("app.recap_warm.prepare", work)
    app.state.settings.reader_prepare_delay_seconds = 0
    async def start(): app.state.recap_warmer = RecapPrewarmer(app)
    api.portal.call(start)
    warmer = app.state.recap_warmer
    incarnation = app.state.catalog.get_book(bid)["incarnation"]
    warmer.notify(bid, incarnation)
    assert entered.wait(2)
    try:
        # A source correction at the same frontier must also schedule fresh work.
        warmer.notify(bid, incarnation, force=True)
        time.sleep(.02)
        release.set()
        api.portal.call(warmer.queue.join)
        assert calls == [2, 2]
        warmer.notify(bid, incarnation)
        api.portal.call(warmer.queue.join)
        assert calls == [2, 2]
    finally:
        release.set()
        api.portal.call(warmer.close)
        app.state.recap_warmer = None


def test_preparation_uses_only_completed_facts_and_normal_reviewed_answer_route(env, monkeypatch):
    _, bid, app, _ = env
    scope = capture(app, bid)
    app.state.settings.reader_prepare_enabled = True
    app.state.settings.reader_answer_cache = True
    app.state.settings.reader_prepare_max_questions = 2
    questions, inputs = [], []
    def plan(*args, **kwargs):
        inputs.append(json.loads(kwargs["user"]))
        if kwargs["schema"].__name__ == "PreparationReview":
            return SimpleNamespace(value={"audits": [{"question_id": c["id"],
                "safe_to_prepare": True, "basis_ids": [1]} for c in inputs[-1]["questions"]]})
        return SimpleNamespace(value={"questions": [{"question": q, "kind": "character",
            "basis_ids": [1], "follow_up_to": None} for q in ["Who is Berenice?", "Who is Berenice?",
                                                     "Where did Berenice meet Aldric?"]]})
    def reviewed(book_id, body, catalog, store, settings, client, worker):
        assert client.preparing and book_id == bid and body.bookmark == 2
        questions.append(body.question)
        client.answers.set((*scope.answer_key, body.question), payload())
        return payload()
    monkeypatch.setattr("app.reader_prepare.budgeted_completion", plan)
    monkeypatch.setattr("app.api.views.ask_the_book", reviewed)
    prepare(app, scope)
    prepare(app, scope)
    assert len(inputs) == 2 and len(questions) == 2
    assert "Corvus" not in json.dumps(inputs)
    assert app.state.reader_client.answers.prepared(preparation_key(scope, app.state.settings))
    assert len(app.state.reader_client.answers.candidates(scope.answer_key, "Berenice")) == 2


def test_semantic_reuse_is_unchanged_and_never_considers_future_or_retracted_scope(env, monkeypatch):
    api, bid, app, _ = env
    settings, client = app.state.settings, app.state.reader_client
    settings.reader_answer_cache = settings.reader_reuse_enabled = True
    client.jev = SimpleNamespace(model="jev-1.13.0", close=lambda: None)
    scope = answer_scope(bid, 2, app.state.catalog.get_state(bid),
        load_manifest(settings.data_dir, bid), catalog=app.state.catalog,
        store=app.state.store, settings=settings, client=client)
    original = "Where did Berenice meet Aldric?"
    client.answers.set((*scope, original), payload())
    client.answers.mark_suggestion((*scope, original))
    offered = api.get(f"/api/books/{bid}/prepared-questions").json()
    assert offered["questions"] == [original]
    assert api.get(f"/api/books/{bid}/prepared-questions?bookmark=1").json()["questions"] == []
    calls = []
    def choose(question, candidates, **kwargs):
        calls.append(question)
        assert len(candidates) == 1 and candidates[0]["reviewed_question"] == original
        return candidates[0]
    monkeypatch.setattr(jev_tasks, "reuse_answer", choose)
    new = "Where was Berenice's meeting with Aldric?"
    value = api.post(f"/api/books/{bid}/ask", json={"question": new}).json()
    assert value["cached"] and value["claims"] == payload()["claims"]
    assert value["citations"] == payload()["citations"]
    assert api.post(f"/api/books/{bid}/ask", json={"question": new}).json()["cached"]
    assert calls == [new]
    api.post(f"/api/books/{bid}/ask", json={"question": new, "bookmark": 1})
    assert calls == [new]  # No future candidate enters Jev's state.
    with app.state.store.book(bid) as mem:
        mem.add_entity("Corrected identity", "character", revealed_at=2)
    assert api.get(f"/api/books/{bid}/prepared-questions").json()["questions"] == []
    api.post(f"/api/books/{bid}/ask", json={"question": original})
    assert calls == [new]


@pytest.mark.parametrize("failure", ["uncertain", "outage"])
def test_reuse_failure_runs_full_reviewed_answer_path_instead_of_returning_a_related_answer(env, monkeypatch, failure):
    api, bid, app, _ = env
    settings, client = app.state.settings, app.state.reader_client
    settings.reader_answer_cache = settings.reader_reuse_enabled = True
    client.jev = SimpleNamespace(model="jev-1.13.0", close=lambda: None)
    scope = capture(app, bid)
    client.answers.set((*scope.answer_key, "Where did Berenice meet Aldric?"), payload())
    calls = []
    original = client.complete
    def complete(*args, **kwargs):
        calls.append(getattr(kwargs.get("schema"), "__name__", ""))
        return original(*args, **kwargs)
    def reject(*args, **kwargs):
        raise JevUnavailable(failure)
    monkeypatch.setattr(jev_tasks, "reuse_answer", reject)
    monkeypatch.setattr(client, "complete", complete)
    value = api.post(f"/api/books/{bid}/ask", json={
        "question": "Who owned a silver compass?"}).json()
    assert not value.get("cached", False) and value["insufficient_evidence"]
    assert value["claims"] == [] and calls
