# ADR 0019 — SQLite/PostgreSQL spoiler-parity cutover gate

**Status:** Accepted (2026-07-16; LIT-39)

## Context

ADR 0017 requires behavioral parity before hosted PostgreSQL can replace local SQLite for any reader
operation. Matching table names is insufficient: the gate must prove that valid-time corrections,
referential closure, receipts, reset epochs, cache invalidation, and retrieval prefiltering preserve
the reader's exact frontier.

LIT-39 remains a harness ticket. It does not compose a PostgreSQL repository into FastAPI or expose a
hosted route; those authorization and repository boundaries belong to LIT-40 and LIT-41.

## Decision

### One corpus, two real backends

`backend/tests/parity/fixtures/spoiler_parity.json` is the single source for both stores. The SQLite
adapter writes it through the production `Store`, `MemoryDB`, `BookmarkView`, sqlite-vec, and
`Catalog`. The PostgreSQL adapter applies the committed migrations to an empty real database, writes
the same logical facts through owner-composite keys and receipts, and reads them with owner-,
incarnation-, valid-time-, retraction-, and referential-closure predicates plus the committed
`search_chunks_prefiltered` pgvector function.

The corpus deliberately contains:

- chapter frontiers from zero through six and a retracted sixth chapter;
- an identity correction where Alexander ends as Alexandra begins;
- future-referenced aliases, edges, and event participants whose target entity appears at chapter 5;
- valid-time edge, theme, event, and state supersessions;
- durable receipts whose contiguous live frontier stops at chapter 5;
- an explicit position reset/epoch with a stale-write canary;
- summary re-extraction that must invalidate each backend's cache identity; and
- future and retracted exact-match vectors that would crowd visible results out under post-filtered
  global KNN.

Every structured surface is normalized to logical fixture keys and compared at every bookmark:
chapters, all entity types, aliases, relationships, timeline, participants, events-for-entity,
themes, current state, summaries, bios, and catch-me-up. Independent assertions derive the permitted
entity/chapter sets from the corpus rather than trusting either backend. Both retrieval queries run at
every bookmark, and every hit is independently checked against the reveal boundary.

The parity review also closed a schema-level stale-receipt gap: migration 0005 binds
`ingested_chapters.content_hash` to the exact owner/book/incarnation/chapter content hash. A receipt
for different bytes now fails at commit and cannot authorize retrieval or a completion frontier.

### Cutover-blocking and falsifiable

The pinned PostgreSQL 16 + pgvector GitHub Actions job runs both `backend/tests/hosted` and
`backend/tests/parity`. `backend/scripts/test_postgres.sh` mirrors that command locally. The parity
test itself asserts both entry points include the suite, so accidentally dropping the gate is a test
failure.

At bookmark 2 with `k=1`, the nearest global candidates are the future chapter-5 vector and the
retracted chapter-6 vector. The required result is the less-similar visible chapter-2 chunk. Ranking
globally and filtering afterward therefore fails the test through under-retrieval even if the final
row happens not to leak, making the prefilter property load-bearing and falsifiable.

## Explicit reviewed differences

The machine-checked allow-list is `backend/tests/parity/fixtures/expected_differences.json`. It permits
only representation or storage-boundary differences, never spoiler behavior:

1. Full raw chapters remain local-only under ADR 0002/0018 and are outside hosted parity.
2. SQLite integer row IDs and its combined model/space string normalize to PostgreSQL UUIDs and its
   separate embedding-space fingerprint.
3. Cache-token bytes may differ because physical identities differ; repeat stability and invalidation
   transitions must match.
4. Local `ingest_progress` is a catalog field, while hosted progress derives from durable receipts and
   jobs. Reset parity compares bookmark, cursor, epoch, and unchanged receipt state.

Timestamps, physical IDs, floating-point score bytes, and database-specific error classes are not
product outputs. Logical ordering, visible content, receipt frontier, reset outcome, cache transition,
and retrieval membership are parity requirements.

## Consequences and boundaries

Hosted cutover now has a committed regression gate over both real databases. A PostgreSQL query that
weakens a reveal/retraction/ownership predicate, loses referential closure, changes correction
time-travel, admits an incomplete receipt, regresses epoch handling, serves a stale cache identity, or
post-filters vector results turns CI red.

This harness is intentionally small and adversarial, not a performance benchmark or full-corpus
migration drill. LIT-40 owns OIDC/session bootstrap; LIT-41 owns production owner-scoped repositories
and APIs; LIT-48 owns the endpoint-wide cross-tenant adversarial matrix; LIT-50 owns the accepted local
library migration. SQLite remains the local default and the permanent library is untouched.
