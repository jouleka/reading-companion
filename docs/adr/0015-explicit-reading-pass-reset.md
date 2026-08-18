# ADR 0015: Re-reading is an explicit pass reset, not memory deletion

- Status: Accepted
- Date: 2026-07-13
- Ticket: LIT-17

## Context

Ordinary backward paging has two distinct meanings: the resume cursor may move backward, while the
spoiler frontier must retain the furthest chapter completed in that reading pass. A reader also needs
a deliberate way to experience the companion from the beginning again. Treating those two actions as
the same would let an accidental page move hide already-revealed context, and deleting extracted
memory would repay provider costs for information the application already owns.

Delayed debounced reports and another open tab create a harder race: a position written just after a
reset could silently restore the old frontier unless the write is bound to the pass in which it was
created.

## Decision

1. Backward navigation updates the latest `cfi`, but `reading_state.bookmark` remains the monotonic
   high-water within one pass. The CFI is therefore a resume cursor, not a second spoiler authority.
2. “Start over” is an explicit, confirmed operation. It atomically sets `bookmark=0`, clears `cfi`,
   and increments `reading_state.position_epoch`.
3. Every position write carries its expected epoch. A delayed report, old tab, or pre-LIT-17 client is
   accepted only in epoch zero; after a reset it receives HTTP 409 and cannot widen the new pass.
4. Reset does not change `ingest_progress`, `memory.db`, completion receipts, the cost ledger, provider
   configuration, the source EPUB, or the atom manifest. Advancing through a reread reuses durable
   receipts and performs no completion or embedding calls.
5. The reader cancels pending pre-reset debounce work and freezes relocation reporting while the
   confirmation/reset is active. On success it clears welcome-back state, refreshes the server-clamped
   manifest, and seeks to the first section.
6. Existing catalogs gain epoch zero through an additive catalog shape migration. Exact and portable
   schema-v2/v3 archives may omit the column; restore adds it only in staging. Current backups preserve
   the epoch.

Partial arbitrary frontier rewinds are rejected for now: they create ambiguous “which pass owns this
memory?” semantics and add no capability beyond the existing Codex scrubber plus an explicit new pass.
Deleting memory on reset is also rejected because it destroys useful local work and can cause spend.

## Verification and review

Catalog/API tests cover atomic reset, repeated resets, stale and missing epochs, input bounds, title
re-clamping, and preservation of ingest progress. The reread integration test proves unchanged durable
receipts/costs and zero provider calls. Archive tests cover current epoch preservation and v2/v3
epoch-zero migration in exact and portable modes. Frontend tests cover debounce cancellation,
confirmation, safe focus/Escape/Tab behavior, local welcome-back clearing, API epoch use, axe, and type
checking.

Two inline adversarial passes were performed. Pass 1 found a relocation-window race, misleading
post-commit retry behavior, and an empty focus trap while busy; all were fixed. Pass 2 re-attacked
epoch concurrency, restore defaults, and failure recovery and found no unresolved blocker.
