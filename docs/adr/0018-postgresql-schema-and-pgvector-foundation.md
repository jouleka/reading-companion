# ADR 0018 — PostgreSQL schema, migrations, and pgvector foundation

**Status:** Accepted (2026-07-16; LIT-38)

## Context

ADR 0017 requires hosted persistence to make tenant ownership as structural as the local spoiler
boundary. LIT-38 establishes that database contract without switching the application away from its
current SQLite composition, adding public routes, or migrating the accepted local library.

## Decision

### Forward-only transactional migrations

Hosted DDL lives in ordered, immutable SQL files under `backend/app/hosted/schema`. The small
`app.hosted.migrations` runner:

- requires contiguous versions and rejects transaction-control statements inside migration files;
- takes a PostgreSQL advisory lock and runs each pending file in its own transaction;
- records the file name and SHA-256 checksum in `app_schema_migrations`;
- refuses an altered applied migration or a database version newer than the code; and
- consumes its DSN from a named environment variable without logging it.

The stream is reproducible from an empty PostgreSQL database. A committed GitHub Actions job and
`backend/scripts/test_postgres.sh` both run it against the pinned PostgreSQL 16 + pgvector image.

### Ownership-aware hosted schema

Every tenant-owned or derived table has a non-null `owner_id`. Resource identity includes the book
incarnation, and child references carry `(owner_id, book_id, book_incarnation)` or the analogous
owner-prefixed composite key. This makes a mismatched-owner reference fail at the database boundary,
not merely return an empty application query. Primary keys, idempotency keys, partial live
uniqueness, and lookup indexes begin with owner scope.

Two global uniqueness constraints are intentional account-bootstrap exceptions: `(issuer, subject)`
must map to one internal user, and an opaque session digest must resolve to one session before the
session dependency knows its owner. LIT-40 will expose those lookups only through the authentication
boundary; ordinary repositories remain owner-scoped.

The schema covers users/external identities/sessions; books and opaque source-object metadata;
reading state/preferences; chapter atoms and receipts; bitemporal entities, aliases, edges, events,
participants, themes, state, summaries, and corrections; chunks/embeddings; jobs/attempts;
cost ledger/reservations; encrypted provider-credential fields and model settings; highlights,
annotations, bookmarks; and bounded audit-event metadata.

Full raw chapters remain local-only under ADR 0002. Hosted PostgreSQL does not gain a `raw_chapters`
table in this ticket. Source EPUB bytes belong behind the later object-storage policy, while the
minimum chunk text needed for cited retrieval remains owner- and bookmark-bounded.

### Receipt-bound bitemporal memory

Hosted facts retain story valid-time (`revealed_at`, optional `invalid_at`) and ingestion
transaction-time (`recorded_at`, optional `retracted_at`). All derived memory rows identify the
chapter receipt that authorized them. Those foreign keys are deferrable so ingestion can preserve
the existing rule that the completion receipt is written last, while commit still fails if the
receipt is absent.

### pgvector eligibility before ranking

`chunk_embeddings` stores the model identity, dimension, embedding-space fingerprint, distance metric,
and vector, with a database check that the declared and actual dimensions agree. No cross-tenant ANN
index exists.

`search_chunks_prefiltered` is an invoker-rights exact-search foundation. Its `MATERIALIZED`
eligibility set constrains owner, book, book incarnation, effective bookmark, live chunk/chapter/
embedding state, completed live receipt, embedding model, dimension, embedding-space fingerprint,
and distance metric before any distance operator or limit runs. This intentionally favors a
falsifiable safe baseline over an unsafe post-filtered global KNN. A later adapter may add per-space
ANN indexes only with a plan and
recall proof that preserves the same prefilter contract.

### Transaction-local RLS context

RLS is enabled and forced on every tenant table as defense in depth. Later hosted repositories run
each request or worker unit in one transaction:

```sql
SELECT set_config('app.owner_id', $1, true);
-- Every repository query still includes owner_id = $1.
```

`app_current_owner_id()` reads that transaction-local value, and policies require it to equal the
row owner (or `users.id`). The setting disappears when the transaction ends. Runtime roles must not
be superusers or carry `BYPASSRLS`; authentication bootstrap and service maintenance require separate,
narrow roles in later tickets. RLS does not replace explicit owner predicates, which remain necessary
for SQLite parity and application correctness.

## Verification contract

The real-PostgreSQL suite creates a fresh database per test and proves:

- empty-database application and idempotent reapplication with pgvector enabled;
- required schema coverage, non-null owners, and RLS on every tenant table;
- cross-owner composite references and duplicate same-owner idempotency keys are rejected;
- derived rows cannot commit without a matching completed chapter receipt;
- credential storage exposes ciphertext/envelope metadata fields, not plaintext-key fields;
- transaction-local owner context isolates rows and clears at commit; and
- future or retracted candidates cannot enter vector ranking even when they are nearest neighbors.

## Consequences and boundaries

Local/community mode remains unchanged: SQLite, sqlite-vec, local files, and the accepted permanent
library are neither migrated nor dual-written. No PostgreSQL adapter is composed into FastAPI, no
hosted routes or authentication are exposed, and no credential encryption implementation exists yet.
SQLite/PostgreSQL behavioral parity belongs to LIT-39; OIDC/session bootstrap to LIT-40; repositories
and authorization to LIT-41. Public hosting remains blocked through the ADR 0017 launch gate.
