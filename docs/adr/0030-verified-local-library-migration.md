# ADR 0030 — Local-to-hosted migration is backup-first and content-verified

**Status:** Accepted (2026-07-19)
**Ticket:** LIT-50 / MIGRATE-1

## Context

The local product stores catalog and reading state in `catalog.db`, structured memory in one
`memory.db` per book, and the EPUB/atom manifest beside it. Hosted mode stores owner-composite rows in
PostgreSQL and encrypted source objects behind the LIT-43 abstraction. A cutover must not mutate the
accepted local library, guess an owner, copy raw chapter prose into PostgreSQL, or leave a partially
trusted book after interruption.

## Decision

Migration accepts only an LIT-24 `.rcbackup` that passes all archive, SQLite integrity, source hash,
atom-set, durable receipt-frontier, cost, and reading-state checks. `backup` creates one immutable
archive per catalog book outside `DATA_DIR`; `plan` verifies it and checks that the explicitly supplied
hosted owner exists without writing to PostgreSQL or object storage.

IDs are UUIDv5 values bound to the selected owner and local identity. The source-content checksum is
derived from portable catalog/memory data, atoms, and EPUB hash, so rebuilding an equivalent ZIP does
not create a second import. A changed archive for an already imported local book fails closed. The EPUB
is written under its deterministic owner/object identity first. A matching pre-existing object is
resume evidence; different bytes are a hard conflict. All PostgreSQL rows and the completion record
then commit in one transaction. Retrying a completed plan verifies the encrypted object and returns an
idempotent result.

The mapper imports book metadata, reading state/epoch, atom-aligned chapters, durable receipts,
bitemporal structured memory, correction history, chunks/vectors, and cost ledger rows. Local raw
chapter text is deliberately excluded; chapter/receipt hashes are upgraded from the local truncated
hash to the full SHA-256 of the verified raw archive member before the text is discarded. Verification
compares table counts, source size/hash, reading state, and all structured-memory validity boundaries.

Rollback requires the same archive/owner plan. It deletes only the plan-bound costs and dependency
ordered book rows, deletes the encrypted object, and finally removes the migration record. A failed
object deletion leaves a `rolling_back` record so the same rollback can safely resume. The original
archive remains the tested recovery source throughout.

## Consequences

- Ownership is always explicit and every imported hosted row carries that owner.
- Dry-run is genuinely read-only; storage configuration is not even constructed.
- One failed book does not corrupt another; multi-book invocations resume book by book.
- The migration is a cutover tool, not dual-write or ongoing synchronization.
- Operators retain the verified archives until hosted backup/restore and application smoke checks are
  complete.

Operational commands and the tested recovery sequence are in
[`../runbooks/local-to-hosted-migration.md`](../runbooks/local-to-hosted-migration.md).
