"""Real-PostgreSQL durable job/lease/idempotency contract (LIT-44)."""

from __future__ import annotations

import concurrent.futures
import uuid
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import psycopg
import pytest
from psycopg import conninfo, sql

from app.hosted.jobs import (
    JobIdempotencyError,
    LostLeaseError,
    PostgresWorkerRepository,
    SanitizedFailure,
    WorkerConfigurationError,
)
from app.hosted.credentials import CredentialCipher, CredentialUnavailableError
from app.hosted.limits import LimitExceededError, update_limits
from app.hosted.tenant.models import OwnerId
from app.hosted.migrations import apply_migrations

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def admin_dsn() -> str:
    import os

    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for the real PostgreSQL suite")
    return dsn


@pytest.fixture()
def database(admin_dsn: str):
    database_name = f"lit44_{uuid.uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    dsn = conninfo.make_conninfo(admin_dsn, dbname=database_name)
    apply_migrations(dsn)
    try:
        yield dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=%s AND pid<>pg_backend_pid()",
                (database_name,),
            )
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))


@pytest.fixture()
def worker_dsn(database: str, admin_dsn: str):
    role = f"lit44_worker_{uuid.uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOINHERIT BYPASSRLS "
                "NOCREATEDB NOCREATEROLE NOREPLICATION"
            ).format(sql.Identifier(role))
        )
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        conn.execute(
            sql.SQL(
                "GRANT SELECT,UPDATE ON jobs TO {}; "
                "GRANT SELECT,INSERT,UPDATE ON job_attempts TO {}; "
                "GRANT SELECT,INSERT ON chapters TO {}; "
                "GRANT INSERT ON chapter_summaries TO {}; "
                "GRANT SELECT,INSERT ON cost_ledger TO {}; "
                "GRANT SELECT,INSERT ON ingested_chapters TO {}"
                "; GRANT SELECT ON provider_credentials,provider_model_settings TO {}"
                "; GRANT SELECT ON owner_limits TO {}"
                "; GRANT SELECT,INSERT,UPDATE ON cost_reservations TO {}"
            ).format(*(sql.Identifier(role) for _ in range(9)))
        )
        conn.execute(sql.SQL("GRANT INSERT ON audit_events TO {}").format(sql.Identifier(role)))
    runtime_dsn = conninfo.make_conninfo(database, user=role)
    try:
        yield runtime_dsn
    finally:
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@pytest.fixture()
def corpus(database: str) -> dict:
    owner_a = uuid.uuid4()
    owner_b = uuid.uuid4()
    book_a = uuid.uuid4()
    book_b = uuid.uuid4()
    incarnation_a = uuid.uuid4()
    incarnation_b = uuid.uuid4()
    with psycopg.connect(database) as conn:
        conn.cursor().executemany(
            "INSERT INTO users (id,display_name) VALUES (%s,%s)",
            [(owner_a, "Owner A"), (owner_b, "Owner B")],
        )
        conn.cursor().executemany(
            """
            INSERT INTO books (owner_id,id,incarnation,title,schema_version)
            VALUES (%s,%s,%s,%s,1)
            """,
            [
                (owner_a, book_a, incarnation_a, "A book"),
                (owner_b, book_b, incarnation_b, "B book"),
            ],
        )
    return {
        "owner_a": owner_a,
        "owner_b": owner_b,
        "book_a": book_a,
        "book_b": book_b,
        "incarnation_a": incarnation_a,
        "incarnation_b": incarnation_b,
    }


def _enqueue(
    database: str,
    corpus: dict,
    *,
    owner: str = "a",
    max_attempts: int = 3,
    suffix: str | None = None,
    credential_id: uuid.UUID | None = None,
) -> uuid.UUID:
    job_id = uuid.uuid4()
    suffix = suffix or job_id.hex
    with psycopg.connect(database) as conn:
        conn.execute(
            """
            INSERT INTO jobs
              (owner_id,id,book_id,book_incarnation,kind,idempotency_key,max_attempts,
               payload_metadata,credential_id)
            VALUES (%s,%s,%s,%s,'ingest_book',%s,%s,'{"chapter_count":1}'::jsonb,%s)
            """,
            (
                corpus[f"owner_{owner}"],
                job_id,
                corpus[f"book_{owner}"],
                corpus[f"incarnation_{owner}"],
                f"ingest:{suffix}",
                max_attempts,
                credential_id,
            ),
        )
    return job_id


