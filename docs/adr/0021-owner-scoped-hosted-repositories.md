# ADR 0021 — Owner-scoped hosted repositories and API authorization

**Status:** Accepted (2026-07-16; LIT-41)

## Context

LIT-40 authenticates an OIDC identity into one internal user UUID and resolves an opaque browser
session. That is necessary but insufficient for tenancy: attaching an authentication dependency to a
route would still leave a repository query, cost lookup, or position update able to omit ownership.
ADR 0017 requires the owner boundary below HTTP, with PostgreSQL RLS as a second independent barrier.

This increment must not counterfeit later infrastructure. Hosted upload and source streaming need
LIT-43 object storage; deletion needs LIT-42 lifecycle coordination plus storage deletion; export
needs LIT-50's audited migration/export workflow; position advance needs the hosted atom/source
boundary. Those surfaces remain unavailable rather than receiving incomplete implementations.

## Decision

### Session ownership becomes a typed repository argument

The hosted route dependency validates the server-side session and constructs `OwnerId` from
`Principal.owner_id`. Every public tenant repository method has a required `owner_id: OwnerId`
parameter. No route declares owner/user identity in a path, query, or body. Query keys equivalent to
`owner_id`, `ownerId`, `owner`, or `user_id` are rejected with `422`; write bodies forbid extra
fields. The checked-in static test inspects every public repository signature so a new unscoped method
fails CI.

`OwnerId` is not a client model. A caller cannot select a tenant by supplying a UUID; only the
authenticated session dependency constructs it at HTTP composition.

### Explicit predicates and transaction-local RLS are both mandatory

Every repository operation owns one PostgreSQL transaction and begins with:

```sql
SELECT set_config('app.owner_id', $session_owner_uuid, true);
```

Every query and mutation still includes `owner_id = $session_owner_uuid`, and composite joins retain
owner/book/incarnation identity. The transaction-local setting disappears at commit/rollback and the
connection closes before returning. RLS therefore protects a missed predicate, while explicit
predicates remain correct on a superuser connection that deliberately bypasses RLS in the adversarial
test.

A separate `HOSTED_TENANT_DSN` role is required. Startup rejects superuser, `BYPASSRLS`, role
inheritance/membership, cluster administration capabilities, any missing required grant, any extra
privilege on an allowed table, or any privilege on another table. Its reviewed grants are:

| Table | Privileges |
| --- | --- |
| `books` | `SELECT` |
| `reading_state` | `SELECT, UPDATE` |
| `chapters`, `ingested_chapters`, `chapter_summaries` | `SELECT` |
| `entities`, `edges`, `events`, `themes` | `SELECT` |
| `cost_ledger` | `SELECT` |

It is distinct from LIT-40's narrow pre-owner `BYPASSRLS` authentication role. Schema names are
explicit in repository SQL.

### Enabled hosted surfaces are deliberately small

The enabled owner-scoped API is:

| Method and path | Access |
| --- | --- |
| `GET /api/books` | owned library list |
| `GET /api/books/{book_id}` | owned book metadata |
| `GET /api/books/{book_id}/position` | owned reading state and bookmark-bounded receipt count |
| `POST /api/books/{book_id}/position/reset` | CSRF- and epoch-bound reread reset |
| `GET /api/books/{book_id}/memory` | server-bookmark-bounded structured snapshot |
| `GET /api/costs[?book_id=…]` | owner/book-scoped cost ledger |

The memory snapshot reads the bookmark from owned server state, never a client parameter. Entities,
relationships, events, themes, and summaries require live chapter receipts, live chapters, valid-time
visibility, transaction-time liveness, and referentially visible relationship endpoints. Position
receipt count is additionally bounded to chapters at or before the bookmark; the first review pass
caught that an unbounded count could reveal future processing progress.

The only enabled mutation is reset. It locks the owned state, checks the expected epoch, clears the
cursor/frontier, and increments the epoch while preserving memory, receipts, and costs. It requires
the session-bound CSRF cookie/header. Hosted API responses, including `401`/`404`, carry
`Cache-Control: private, no-store`.

### Foreign identifiers are missing identifiers

For every identifier-bearing endpoint, a valid book UUID owned by A produces the same status and JSON
body for B as a random missing UUID: `404 {"detail":"unknown book"}`. The write path produces no
database change. Lists and cost totals contain only the session owner. `403` is reserved for failed
CSRF on an otherwise authenticated unsafe request, not resource ownership.

`backend/app/hosted/tenant/endpoints.json` is the route/access/evidence inventory. A test derives the
actual FastAPI schema and requires an exact match, so a new hosted tenant route without named
cross-tenant evidence fails CI. The same inventory records upload, delete, and export as unavailable
with their owning follow-up ticket and expected `404`/`405` behavior.

## Verification

The real PostgreSQL suite creates users A and B, seeds A's real book identifier, memory, position, and
cost row, and exercises B through the hosted HTTP app using the non-BYPASSRLS tenant role. It proves:

- every enabled route requires a server session;
- A's valid identifier is indistinguishable from missing on get/position/memory/reset;
- B's lists and totals exclude A, including a cost query with A's real book ID;
- foreign reset cannot alter A's state;
- owner query/body injection is rejected and unsafe writes require CSRF;
- the memory response cannot surface a future entity or future receipt count;
- unavailable delete/export/upload routes remain absent;
- role grants are exact and fail startup when missing or broadened; and
- the same repository remains isolated through explicit predicates when called as the migration
  superuser with RLS bypassed.

The LIT-39 SQLite/PostgreSQL spoiler-parity suite remains a separate required gate.

## Consequences and boundaries

Hosted mode now has a small authenticated read/reset surface, not a complete hosted product. Local
SQLite composition and APIs are unchanged. LIT-42 owns tenant-scoped caches, locks, and lifecycle;
LIT-43 and LIT-44 add storage and ingestion jobs before import/advance can safely open. Later tickets
own credentials, quotas, audit evidence, export/deletion, and the complete cross-tenant product
inventory. No local library is read, migrated, or dual-written by this work.
