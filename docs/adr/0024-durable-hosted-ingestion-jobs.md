# ADR 0024 - Durable hosted ingestion jobs and worker leases

**Status:** Accepted (2026-07-17; LIT-44)

## Context

The local application uses an in-process executor. That is not a hosted durability boundary: process
loss can forget work, two replicas can perform the same provider calls, and an HTTP process cannot
safely own a long-running cross-tenant queue. The hosted schema already contained `jobs` and
`job_attempts`, but nothing claimed, recovered, observed, or cancelled them.

Source upload from LIT-43 is the first hosted operation that needs ingestion. The job must be created
with the book metadata, while execution must use a separate, narrowly privileged process. A retry may
repeat computation, but it must not duplicate committed chapter memory or provider cost.

## Decision

### Upload and enqueue are one database transaction

`POST /api/books` stores and verifies the EPUB, then one owner-scoped PostgreSQL transaction creates
the book, reading state, source-object metadata, and one `ingest_book` job. Its idempotency key binds
the owner, book, and incarnation. Payload metadata contains only the bounded chapter count; schema
constraints reject credential-shaped keys. Provider credentials, raw chapter text, object keys, and
lease material never enter the job payload or user response.

### Claims are leased, fenced, and recoverable

The separate worker repository performs a global priority/run-time claim with
`FOR UPDATE SKIP LOCKED`. It increments the durable attempt count and stores only SHA-256 of a random
lease token. Every later mutation supplies owner, job, attempt, worker, and token digest while locking
both job and attempt. A stale or expired lease cannot start, heartbeat, commit, fail, or succeed.

Expired active attempts become `expired`. A cancelled job becomes terminal; an exhausted job fails
with the fixed `attempts_exhausted` code; otherwise it returns to `pending`. A partial unique index
allows only one unfinished lease for a job. Attempts are bounded by `max_attempts`.

### Chapter publication is atomic and retry-idempotent

The worker chapter-commit primitive verifies a live running lease and the claimed owner/book/
incarnation. In one transaction it publishes the chapter-derived row, chapter summary, cost-ledger
entry, and finally the ingestion receipt. The deferred memory foreign key keeps the receipt last while
preventing incomplete derived state from committing. A retry with the same receipt and content hash is
a no-op; a changed identity or content hash fails loud. Cost idempotency is independently unique per
owner. Future hosted extraction handlers must use this primitive (or an equivalent transaction that
preserves the same receipt-last contract) for every derived-memory batch.

### Worker and tenant roles are deliberately different

The HTTP tenant role remains non-superuser and RLS-enforced. It receives only SELECT/INSERT/UPDATE on
`jobs` in addition to its existing exact allow-list, and it cannot read `job_attempts` or lease tokens.

The worker uses a separate non-superuser, NOINHERIT, BYPASSRLS role because scheduling must find work
across owners. Startup validates an exact table/verb allow-list: job and attempt claim mutations plus
only the chapter, summary, receipt, and cost operations needed for atomic publication. Extra table or
verb access fails startup. Cross-tenant scheduling never makes owner optional in post-claim SQL.

### Failures and cancellation are content-free

Persisted failures have exactly `code` and `retryable`, using a closed reviewed vocabulary. The worker
does not log or persist arbitrary exception text because parser/provider exceptions can contain source
prose or secrets. Unknown exceptions become retryable `internal_error`; loss of a lease is benign and
cannot mutate the replacement attempt.

An owner can cancel a pending job immediately. A leased or running job records a cancellation request;
`start`, `heartbeat`, `succeed`, and lease recovery cooperatively terminate it. Deleting a book also
requests cancellation for every active job of that incarnation.

### Progress is receipt-derived

The owner-only job list and detail endpoints return state, attempt counts, bounded failure metadata,
timestamps, total chapters, and the count of durable chapter receipts. They never expose payloads,
worker ids, attempts, lease tokens, or storage identity. Cancellation is CSRF-bound. Foreign job UUIDs
are indistinguishable from missing UUIDs.

| Method and path | Behavior |
| --- | --- |
| `GET /api/jobs` | list the signed-in owner's jobs and receipt-derived progress |
| `GET /api/jobs/{job_id}` | return one owned job or the same 404 as missing |
| `POST /api/jobs/{job_id}/cancel` | request or complete eligible cancellation |

## Operation

The installed command is `reading-companion-hosted-worker`. It requires `HOSTED_WORKER_DSN` and a
`HOSTED_JOB_HANDLER=module.path:callable` implementation. The callable receives the fenced claim and
worker repository, heartbeats during long provider calls, stops when heartbeat reports cancellation,
and publishes through the atomic commit primitive. This ticket provides and verifies the durable
runner boundary; the owner-specific credential/provider handler is intentionally supplied by the
credential and provider-routing increments (LIT-45/LIT-46), rather than embedding secrets in jobs.

## Verification

Real PostgreSQL acceptance tests prove concurrent single-winner claims, digest-only tokens, expired
lease recovery, attempt exhaustion, retry scheduling, cooperative pending/running cancellation,
stale-lease fencing, atomic no-duplicate chapter/summary/cost/receipt commits, owner-scoped progress,
cross-tenant 404 behavior, and exact tenant/worker roles. Unit tests prove the process runner's success,
reviewed-failure, unknown-exception sanitization, and cancelled-start behavior.

The full backend suite passes 687 tests with 8 expected skips against real PostgreSQL. The dedicated
hosted/parity PostgreSQL gate contains 85 tests.

## Consequences and boundaries

Hosted ingestion work is now durable, observable, cancellable, and safe to run outside the API
process. Local/community ingestion and permanent data are unchanged. Provider credentials and actual
provider routing remain outside this increment, so a production deployment must not start the worker
without the LIT-45/LIT-46 handler. This is not yet a public-launch claim; quota reservation, operational
metrics/alerts, recovery drills, and the remaining hosted lifecycle work stay required.