def test_concurrent_claim_is_single_winner_and_lease_token_is_only_digest(
    database: str, corpus: dict
) -> None:
    job_id = _enqueue(database, corpus)
    repository = PostgresWorkerRepository(database)

    def claim(worker: str):
        return repository.claim_next(worker_id=worker, lease_for=timedelta(seconds=30))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("worker-a", "worker-b")))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    claimed = winners[0]
    assert claimed.job_id == job_id
    assert claimed.owner_id == corpus["owner_a"]
    assert claimed.attempt_no == 1
    assert "lease_token" not in repr(claimed)

    with psycopg.connect(database) as conn:
        row = conn.execute(
            """
            SELECT job.state,job.attempt_count,octet_length(attempt.lease_token_digest),
                   attempt.worker_id
            FROM jobs AS job JOIN job_attempts AS attempt
              ON (attempt.owner_id,attempt.job_id)=(job.owner_id,job.id)
            WHERE job.owner_id=%s AND job.id=%s
            """,
            (corpus["owner_a"], job_id),
        ).fetchone()
    assert row == ("leased", 1, 32, claimed.worker_id)


def test_worker_claim_is_owner_and_book_fenced_against_foreign_identifiers(
    worker_dsn: str, database: str, corpus: dict
) -> None:
    job_id = _enqueue(database, corpus, suffix="owner-fence")
    repository = PostgresWorkerRepository(worker_dsn)
    claim = repository.claim_next(worker_id="fence-worker", lease_for=timedelta(seconds=30))
    assert claim is not None and claim.job_id == job_id

    foreign_owner_claim = replace(claim, owner_id=corpus["owner_b"])
    for operation in (
        lambda: repository.start(foreign_owner_claim),
        lambda: repository.heartbeat(
            foreign_owner_claim, lease_for=timedelta(seconds=30)
        ),
        lambda: repository.reserve_spend(
            foreign_owner_claim,
            phase="extraction",
            provider="test-provider",
            model="test-model",
            reserved_input_tokens=1,
            reserved_output_tokens=1,
            reserved_usd="0.001",
            idempotency_key="foreign-owner-reservation",
        ),
        lambda: repository.fail(
            foreign_owner_claim,
            failure=SanitizedFailure("internal_error", retryable=False),
            retry_after=timedelta(0),
        ),
        lambda: repository.succeed(foreign_owner_claim),
    ):
        with pytest.raises(LostLeaseError):
            operation()

    assert repository.start(claim)
    with pytest.raises(JobIdempotencyError, match="claimed book"):
        repository.commit_chapter(
            claim,
            book_id=corpus["book_b"],
            book_incarnation=corpus["incarnation_b"],
            chapter_id=uuid.uuid4(),
            chapter_key="foreign-book",
            ordinal=1,
            content_hash="9" * 64,
            summary="must not commit",
            extractor_version="test-v1",
            provider="test-provider",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            usd="0.001",
            cost_idempotency_key="foreign-book-commit",
        )

    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT state FROM jobs WHERE owner_id=%s AND id=%s",
            (corpus["owner_a"], job_id),
        ).fetchone() == ("running",)
        assert conn.execute(
            "SELECT count(*) FROM ingested_chapters WHERE owner_id=%s AND book_id=%s",
            (corpus["owner_b"], corpus["book_b"]),
        ).fetchone() == (0,)


