# ADR 0017 — Hosted multi-user architecture and data-ownership contract

**Status:** **Accepted** (2026-07-16; LIT-37)

## Context

Litlet is a production-shaped **local, single-user** application. Its safety core is strong: chapter
atoms define the spoiler frontier, memory reads go through one bookmark-bounded funnel, ingestion is
idempotent, and the local library has durable backup and recovery. Its process and persistence
boundaries are intentionally not multi-user: one global SQLite catalog, one process-wide Store and
LLM client, filesystem paths derived from book IDs, process-local locks/caches, and an in-process
thread-pool worker.

Putting that application on the public internet unchanged would make a guessed book ID, an unscoped
cache entry, a job retry, or a filesystem path a possible cross-account disclosure. Adding login only
at the route layer would not fix those structural risks. The hosted product therefore needs a tenant
contract that is at least as difficult to bypass as the existing spoiler contract.

This decision defines the target and the migration order. It does not authorize a public deployment;
the launch gate at the end of this record must be green first.

## Decision

### 1. Two product modes share one safety core

- **Local/community mode** keeps SQLite, local EPUB files, the existing offline provider option, and
  owner-managed environment configuration. It binds to loopback by default. A stable synthetic local
  owner UUID is injected at the composition boundary so repositories keep the same owner-scoped
  signatures as hosted mode.
- **Hosted mode** uses OIDC login, PostgreSQL plus pgvector, S3-compatible object storage, a separate
  durable worker, encrypted per-user provider credentials, and distributed limits.
- Chapter atoms, the monotonic revealed frontier, bitemporal memory, prefiltered retrieval, generated
  prose gates, and marker-based ingestion remain common domain behavior. A hosted adapter may not
  weaken or bypass them.

SQLite remains the supported local backend and migration source. PostgreSQL is the hosted system of
record; this decision does not introduce dual writes in normal operation.

### 2. Identity and session boundary

`users.id` is an application-generated UUID, never an email address and never the OIDC subject alone.
An `external_identities` record maps `(issuer, subject)` to that internal user. Email is profile data,
not an authorization key, and linking identities requires an explicit verified flow.

Hosted browser authentication uses Authorization Code with PKCE. The server owns the session and sends
only an opaque session identifier in an `HttpOnly` cookie that is `Secure` in hosted mode and has an
explicit `SameSite` policy. Session rotation, expiry, revocation, logout, OAuth state/nonce validation,
and CSRF protection for state-changing cookie-authenticated requests are mandatory. API handlers
receive an authenticated principal from the session dependency; clients never submit an `owner_id`.

### 3. Ownership model

Every tenant-owned row has a non-null `owner_id` referencing `users.id`, including derived data. The
denormalization is deliberate: it makes ownership visible at each query and permits composite foreign
keys that reject mismatched tenant/resource pairs.

| Resource | Ownership and required key |
| --- | --- |
| `users`, `external_identities`, `sessions` | User/account boundary; sessions reference one internal user |
| `books`, `source_objects` | `owner_id`; `(owner_id, book_id)` is the tenant resource identity |
| `reading_state`, `reader_preferences` | `owner_id` plus composite reference to the owned book/user |
| memory facts, chunks, vectors, summaries, corrections, receipts | `owner_id` plus composite reference to the owned book; existing reveal/valid-time keys remain |
| highlights, annotations, bookmarks | `owner_id` plus composite reference to the owned book and stable EPUB anchor |
| `cost_ledger`, `cost_reservations` | `owner_id`; optionally book/job; no provider secret material |
| `jobs`, attempts, progress events | `owner_id`; owned book and credential IDs where applicable |
| `provider_credentials`, provider/model settings | `owner_id`; encrypted secret or metadata only |
| recap/search caches, locks, idempotency keys | Key begins with `owner_id`; resource incarnation and spoiler frontier follow |
| migrations and public provider capability metadata | Service-scoped; no tenant content |

Books are private. There is no cross-user book deduplication, shared object key, shared derived memory,
or content-addressed existence signal in the first hosted release. A matching EPUB hash in two accounts
creates two independently owned logical books and opaque storage objects.

