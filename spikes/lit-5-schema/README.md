# LIT-5 spike — bitemporal schema + spoiler-safe DAL

The keystone spike. Every view in the product reads the story memory through this
layer, so this is where **spoiler-safety becomes structural** rather than a model
promise. Also settles **LIT-18** (multi-book storage layout) and **LIT-19**
(raw-text retention / re-extraction).

## Run it

```bash
python3 spikes/lit-5-schema/demo.py     # stdlib only; exits non-zero on any failure
```

It builds two real SQLite books through the DAL and asserts every exit criterion.
All 35 checks pass. The design survived two adversarial Opus review passes (the first
caught a real spoiler-leak **BLOCKER**; the second caught two residual HIGH leaks) —
all fixed and re-proven here. See [ADR 0002](../../docs/adr/0002-bitemporal-schema-and-dal.md).

## Files

| File | What it is |
|---|---|
| `schema.sql` | **Committed DDL** for a per-book `memory.db` — every fact table carries `revealed_at` (+ `invalid_at` where facts change in story-time) and the transaction-time stamps (`schema_version`, `extractor_version`, `recorded_at`, `retracted_at`). |
| `catalog.sql` | **Committed DDL** for the one global `catalog.db` — the shelf (`books`), the mutable reading state (`reading_state.bookmark`/`cfi`), and `cost_ledger`. |
| `dal.py` | The spoiler-safe DAL. `MemoryDB` (private connection + SQLite authorizer) → `view(bookmark)` → `BookmarkView`. The single `_select` funnel applies the canonical filter; nothing else can read a fact table. |
| `demo.py` | Worked examples + the executable proof harness (the 21 checks). |

## The two temporal axes (the core idea)

| Axis | Columns | Measured in | Drives |
|---|---|---|---|
| **Valid-time** (story) | `revealed_at`, `invalid_at` | chapter ordinals | the spoiler filter + the time-travel scrubber |
| **Transaction-time** (ingestion) | `schema_version`, `extractor_version`, `recorded_at`, `retracted_at` | wall-clock / versions | re-extraction (LIT-19), audit, rollback |

The ticket's leaning only named the valid-time pair; LIT-19's "re-extract when the
prompt/model improves" **requires** the second axis, so the schema is bitemporal on
both. Conflating them would either leak spoilers (treating a better extraction as a
plot change) or corrupt history (treating a plot change as a re-extraction).

## The canonical spoiler read (one place: `dal._select`)

```sql
book_id = :book
AND revealed_at <= :bookmark
AND retracted_at IS NULL                              -- current transaction view
AND (invalid_at IS NULL OR invalid_at > :bookmark)    -- valid-time tables only
```

`bookmark` is an integer chapter ordinal. It is **not** stored in `memory.db` (which
is immutable ground truth) — it lives in `catalog.db.reading_state` and is passed
into `view(bookmark)`. Time-travel = pass a smaller integer. LIT-12 maps the reader's
continuous CFI position onto this integer frontier.

## How a raw read is blocked (exit criterion 2)

Four layers, strongest first:
1. **SQLite authorizer (per-connection)** — denies `SQLITE_READ` on any fact table unless
   *this connection's* guard flag is engaged (only `_select`/`_writer` do). `demo.py` proves
   `conn.execute("SELECT … FROM entities")` raises `DatabaseError` even with the raw
   connection in hand, and that a writer on book B can't unlock a raw read of book A.
2. **Single filter funnel** — every view read goes through `_select`, which always appends
   the spoiler clause. The harness inspects the emitted SQL (via `set_trace_callback`) and
   asserts all 17 view-path reads carry `revealed_at <=` and `book_id =`.
3. **Referential closure** — entity-referencing reads semijoin the visible-entity set and
   chunk/summary/raw-text reads semijoin the live-chapter set, so no read surfaces an unmet
   entity or an orphaned-chapter row (the necessary-but-not-sufficient gap the per-row filter
   alone leaves — this is what the two review passes hammered).
4. **Required bookmark** — reads are only reachable via `view(bookmark)`; there is no API
   that returns rows without a frontier.

Honest scope: Python has no true `private`, so willful circumvention (setting the
guard flag by hand) is possible — but no *accidental* bypass is. See ADR 0002 for the
threat model.

## What each check proves

1. **Spoiler block** — future facts (`revealed_at > bookmark`) are invisible to
   structured reads *and* the KNN/RAG path.
2. **Supersession** — an edge invalidated at chapter N flips cleanly; old & new never
   coexist at any bookmark.
3. **Time-travel** — the same store renders differently as of different bookmarks
   (cast grows; Alyosha's state moves monastery → town).
4. **Multi-book isolation** — per-file separation + the `book_id` hook; a KNN in book A
   never returns book B's chunks, nor a stray foreign-`book_id` row inside A's file.
5. **Re-extraction** — a better extractor supersedes old rows (transaction-time);
   current reads update, history stays auditable, re-ingest is idempotent (content hash).
6. **No-bypass** — the three enforcement layers above.

See `../../docs/adr/0002-bitemporal-schema-and-dal.md` for the full decision record.