def test_worker_role_is_bypassrls_and_exactly_privileged(
    worker_dsn: str, database: str, corpus: dict
) -> None:
    repository = PostgresWorkerRepository(worker_dsn)
    repository.check_runtime_role()
    job_id = _enqueue(database, corpus)
    claim = repository.claim_next(worker_id="isolated-worker", lease_for=timedelta(seconds=30))
    assert claim is not None and claim.job_id == job_id
    assert repository.start(claim)
    assert repository.commit_chapter(
        claim,
        book_id=corpus["book_a"],
        book_incarnation=corpus["incarnation_a"],
        chapter_id=uuid.uuid4(),
        chapter_key="role-proof-chapter",
        ordinal=1,
        content_hash="d" * 64,
        summary="Role proof summary",
        extractor_version="test-v1",
        provider="test-provider",
        model="test-model",
        input_tokens=1,
        output_tokens=1,
        usd="0.0001",
        cost_idempotency_key=f"job:{job_id}:role-proof",
    )
    assert repository.succeed(claim)

    role = conninfo.conninfo_to_dict(worker_dsn)["user"]
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute(sql.SQL("GRANT SELECT ON books TO {}").format(sql.Identifier(role)))
    try:
        with pytest.raises(WorkerConfigurationError, match="allow-list"):
            repository.check_runtime_role()
    finally:
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL("REVOKE SELECT ON books FROM {}").format(sql.Identifier(role)))


def test_provider_concurrency_is_atomic_across_worker_instances(
    worker_dsn: str, database: str, corpus: dict
) -> None:
    update_limits(database, corpus["owner_a"], {"max_provider_concurrency": 1})
    jobs_a = [_enqueue(database, corpus, suffix=f"limit-a-{index}") for index in range(2)]
    job_b = _enqueue(database, corpus, owner="b", suffix="limit-b")

    def claim(worker: str):
        return PostgresWorkerRepository(worker_dsn).claim_next(
            worker_id=worker, lease_for=timedelta(seconds=30)
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        claims = list(pool.map(claim, ("limit-worker-1", "limit-worker-2", "limit-worker-3")))
    claimed = [value for value in claims if value is not None]
    assert len(claimed) == 2
    assert {value.owner_id for value in claimed} == {corpus["owner_a"], corpus["owner_b"]}
    assert sum(value.job_id in jobs_a for value in claimed) == 1
    assert any(value.job_id == job_b for value in claimed)


def test_optional_spend_ceiling_requires_atomic_preflight_reservations(
    worker_dsn: str, database: str, corpus: dict
) -> None:
    update_limits(database, corpus["owner_a"], {"max_spend_usd": Decimal("0.10")})
    job_id = _enqueue(database, corpus, suffix="spend-limit")
    repository = PostgresWorkerRepository(worker_dsn)
    claim = repository.claim_next(worker_id="spend-worker", lease_for=timedelta(seconds=30))
    assert claim is not None and claim.job_id == job_id
    assert repository.start(claim)
    reservation_id = repository.reserve_spend(
        claim,
        phase="extraction",
        provider="test-provider",
        model="test-model",
        reserved_input_tokens=100,
        reserved_output_tokens=20,
        reserved_usd="0.08",
        idempotency_key="spend:chapter-1",
    )
    assert repository.reserve_spend(
        claim,
        phase="extraction",
        provider="test-provider",
        model="test-model",
        reserved_input_tokens=100,
        reserved_output_tokens=20,
        reserved_usd="0.08",
        idempotency_key="spend:chapter-1",
    ) == reservation_id
    with pytest.raises(LimitExceededError, match="spend_limit_exceeded"):
        repository.reserve_spend(
            claim,
            phase="extraction",
            provider="test-provider",
            model="test-model",
            reserved_input_tokens=10,
            reserved_output_tokens=2,
            reserved_usd="0.03",
            idempotency_key="spend:chapter-2",
        )
    assert repository.commit_chapter(
        claim,
        book_id=corpus["book_a"],
        book_incarnation=corpus["incarnation_a"],
        chapter_id=uuid.uuid4(),
        chapter_key="spend-proof-chapter",
        ordinal=1,
        content_hash="e" * 64,
        summary="Spend proof summary",
        extractor_version="test-v1",
        provider="test-provider",
        model="test-model",
        input_tokens=90,
        output_tokens=15,
        usd="0.07",
        cost_idempotency_key="spend:chapter-1",
    )
    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT state,actual_usd FROM cost_reservations WHERE owner_id=%s AND id=%s",
            (corpus["owner_a"], reservation_id),
        ).fetchone() == ("settled", Decimal("0.0700000000"))

    no_reservation_job = _enqueue(database, corpus, suffix="spend-no-reservation")
    # Finish the first claim so the per-owner concurrency slot is available.
    assert repository.succeed(claim)
    second = repository.claim_next(
        worker_id="spend-worker-2", lease_for=timedelta(seconds=30)
    )
    assert second is not None and second.job_id == no_reservation_job
    assert repository.start(second)
    with pytest.raises(LimitExceededError, match="spend_reservation_required"):
        repository.commit_chapter(
            second,
            book_id=corpus["book_a"],
            book_incarnation=corpus["incarnation_a"],
            chapter_id=uuid.uuid4(),
            chapter_key="unreserved-chapter",
            ordinal=2,
            content_hash="f" * 64,
            summary="Must not commit",
            extractor_version="test-v1",
            provider="test-provider",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            usd="0.001",
            cost_idempotency_key="spend:missing",
        )