Deleting an account is a durable workflow that revokes sessions, prevents new work, tombstones or
deletes owned rows, deletes object storage, and records only the minimum non-content audit evidence
required by policy. Export and deletion never accept an owner supplied by the client.

### 4. Authorization is session-derived and enforced below HTTP

Every tenant repository method takes an explicit `OwnerId`; every lookup predicate includes it. The
route dependency creates the owner context from the verified session and passes it inward. Routes,
workers, CLI/admin operations, storage adapters, and cache/lock helpers may not infer ownership from a
bare book, job, object, or credential ID.

For an authenticated user, a well-formed identifier that belongs to another tenant returns the same
`404` shape and timing class as a missing identifier. Lists, counts, validation errors, imports,
exports, search, citations, job status, costs, and deletion follow the same non-disclosure rule.
Unauthenticated requests return `401`; authenticated requests lacking an operator capability return
`403` only for genuinely global operator surfaces where revealing that surface is intentional.

PostgreSQL Row-Level Security is defense in depth, not the primary programming model. Each request or
worker unit runs in a transaction that sets a transaction-local owner UUID; policies require
`owner_id = current_setting('app.owner_id', true)::uuid`. Runtime roles are neither superusers nor
`BYPASSRLS`, connection-pool return clears transaction state, and service-wide maintenance uses a
separate narrowly controlled role and code path. Repository scoping is still tested independently so
the application remains correct on local SQLite.

### 5. PostgreSQL preserves the spoiler funnel

PostgreSQL stores hosted catalog data and structured memory. pgvector ranks only candidates already
constrained by `owner_id`, `book_id`, the current book incarnation, live chapter receipts, embedding
space, retraction state, and `revealed_at <= effective_bookmark`. The database adapter exposes the
same bookmark-bounded domain operations as `BookmarkView`; handlers do not receive a general query
interface or raw connection.

Migrations are transactional, forward-only, reviewed, and run from an empty database in CI. Composite
foreign keys include owner identity wherever a child refers to tenant data. Unique constraints and
idempotency keys are tenant-scoped. The SQLite/PostgreSQL parity suite is a hosted cutover gate and
must exercise structured reads, RAG, corrections, resets, receipts, and cache identities across every
bookmark fixture.

### 6. Object storage uses opaque, server-owned keys

The storage interface provides upload, verified streaming read, existence check, and delete. Local
mode implements it with the filesystem; hosted mode uses an S3-compatible service. Object keys are
generated by the server from opaque IDs and tenant scope, never accepted from request paths or
returned as reusable bucket keys. Upload size, MIME/container validation, checksum, encryption at
rest, and the existing adversarial EPUB checks apply before a source becomes readable by a worker.

Downloads use an authorized application stream or a short-lived signed URL created only after the
owner-scoped lookup. Bucket listing is not a product API. Storage tests attempt cross-tenant reads and
deletes with real object identifiers.

### 7. Ingestion becomes a durable, owner-scoped job

Hosted ingestion does not use the in-process `ThreadPoolExecutor`. The web transaction creates a job
with `owner_id`, book ID, state, idempotency key, attempt count, timestamps, and sanitized error data.
A separate worker atomically claims a lease, heartbeats it, and can recover an expired lease. Provider
calls happen outside database locks; short transactions commit one chapter's derived facts, receipt,
cost, and progress atomically as they do today.

Retries and duplicate deliveries must converge on the same receipt and cost outcome. Job payloads hold
credential IDs, never decrypted keys. Cancellation is owner-scoped and checked between safe commit
boundaries. User-visible progress is derived from durable job/receipt state, not process memory.

### 8. BYOK credentials are secrets, not settings strings

Hosted users may submit provider keys through a dedicated TLS-only endpoint. The service envelope-
encrypts each secret with a random data key; only the configured KMS/secret-manager master key can
unwrap it. The database stores ciphertext, encrypted data key, key version, provider, masked label,
owner, and lifecycle timestamps. A successful create response never returns the plaintext again.

Workers resolve an owner-scoped credential ID just in time, keep plaintext only in memory for the
provider call, and never place it in a job record, exception, trace, prompt log, analytic event, or API
response. Rotation creates a new version; replacement and deletion have defined job behavior. Canary
secret tests scan logs, errors, database fields, and responses. Local mode continues to reference the
owner's environment key and must not copy or rotate it automatically.

