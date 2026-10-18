"""Recap proof concurrency joins every mandatory audit; provider calls are entirely mocked."""
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest

from app import recap_grounding as module
from app.ask import AskDraft
from app.eval.spoiler_gate.judge import JudgeUnavailable
from test_views import _supported_citation_audit


def _proof_inputs(monkeypatch, count):
    sentences = [f"Reader{i} carried parcel{i}." for i in range(count)]
    draft = AskDraft.model_validate({"insufficient_evidence": False, "claims": [
        {"text": sentence, "citation_ids": [index]}
        for index, sentence in enumerate(sentences, 1)
    ]})
    sources = [{"id": index, "ordinal": index, "chapter_key": str(index),
                "href": f"chapter{index}.xhtml", "title": f"Chapter {index}",
                "text": sentence, "recap_claim_ids": [index]}
               for index, sentence in enumerate(sentences, 1)]
    original = deepcopy((draft.model_dump(mode="json"), sources))
    monkeypatch.setattr(module, "recap_evidence", lambda *args, **kwargs: (draft, sources))
    # One original unit per batch forces independent audits without changing production batching.
    args = dict(catalog=None, settings=SimpleNamespace(cost_max_output_tokens_per_call=512),
                client=SimpleNamespace(preparing=True), book_id="book")

    def review():
        return module.review_primary_recap(" ".join(sentences), ([], {}), count, {}, **args)

    def unchanged():
        assert (draft.model_dump(mode="json"), sources) == original

    return sentences, review, unchanged


def _audit_data(kwargs):
    return json.loads(kwargs["user"].split("(untrusted JSON data):\n")[1])


def _supported(data):
    return SimpleNamespace(value=_supported_citation_audit(
        data, [data["sources"][0]["source_id"]],
    ))


def _verdict():
    return SimpleNamespace(value={"references_future": False, "unsupported_claims": [],
                                  "missing_details": []})


def test_independent_proofs_start_together_finish_out_of_order_and_join_before_judge(monkeypatch):
    sentences, review, unchanged = _proof_inputs(monkeypatch, 3)
    entered = [threading.Event() for _ in sentences]
    releases = [threading.Event() for _ in sentences]
    finished = [threading.Event() for _ in sentences]
    judge_called = threading.Event()
    completion_order, judged = [], []
    worker_ids = set()
    lock = threading.Lock()

    def complete(*args, **kwargs):
        data = _audit_data(kwargs)
        if kwargs["schema"].__name__ == "AskEvidenceReview":
            assert len(data["claims"]) == 1
            index = sentences.index(data["claims"][0]["text"])
            with lock:
                worker_ids.add(threading.get_ident())
            entered[index].set()
            assert releases[index].wait(3), "the independent proof was not released"
            with lock:
                completion_order.append(index)
            finished[index].set()
            return _supported(data)
        assert all(event.is_set() for event in finished), "final judge ran before all proofs joined"
        judged.extend(claim["claim"] for claim in data["claims"])
        for index, claim in enumerate(data["claims"], 1):
            assert [source["completed_chapter"] for source in claim["cited_passages"]] == [index]
        judge_called.set()
        return _verdict()

    monkeypatch.setattr(module, "_complete", complete)
    with ThreadPoolExecutor(max_workers=1) as caller:
        result = caller.submit(review)
        try:
            assert all(event.wait(2) for event in entered), "proof batches still ran serially"
            assert len(worker_ids) == 3 and not judge_called.is_set()
            for index in (2, 0):
                releases[index].set()
                assert finished[index].wait(2)
            assert not result.done() and not judge_called.is_set()
            releases[1].set()
            now, _sources = result.result(timeout=3)
        finally:
            for event in releases:
                event.set()
    assert completion_order == [2, 0, 1]
    assert judged == sentences and judge_called.is_set()
    assert now == sentences[-1]
    unchanged()


@pytest.mark.parametrize("failure_index", [0, 2])
def test_proof_failure_cancels_queued_batches_and_joins_running_calls_with_at_most_three_workers(
    monkeypatch, failure_index,
):
    sentences, review, unchanged = _proof_inputs(monkeypatch, 6)
    three_running = threading.Event()
    release_failure, release_running = threading.Event(), threading.Event()
    cancellation_seen, judge_called = threading.Event(), threading.Event()
    started, finished = set(), set()
    active = maximum = 0
    lock = threading.Lock()
    original_cancel = Future.cancel

    def record_cancel(future):
        cancelled = original_cancel(future)
        if cancelled:
            cancellation_seen.set()
        return cancelled

    monkeypatch.setattr(Future, "cancel", record_cancel)

    def complete(*args, **kwargs):
        nonlocal active, maximum
        data = _audit_data(kwargs)
        if kwargs["schema"].__name__ != "AskEvidenceReview":
            judge_called.set()
            return _verdict()
        index = sentences.index(data["claims"][0]["text"])
        with lock:
            active += 1
            maximum = max(maximum, active)
            started.add(index)
            if len(started) == 3:
                three_running.set()
        try:
            if index == failure_index:
                assert release_failure.wait(3)
                reply = _supported(data)
                reply.value["claim_audits"][0]["units"][0].update(
                    supported=False, evidence=[], diagnostic="missing source proof",
                )
                return reply
            assert release_running.wait(3), "running proof was abandoned after another batch failed"
            return _supported(data)
        finally:
            with lock:
                active -= 1
                finished.add(index)

    monkeypatch.setattr(module, "_complete", complete)
    with ThreadPoolExecutor(max_workers=1) as caller:
        result = caller.submit(review)
        try:
            assert three_running.wait(2), "three proof batches did not start independently"
            with lock:
                assert active == maximum == 3 and len(started) == 3
            release_failure.set()
            assert cancellation_seen.wait(2), "queued paid work was not cancelled after proof failure"
            assert not result.done(), "a proof request escaped while other provider calls still ran"
            assert not judge_called.is_set()
            release_running.set()
            with pytest.raises(JudgeUnavailable):
                result.result(timeout=3)
        finally:
            release_failure.set()
            release_running.set()
    with lock:
        assert maximum == 3 and active == 0
        assert started == finished and len(started) < len(sentences)
    assert not judge_called.is_set()
    unchanged()


def test_single_proof_batch_runs_directly_without_a_worker_thread(monkeypatch):
    sentences, review, unchanged = _proof_inputs(monkeypatch, 1)
    caller_id = threading.get_ident()
    calls = []

    def complete(*args, **kwargs):
        assert threading.get_ident() == caller_id
        calls.append(kwargs["schema"].__name__)
        return _supported(_audit_data(kwargs)) if len(calls) == 1 else _verdict()

    monkeypatch.setattr(module, "_complete", complete)
    now, _sources = review()
    assert calls == ["AskEvidenceReview", "AskVerdict"] and now == sentences[0]
    unchanged()