def test_worker_resolves_only_claim_bound_owner_credential_just_in_time(
    worker_dsn: str, database: str, corpus: dict
) -> None:
    cipher = CredentialCipher({"test-v1": b"c" * 32}, active_version="test-v1")
    credential_id = uuid.uuid4()
    envelope = cipher.encrypt(
        OwnerId(corpus["owner_a"]), credential_id, "anthropic", "worker-private-canary"
    )
    with psycopg.connect(database) as conn:
        conn.execute(
            """
            INSERT INTO provider_credentials
              (owner_id,id,provider,masked_label,ciphertext,encrypted_data_key,
               encryption_algorithm,key_version,nonce)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                envelope.owner_id,
                envelope.credential_id,
                envelope.provider,
                envelope.masked_label,
                envelope.ciphertext,
                envelope.encrypted_data_key,
                envelope.encryption_algorithm,
                envelope.key_version,
                envelope.nonce,
            ),
        )
        conn.execute(
            """
            INSERT INTO provider_model_settings
              (owner_id,id,provider,capability,credential_id,model,base_url,
               validation_status,validated_at)
            VALUES (%s,%s,'anthropic','extraction',%s,'available-model',
                    'https://api.anthropic.com/v1','ready',now())
            """,
            (corpus["owner_a"], uuid.uuid4(), credential_id),
        )
    job_id = _enqueue(database, corpus, credential_id=credential_id)
    repository = PostgresWorkerRepository(worker_dsn, cipher)
    claim = repository.claim_next(worker_id="credential-worker", lease_for=timedelta(seconds=30))
    assert claim is not None and claim.job_id == job_id and claim.credential_id == credential_id
    assert repository.start(claim)
    with repository.resolve_credential(claim) as resolved:
        assert resolved.provider == "anthropic"
        assert resolved.get_secret_value() == "worker-private-canary"
        assert "worker-private-canary" not in repr(resolved)
    with pytest.raises(CredentialUnavailableError):
        resolved.get_secret_value()

    with pytest.raises(CredentialUnavailableError):
        repository.resolve_credential(replace(claim, credential_id=uuid.uuid4()))
    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE provider_credentials SET disabled_at=now() WHERE owner_id=%s AND id=%s",
            (corpus["owner_a"], credential_id),
        )
    with pytest.raises(CredentialUnavailableError):
        repository.resolve_credential(claim)


@pytest.mark.parametrize(
    "payload",
    ({}, {"chapter_count": 1, "token": "must-not-persist"}),
)
def test_ingestion_job_payload_requires_progress_and_rejects_credentials(
    database: str, corpus: dict, payload: dict
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        with psycopg.connect(database) as conn:
            conn.execute(
                """
                INSERT INTO jobs
                  (owner_id,id,book_id,book_incarnation,kind,idempotency_key,payload_metadata)
                VALUES (%s,%s,%s,%s,'ingest_book',%s,%s::jsonb)
                """,
                (
                    corpus["owner_a"],
                    uuid.uuid4(),
                    corpus["book_a"],
                    corpus["incarnation_a"],
                    f"ingest:{uuid.uuid4().hex}",
                    psycopg.types.json.Jsonb(payload),
                ),
            )


def test_database_rejects_unreviewed_failure_codes(database: str, corpus: dict) -> None:
    job_id = _enqueue(database, corpus)
    with pytest.raises(psycopg.errors.CheckViolation):
        with psycopg.connect(database) as conn:
            conn.execute(
                """
                UPDATE jobs SET sanitized_error=
                  '{"code":"private_chapter_text","retryable":false}'::jsonb
                WHERE owner_id=%s AND id=%s
                """,
                (corpus["owner_a"], job_id),
            )


def test_expired_lease_is_recovered_and_attempt_budget_is_bounded(
    database: str, corpus: dict
) -> None:
    job_id = _enqueue(database, corpus, max_attempts=2)
    repository = PostgresWorkerRepository(database)
    first = repository.claim_next(worker_id="worker-one", lease_for=timedelta(seconds=30))
    assert first is not None
    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE job_attempts SET leased_at=now()-interval '2 seconds', "
            "lease_expires_at=now()-interval '1 second' "
            "WHERE owner_id=%s AND job_id=%s",
            (corpus["owner_a"], job_id),
        )
    second = repository.claim_next(worker_id="worker-two", lease_for=timedelta(seconds=30))
    assert second is not None
    assert second.job_id == job_id and second.attempt_no == 2
    with pytest.raises(LostLeaseError):
        repository.start(first)
    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT outcome FROM job_attempts WHERE owner_id=%s AND job_id=%s ORDER BY attempt_no",
            (corpus["owner_a"], job_id),
        ).fetchall() == [("expired",), (None,)]

    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE job_attempts SET leased_at=now()-interval '2 seconds', "
            "lease_expires_at=now()-interval '1 second' "
            "WHERE owner_id=%s AND job_id=%s AND attempt_no=2",
            (corpus["owner_a"], job_id),
        )
    assert repository.claim_next(
        worker_id="worker-three", lease_for=timedelta(seconds=30)
    ) is None
    with psycopg.connect(database) as conn:
        state, attempts, error = conn.execute(
            "SELECT state,attempt_count,sanitized_error FROM jobs WHERE owner_id=%s AND id=%s",
            (corpus["owner_a"], job_id),
        ).fetchone()
    assert state == "failed" and attempts == 2
    assert error == {"code": "attempts_exhausted", "retryable": False}


def test_start_heartbeat_failure_retry_and_sanitized_terminal_error(
    database: str, corpus: dict
) -> None:
    job_id = _enqueue(database, corpus, max_attempts=2)
    repository = PostgresWorkerRepository(database)
    claim = repository.claim_next(worker_id="worker-one", lease_for=timedelta(seconds=30))
    assert claim is not None
    assert repository.start(claim)
    assert not repository.heartbeat(claim, lease_for=timedelta(seconds=30))
    secret = "private chapter text and provider credential"
    repository.fail(
        claim,
        failure=SanitizedFailure("provider_unavailable", retryable=True),
        retry_after=timedelta(0),
    )

    retry = repository.claim_next(worker_id="worker-two", lease_for=timedelta(seconds=30))
    assert retry is not None and retry.attempt_no == 2
    assert repository.start(retry)
    repository.fail(
        retry,
        failure=SanitizedFailure("internal_error", retryable=True),
        retry_after=timedelta(0),
    )
    with psycopg.connect(database) as conn:
        state, error = conn.execute(
            "SELECT state,sanitized_error FROM jobs WHERE owner_id=%s AND id=%s",
            (corpus["owner_a"], job_id),
        ).fetchone()
        rendered = str(conn.execute("SELECT sanitized_error FROM jobs").fetchall())
        audit = conn.execute(
            "SELECT action,result,metadata FROM audit_events WHERE owner_id=%s AND target_id=%s "
            "ORDER BY occurred_at,id",
            (corpus["owner_a"], job_id),
        ).fetchall()
    assert state == "failed"
    assert error == {"code": "internal_error", "retryable": True}
    assert secret not in rendered
    assert [row[0] for row in audit] == [
        "job.claim",
        "job.start",
        "job.fail",
        "job.claim",
        "job.start",
        "job.fail",
    ]
    assert audit[-1][1:] == ("failed", {"reason_code": "internal_error"})


def test_pending_and_running_cancellation_are_cooperative(database: str, corpus: dict) -> None:
    pending_id = _enqueue(database, corpus, suffix="pending")
    running_id = _enqueue(database, corpus, suffix="running")
    repository = PostgresWorkerRepository(database)
    running = repository.claim_next(worker_id="worker", lease_for=timedelta(seconds=30))
    assert running is not None and running.job_id in {pending_id, running_id}
    other = running_id if running.job_id == pending_id else pending_id

    with psycopg.connect(database) as conn:
        conn.execute(
            """
            UPDATE jobs SET state='cancelled',cancellation_requested_at=now(),completed_at=now()
            WHERE owner_id=%s AND id=%s AND state='pending'
            """,
            (corpus["owner_a"], other),
        )
        conn.execute(
            "UPDATE jobs SET cancellation_requested_at=now() WHERE owner_id=%s AND id=%s",
            (corpus["owner_a"], running.job_id),
        )
    assert not repository.start(running)
    assert repository.claim_next(
        worker_id="worker-next", lease_for=timedelta(seconds=30)
    ) is None
    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT state FROM jobs WHERE owner_id=%s ORDER BY id",
            (corpus["owner_a"],),
        ).fetchall() == [("cancelled",), ("cancelled",)]


def test_chapter_memory_receipt_and_cost_commit_is_atomic_and_idempotent(
    database: str, corpus: dict
) -> None:
    job_id = _enqueue(database, corpus)
    repository = PostgresWorkerRepository(database)
    claim = repository.claim_next(worker_id="worker", lease_for=timedelta(seconds=30))
    assert claim is not None and repository.start(claim)
    chapter_id = uuid.uuid4()
    model_payload_canary = "MODEL_PAYLOAD_PRIVATE_CANARY_H6s3"
    values = dict(
        book_id=corpus["book_a"],
        book_incarnation=corpus["incarnation_a"],
        chapter_id=chapter_id,
        chapter_key="chapter-1",
        ordinal=1,
        content_hash="a" * 64,
        summary=model_payload_canary,
        extractor_version="test-v1",
        provider="test-provider",
        model="test-model",
        input_tokens=10,
        output_tokens=2,
        usd="0.001",
        cost_idempotency_key=f"job:{job_id}:chapter:1:extract",
    )
    assert repository.commit_chapter(claim, **values)
    assert not repository.commit_chapter(claim, **values)
    assert repository.succeed(claim)

    with psycopg.connect(database) as conn:
        counts = tuple(
            conn.execute(
                f"SELECT count(*) FROM {table} WHERE owner_id=%s AND book_id=%s",
                (corpus["owner_a"], corpus["book_a"]),
            ).fetchone()[0]
            for table in ("chapters", "chapter_summaries", "ingested_chapters", "cost_ledger")
        )
        audit = conn.execute(
            "SELECT actor_kind,action,target_kind,result,metadata FROM audit_events "
            "WHERE owner_id=%s ORDER BY occurred_at,id",
            (corpus["owner_a"],),
        ).fetchall()
    assert counts == (1, 1, 1, 1)
    assert [row[1] for row in audit] == [
        "job.claim",
        "job.start",
        "chapter.commit",
        "job.succeed",
    ]
    assert all(row[0] == "worker" and row[2] in {"job", "chapter"} for row in audit)
    assert model_payload_canary not in repr(audit)
