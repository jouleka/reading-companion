# ADR 0027 - Atomic owner quotas, concurrency, and rate limits

**Status:** Accepted (2026-07-19; LIT-47)

## Context

Process-local counters cannot protect a multi-replica hosted service: two web instances can each admit
an upload and two workers can each believe they own the final provider slot. Limits also need to be
visible and adjustable without inspecting book titles, source bytes, prompts, or credentials.

## Decision

### PostgreSQL owns one explicit policy per owner

Every user receives an `owner_limits` row with positive ceilings for a single upload, live source
storage, live book count, active ingestion jobs, requests per fixed window, provider concurrency, and
an optional USD spend ceiling. Existing users are backfilled and the user-insert rule supplies the
same defaults for later OIDC accounts. Both policy and request-window tables are owner-RLS protected.

The authenticated `GET /api/limits` response contains only that owner's numeric policy and aggregate
usage. It contains no owner UUID, profile data, book metadata, content, or credential identifier.

### Admission decisions serialize on one owner-keyed transaction lock

Upload metadata creation takes a PostgreSQL transaction advisory lock derived from the owner UUID
before reading policy and counting live books, source bytes, and active jobs. Concurrent web instances
therefore cannot both consume the last unit. The object is
deleted through the existing compensation path when database admission rejects it. The deployment's
global upload-body cap remains a hard safety maximum; an owner policy can be narrower but cannot make
the global parser boundary larger.

Authenticated tenant requests consume a PostgreSQL fixed-window counter under the same owner lock and
a row lock on the window. Rejection is `429` with `Retry-After`, a fixed code, numeric limit, reset delay,
and an action. Time-independent upload/book/storage quotas return structured `413` or `409` responses
that explain whether to choose a smaller EPUB, delete owned resources, or contact an operator. Active
job rejection is retryable and includes a retry delay.

Worker claim selection combines job-row `SKIP LOCKED` with a non-blocking attempt at that owner lock.
It counts already leased/running jobs, so concurrent workers can continue serving another owner but cannot
exceed one owner's `max_provider_concurrency`. This conservatively bounds the whole handler lifetime,
including provider calls, rather than relying on an in-process semaphore.

### Optional spend is reserved before provider I/O

The worker repository exposes a claim-fenced, idempotent reservation operation over the existing
`cost_reservations` table. With a spend ceiling configured, ledger totals plus open reservations are
checked under the owner lock. Chapter commit requires the matching reservation,
rechecks actual cost, atomically settles it with the ledger/receipt transaction, and rejects mismatched
or unreserved work. A crash leaves the reservation open and conservative instead of guessing that the
provider did not bill.

### Operator tooling is aggregate-only

`python -m app.hosted.limits show` lists opaque owner UUIDs, numeric policies, and aggregate counts/
bytes/spend. `set OWNER` changes only reviewed numeric fields through a privileged operator DSN.
Database constraints reject zero/negative or unbounded window values. The tool never selects users,
books, source bytes, memory, prompts, or credential tables beyond aggregate subqueries.

## Verification

Real PostgreSQL/pgvector tests prove RLS and exact grants, per-owner request isolation and `429` reset
metadata, upload compensation, every quota class, concurrent last-book admission, concurrent worker
claim limits with another owner still making progress, optional spend reservation/idempotency/settling,
and the requirement to reserve when a ceiling exists. The OIDC real-database suite proves later users
receive a policy row without expanding the auth role's privileges.

The completed checkout collects 716 backend tests: 708 pass and eight environment-specific tests skip
with the real PostgreSQL 16 + pgvector gate enabled. The frontend remains 164 passing tests with a
clean production build; Ruff and diff checks are clean.

## Consequences and boundaries

Fixed windows can reject a burst near a boundary more abruptly than a token bucket; their benefit here
is a small, auditable atomic state. The conservative provider limit may hold a slot during parsing or
database work, trading some throughput for a hard cross-instance bound. Open spend reservations need
operator reconciliation after ambiguous crashes; automatically releasing them would weaken the spend
guarantee.

Limits protect availability but do not replace LIT-48's complete cross-tenant adversarial matrix or
LIT-49's audit/redaction/leak gate. Local SQLite/community behavior and the permanent library remain
unchanged.