### 9. Caches, locks, quotas, and telemetry are tenant-aware

Every cache, single-flight, request lock, worker lock, idempotency key, and object path begins with
`owner_id` and includes book incarnation where stale shelf lifetimes matter. Hosted locks and rate
limits use shared infrastructure or database primitives so multiple web/worker processes agree;
process-local state may be only an optimization whose loss cannot break correctness.

Upload size, owned storage, book count, active jobs, request rate, provider concurrency, and optional
spend ceilings are explicit per-user policies. Enforcement is atomic and returns actionable `429` or
quota responses without disclosing other tenants. Metrics use opaque identifiers and bounded labels;
logs exclude book text, prompts, EPUB filenames where sensitive, authorization headers, and secrets.
Security audit events record actor, action, target class/opaque ID, result, and time with documented
access and retention.

## Required cross-tenant test contract

For every tenant-owned endpoint or worker operation, fixtures create users A and B, create the target
under A, then exercise B with A's real identifier. The inventory covers list/get/update/delete,
position/reset, import/export, source streaming, structured views, search/citations, recap, annotations,
costs, credentials, job progress/cancel/retry, account export/deletion, caches, locks, and storage.
Assertions cover response status/body, database effects, object access, jobs, logs, and timing class.

A checked-in endpoint/resource inventory must fail CI when a new tenant-owned operation lacks an
adversarial case. Tests run against PostgreSQL with RLS enabled and against application scoping with
RLS deliberately disabled, proving two independent barriers. Secret canaries and spoiler-parity tests
are separate release-blocking suites.

## Migration sequence

1. **Contracts:** introduce typed owner context, persistence/storage/job interfaces, and this inventory
   while local mode injects its synthetic owner (LIT-37).
2. **Hosted persistence:** add PostgreSQL/pgvector schema and SQLite/PostgreSQL spoiler-parity harness
   (LIT-38, LIT-39).
3. **Identity and isolation:** add OIDC sessions, owner-scoped repositories/APIs, and tenant-scoped
   runtime state (LIT-40–LIT-42).
4. **Durable boundaries:** add object storage and the leased worker; then encrypted BYOK configuration
   so no interim job format ever contains raw keys (LIT-43–LIT-46).
5. **Abuse and evidence:** add distributed quotas, cross-tenant tests, audit/redaction tests, and the
   local-library migration tool (LIT-47–LIT-50).
6. **Product experience:** land synchronized reading state before mobile/offline/annotation features;
   add cited AI assistance only after persistence, BYOK, and spoiler parity are green (LIT-51–LIT-60).

The existing library migrates by explicit owner selection into a fresh hosted database and object
namespace. The tool is dry-run capable, resumable, idempotent, and verifies counts, hashes, atoms,
receipts, bookmark/epoch, memory boundaries, vectors, costs, and source objects. It never mutates the
accepted local store in place.

## Hosted launch gate

Public hosting is blocked until all of the following are true:

- OIDC/session/CSRF behavior is production-configured and reviewed.
- Every tenant resource and derived path is owner-scoped; the adversarial inventory is complete.
- PostgreSQL spoiler parity, pgvector prefiltering, migrations, backup, restore, and migration drills pass.
- Object storage and durable worker recovery pass cross-process and cross-tenant tests.
- BYOK encryption, redaction, rotation, and canary-leak tests pass with no plaintext persistence.
- Quotas/rate limits are atomically enforced and operational alerts/runbooks exist.
- Account export/deletion, audit retention, privacy terms, dependency/security scanning, TLS, headers,
  and deployment rollback are documented and exercised.

## Consequences and explicit non-decisions

The hosted system carries more infrastructure than the local app, but ownership becomes a structural
property rather than a collection of route checks. Local development stays lightweight and remains a
real supported product mode. PostgreSQL parity work must precede feature work that relies on hosted
state, and public deployment cannot be declared by simply adding a domain and login screen.

This ADR does not choose a hosting vendor, OIDC vendor, KMS, object-storage vendor, queue product,
subscription/billing model, or domain. Those are adapter and commercial decisions constrained by the
contracts above. Shared libraries, social features, organization tenants, and server-paid model usage
are also out of the first hosted release.
