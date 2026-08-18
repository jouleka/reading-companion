"""Deterministic durable-worker process behavior (LIT-44)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.hosted.jobs import ClaimedJob, SanitizedFailure
from app.hosted.credentials import CredentialUnavailableError
from app.hosted.worker import DurableWorker, HandledJobFailure


def _claim() -> ClaimedJob:
    return ClaimedJob(
        owner_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        book_id=uuid.uuid4(),
        book_incarnation=uuid.uuid4(),
        kind="ingest_book",
        attempt_no=1,
        worker_id="worker",
        lease_token="private-token",
    )


@dataclass
class FakeRepository:
    claim: ClaimedJob | None
    started: bool = True
    calls: list[tuple] = field(default_factory=list)

    def claim_next(self, **kwargs):
        self.calls.append(("claim", kwargs))
        return self.claim

    def start(self, claim):
        self.calls.append(("start", claim.job_id))
        return self.started

    def succeed(self, claim):
        self.calls.append(("succeed", claim.job_id))

    def fail(self, claim, *, failure, retry_after):
        self.calls.append(("fail", failure, retry_after))


def test_worker_runs_claimed_job_to_success() -> None:
    repository = FakeRepository(_claim())
    handled = []
    worker = DurableWorker(repository, lambda claim, repo: handled.append(claim.job_id), "worker")
    assert worker.run_once()
    assert handled == [repository.claim.job_id]
    assert [call[0] for call in repository.calls] == ["claim", "start", "succeed"]


def test_worker_persists_only_reviewed_failure_classification() -> None:
    secret = "provider-key-and-private-chapter-text"
    repository = FakeRepository(_claim())

    def fail(_claim, _repository):
        raise RuntimeError(secret)

    assert DurableWorker(repository, fail, "worker").run_once()
    call = repository.calls[-1]
    assert call[0] == "fail"
    assert call[1] == SanitizedFailure("internal_error", retryable=True)
    assert secret not in repr(repository.calls)


def test_worker_honors_reviewed_retryability_and_cancelled_start() -> None:
    repository = FakeRepository(_claim())

    def fail(_claim, _repository):
        raise HandledJobFailure(SanitizedFailure("source_missing", retryable=False))

    assert DurableWorker(repository, fail, "worker").run_once()
    assert repository.calls[-1][1] == SanitizedFailure("source_missing", retryable=False)

    cancelled = FakeRepository(_claim(), started=False)
    assert DurableWorker(cancelled, fail, "worker").run_once()
    assert [call[0] for call in cancelled.calls] == ["claim", "start"]


def test_worker_classifies_unavailable_credential_without_leaking_detail() -> None:
    repository = FakeRepository(_claim())

    def fail(_claim, _repository):
        raise CredentialUnavailableError("private-key-canary")

    assert DurableWorker(repository, fail, "worker").run_once()
    assert repository.calls[-1][1] == SanitizedFailure("provider_rejected", retryable=False)
    assert "private-key-canary" not in repr(repository.calls)
