# ADR 0002 — Bitemporal schema, spoiler-safe DAL & multi-book storage

**Status:** **Accepted** (2026-06-26). The schema + DAL are implemented and validated by a
31-check executable proof against a real two-book SQLite store, and survived **two**
adversarial Opus review passes (the first found a spoiler-leak **BLOCKER** that was fixed and
re-proven). Covers three build-blocker tickets settled in tandem.
**Date:** 2026-06-26
**Tickets:** LIT-5 (bitemporal schema + spoiler-safe DAL — keystone), LIT-18 (multi-book storage layout), LIT-19 (raw-text retention / re-extraction) — all build-blockers.
**Spike code:** [`spikes/lit-5-schema/`](../../spikes/lit-5-schema/) — `schema.sql` (per-book DDL), `catalog.sql` (global DDL), `dal.py` (the DAL), `demo.py` (the 31-check proof). Run: `python3 spikes/lit-5-schema/demo.py`.
**Builds on:** [ADR 0001](0001-epub-chapter-segmentation.md) — the chapter atom and the content-identity `chapter_key`.

## Context

Spoiler-safety by construction is the product's whole differentiator. The chapter atom (ADR 0001)
gives us a `revealed_at` ordinal; this ADR locks **where the memory lives**, **how it is shaped**,
and **the one read path every view goes through** — so that no future view can silently leak. Three
blockers had to be decided together because each constrains the others:

- **LIT-18** — one DB per book, or one DB partitioned by `book_id`? (The "second book is immediate.")
- **LIT-19** — retain raw chapter text, or store only derived memory? And how is memory migrated when the prompt/model/schema improves?
- **LIT-5** — the bitemporal tables and the data-access layer that makes the spoiler filter unbypassable.

## Decision

### 1. Storage layout (LIT-18): per-book file + a global catalog

**One SQLite file per book** (`books/<book_id>/memory.db`) **+ one small global `catalog.db`.**

- *Isolation is automatic and physical:* a KNN/SELECT in one file **cannot** return another book's
  rows — cross-book contamination is impossible by construction, not by a `WHERE` we might forget.
- *Backup / export / delete = file operations.* Per-book portability falls out for free.
- *Concurrency* is naturally per-book (one writer per file; WAL for concurrent readers — LIT-22).
- The global **`catalog.db`** holds the shelf (`books`), the **mutable reading state**
  (`reading_state.bookmark`/`cfi`/`ingest_progress`), and `cost_ledger` (per-book cost-to-date).
- **`book_id` is still carried on every fact row** as a reserved logical hook (see §4) so the exact
  same DAL works unchanged if files are ever collapsed into one multi-book DB (a hosted/multi-tenant
  future) or attached together. *Rejected:* one DB with `book_id` everywhere now — it makes the
  spoiler+isolation filter a thing you must remember on every query, the opposite of the goal.

### 2. Raw-text retention (LIT-19): retain, local-only, version-stamped

**Retain bookmark-bounded raw chapter text** (`raw_chapters`) **locally** as ground truth.

- *Why:* enables RAG quote-level answers and **cheap re-extraction** when the prompt/model/schema
  improves, without re-opening the user's file. The alternative (store only derived memory) makes
  "re-derive better memory later" impossible without the original file.
- *Legal/privacy:* it is the user's own legally-imported content (DRM-free / public-domain per D9),
  stored **local-first only**. Any hosted/synced retention is **explicitly gated** behind a separate
  future policy (flagged for LIT-24 / productization) — not authorized by this ADR.
- *Read path:* raw text is read **through the same spoiler funnel** (`view.raw_text()`), so a future
  chapter's text is invisible — it is **not** reachable only via the audit hatch.
- *Migration:* every derived row carries `schema_version` + `extractor_version`. A better extractor
  **re-extracts** from retained text and **supersedes** old rows in transaction-time (see §3/§4);
  mixed versions coexist; re-ingest of unchanged content is idempotent (content-hash skip).

### 3. The two temporal axes (the core of LIT-5)

The ticket's leaning named only `revealed_at`/`invalid_at`. LIT-19's "re-extract when the model
improves" **forces a second axis**. The schema is therefore bitemporal on **two independent axes**:

| Axis | Columns | Measured in | Drives |
|---|---|---|---|
| **Valid-time** (story) | `revealed_at`, `invalid_at` | chapter ordinals | the spoiler filter + the time-travel scrubber |
| **Transaction-time** (ingestion) | `schema_version`, `extractor_version`, `recorded_at`, `retracted_at` | versions / wall-clock | re-extraction (LIT-19), audit, rollback |

Conflating them would be a correctness bug either way: treating a re-extraction as a plot change
would leak/garble the story; treating a plot change as a re-extraction would lose history. Keeping
them separate is what lets a *better extraction* of chapter 3 and a *plot supersession* at chapter 20
coexist without interference. (Graphiti/Zep bitemporal pattern, arXiv:2501.13956.)

### 4. The schema (typed tables, uniform invariant)

Typed tables (not a single fact table): `book_meta`, `chapters`, `raw_chapters`,
`chapter_summaries`, `entities` (character|place|faction|object), `aliases`, `edges`, `events`,
`event_participants`, `themes`, `entity_state`, `chunks`. (Themes are a separate table — no
alias/coreference need; places/factions/objects are `entities` by `type` so they share one
resolution pipeline.) Full DDL: [`schema.sql`](../../spikes/lit-5-schema/schema.sql).

**Uniform invariant:** every fact-bearing table has a `revealed_at` column (uniformly named — it
means "first revealed" on `entities`/`themes`) so the single filter clause is universal. Tables whose
facts can change in story-time (`edges`, `events`, `themes`, `entity_state`) also carry `invalid_at`,
with `CHECK (invalid_at IS NULL OR invalid_at > revealed_at)` to reject inverted/zero-width windows.
`event_participants` is a **pure link** (no temporal stamps): event visibility comes from `events`,
entity visibility from `entities` — one source of truth, no denormalization to drift.

**`book_id` hook:** present on every fact row; the DAL always filters it (defense-in-depth even
inside a per-book file) and scopes every write to it — so the "unchanged DAL after a single-DB
collapse" claim holds for reads *and* writes.

### 5. The bookmark, and time-travel

The **bookmark is an integer chapter ordinal** and is **not stored in `memory.db`** (which is
immutable ground truth). It lives in `catalog.db.reading_state` and is **passed into**
`MemoryDB.view(bookmark)`. Consequences:
- *Time-travel is trivial:* pass a smaller integer → the store renders as of that chapter.
- *The DAL has no mutable "current position"* to get out of sync.
- The integer is the *highest fully-read chapter*. LIT-12 owns the continuous-CFI → integer mapping
  and any later sub-chapter frontier; the DAL's contract is purely integer-ordinal (see limitations).

### 6. The DAL contract — spoiler-safety made structural

Reads are reachable **only** through `MemoryDB.view(bookmark) → BookmarkView`. Four enforcement
layers (strongest first), all proven in `demo.py`:

1. **SQLite authorizer (engine-level, per-connection).** Each connection denies `SQLITE_READ` on any
   fact table unless *this connection's* guard flag is engaged — which only the DAL's own `_select`
   (reads) and `_writer` (ingestion) do. A raw `SELECT … FROM entities` on the connection is **denied
   by SQLite itself**. Per-connection (not a global thread-local) so a writer on book B can't unlock
   a raw read of book A.
2. **Single filter funnel.** Every read goes through one method, `_select`, which always appends
   `book_id=? AND revealed_at<=? AND retracted_at IS NULL [AND (invalid_at IS NULL OR invalid_at>?)]`.
3. **Referential closure.** The per-row filter is necessary but **not sufficient** — a visible row may
   *reference* a future entity. So every entity-referencing read semijoins the visible-entity set, and
   chunk/summary reads semijoin the live-chapter set. No read can surface an unmet entity or an
   orphaned-chapter chunk. *(This closed the BLOCKER from review pass 1.)*
4. **Required bookmark.** No `view` without a bookmark; `_select` rejects `bookmark=None`.

**Threat-model honesty.** Python has no true `private`, and the authorizer guards only the DAL's own
connection. A second `sqlite3.connect(path)` with no authorizer, or willfully flipping the guard
flag, bypasses — `demo.py` demonstrates the second-connection boundary explicitly. The guarantee is:
**no accidental bypass through the DAL's own connection**, which the app makes the sole owner of. The
funnel's `cols`/`where_extra`/`order` are **internal literals only** — never user input (user data
only ever flows as bound `params`), so the f-string assembly is not an injection vector. This is the
realistic, defensible guarantee for a local-first app; a stricter posture (e.g. SQL views per
bookmark) is recorded as a possible future hardening.

## Worked examples (all asserted in `demo.py`, 31/31 pass)

- **Spoiler block** — a fact at `revealed_at=30` is invisible at bookmark 10 across structured reads
  *and* the KNN/RAG path.
- **Referential closure** — an edge/alias/state/event-participant pointing at a future entity is
  hidden at bookmark 10 and correctly appears once the entity is revealed.
- **Supersession (valid-time)** — `engaged` (rev 2) → `estranged` via an atomic, gap-free
  `replace_edge`; the two never coexist and the relationship is never momentarily absent.
- **Time-travel** — the cast grows with the bookmark; Alyosha's state moves monastery → town.
- **Multi-book isolation** — per-file separation + the `book_id` hook; a writer on book B can't read
  book A raw.
- **Re-extraction (transaction-time)** — a better extractor supersedes old summary/entity rows
  (current reads update, history is auditable, no "double vision"); raw text is available to
  re-derive; re-ingest is idempotent; retracting a chapter cascades to its chunks/summaries.

## Adversarial review

### Pass 1 (2026-06-26) — design + rev-1 code. 5 lenses → verifier; 27 raw → consolidated.

| # | Sev | Finding | Disposition |
|---|---|---|---|
| 1 | **BLOCKER** | **Referential leak** — a visible edge/alias/event/state can reference an entity whose own `revealed_at` is in the future; the per-row filter never checks the referenced entity. The character graph would surface unmet characters + spoilery labels. | **FIXED & RE-PROVEN.** Referential closure (semijoin visible entities) on all entity-referencing reads; chunk/summary reads semijoin live chapters. New `demo.py` §2 reproduces the attack and asserts it is hidden until reveal. |
| 2 | HIGH | `event_participants` denormalized `revealed_at`/`invalid_at` = a second source of truth that drifts; an invalidated event leaks via `events_for()`/`bio()`. | **FIXED.** `event_participants` is now a pure link; event visibility comes from `events` via the join — one source of truth. |
| 3 | HIGH | `chapters` PK + live-unique ordinal index make re-segmentation/transaction-time history impossible; delta-skip on content-hash silently no-ops an ordinal-only change. | **PARTIALLY FIXED + documented.** Delta-skip now keys on `(content_hash AND revealed_at)` and re-stamps an ordinal change in place. Full bulk-renumber is a two-phase routine, specified under "open follow-ups" (chapter_key is the content-identity key; realistic re-segmentation creates *new* keys, so same-key retract+re-add is intentionally not supported). |
| 4 | HIGH | Global thread-local guard: a writer on book B permits raw fact reads of book A on the same thread. | **FIXED.** Per-connection guard flag; `demo.py` §5 asserts B's writer does not unlock A. |
| 5 | HIGH | `raw_chapters` had no spoiler-safe read path (`_select` would crash; only the audit hatch read it). | **FIXED.** `raw_chapters` got `retracted_at`/`recorded_at`; `view.raw_text()` reads it through the funnel; future text hidden (asserted). |
| 6 | HIGH | No transaction-time supersede for the graph → re-extraction makes duplicate live rows ("double vision"); `reextract_summary` hardcoded `kind='chapter'`. | **FIXED** (refined in pass 2). Generic `_retract` (retract-then-insert by a stable non-FK key, used by `reextract_summary`/`retract_chapter`); `reextract_summary` takes `kind`. Entity re-extraction is **identity-preserving in-place** (`reextract_entity(entity_id, …)`) so FK sub-graphs stay valid — see pass 2 #3. Asserted one live row. |
| 7 | MED | Inverted/zero-width validity windows accepted silently. | **FIXED.** `CHECK (invalid_at IS NULL OR invalid_at > revealed_at)` on all valid-time tables; asserted rejected. |
| 8 | MED | Supersession could leave a validity gap (neither row live). | **FIXED.** Atomic gap-free `replace_edge`/`replace_state`; asserted no gap across the transition. |
| 9 | MED | Overlapping un-superseded rows can coexist (supersession is caller discipline). | **MITIGATED.** The atomic `replace_*` primitive is the sanctioned path; the per-chapter extractor must use it. Documented as an extractor contract. |
| 10 | MED | Retraction doesn't cascade → orphaned chunks/summaries stay readable. | **FIXED.** `retract_chapter` cascades; chunk/summary reads also semijoin live chapters. Asserted. |
| 11 | MED | vec0 spoiler **pre-filter recall** asserted but the prototype proves a weaker property (Python cosine over the filtered set). | **DOCUMENTED.** Claim softened in schema/dal; routed to a dedicated vector spike (open follow-ups). Per-file `book_id` isolation is genuine; the `revealed_at` *range* pre-filter recall in real vec0 is unproven. |
| 12 | LOW | Non-deterministic tie-break (e.g. `current_state` on same-chapter rows). | **FIXED.** Unique secondary sort keys added (`state_id DESC`, `summary_id`, `event_id`). |
| 13 | LOW | Writes (`supersede`/`reextract`) not `book_id`-scoped → breaks the collapsed-DB claim. | **FIXED.** All writes now `AND book_id=?`. |
| 14 | MED | `catalog.db` ↔ `memory.db` consistency unenforced; wrong `book_id` fails open to empty. | **PARTIALLY FIXED.** `book_meta` identity assert on open (raises on mismatch). Backup/restore-together, orphan scan, atomic cross-file delete are app/ops concerns documented for LIT-24. |
| 15 | MED | `revealed_at` is a caller-supplied integer, unvalidated against the chapter. | **DOCUMENTED** as the extractor contract; referential closure already blocks the dangerous *referential* form. Deriving `revealed_at` from `chapter_key` where present is a build-time hardening note. |
| 16 | MED | Integer bookmark can't represent a sub-chapter frontier (partial chapter all-or-nothing). | **DOCUMENTED.** Policy: chapter memory reveals only once the chapter is fully read; LIT-12 owns any sub-chapter frontier. |
| 17 | LOW | No-bypass is per-connection only; a 2nd raw connection reads everything. | **ALREADY HONEST** — scoped in the threat model; `demo.py` §8 now demonstrates the boundary explicitly. |

### Pass 2 (2026-06-26) — re-attack of the revised (rev-2) code. Verdict: FIX_THEN_SHIP → fixed.

3 lenses (residual spoiler-leak · threat-model/injection · regression) → verifier; each finding
reproduced with an independent probe script against rev-2. It **confirmed** the rev-2 fixes
(referential closure on relationships/aliases/state/events/bio; per-connection guard; cascade;
gap-free supersession; CHECK; `book_meta` assert; and that the funnel is **not** SQL-injectable —
user data is always bound, `cols`/`where_extra`/`order` are internal literals). It found that the
referential-closure fix-class had been applied **incompletely** — two sibling methods were missed:

| # | Sev | Finding (all reproduced) | Disposition |
|---|---|---|---|
| 1 | HIGH | **`participants_of()` leaked the cast of a FUTURE / story-invalidated event** — it gated the participant *entities* but never the parent *event* (`revealed_at`/`invalid_at`/`retracted_at`); its sibling `events_for()` did. A hidden plot beat's cast was reachable via an enumerable `event_id`. | **FIXED & PROVEN.** Added the `EXISTS` event-visibility gate mirroring `events_for`. `demo.py` §2 asserts a future event's and an invalidated event's cast are both empty, and reappear after reveal. |
| 2 | HIGH | **`raw_text()` leaked future-chapter raw prose** — it applied the per-row frontier but omitted the live-chapter semijoin its siblings (`chunks`, `chapter_summaries`) have, so a raw row mis-stamped `revealed_at<=bookmark` under a future parent chapter surfaced. | **FIXED & PROVEN.** Added the `_live_chapters()` semijoin. `demo.py` §6 asserts a mis-stamped future raw chapter returns `None`. |
| 3 | MED | **`reextract_entity` collapsed two distinct same-name entities into one** (the name-keyed retract hit both live rows) and orphaned the re-extracted entity's sub-graph. Fails *closed* (under-reveals) but is silent data loss. | **FIXED & PROVEN.** Re-extraction is now **identity-preserving** (`reextract_entity(entity_id, …)`, in-place, FK-safe); two same-name entities are never collapsed. `demo.py` §6 asserts one row, new name, stable id, alias FK intact. |

After these fixes the harness is **35/35**. Remaining items the reviewer judged acceptable to
**document** (all fail *closed* — they under-reveal, the safe direction for a spoiler product):

- **Bulk ascending re-segmentation renumber** collides with the `ux_chapters_ordinal` unique index
  (fails closed with rollback). Needs a two-phase renumber primitive → open follow-up / LIT-7.
- **Intra-chapter (same-ordinal) valid-time supersession** is rejected by `CHECK(invalid_at > revealed_at)`.
  This is intentional: chapter-ordinal grain cannot order two changes within one chapter; same-chapter
  corrections are **transaction-time** (re-extraction), not valid-time. Documented; revisit grain with LIT-6/LIT-12.
- **SQL-injection** hardening (whitelist the funnel's table arg) — adopted: `_select` now rejects any
  non-fact table.

## Consequences & open follow-ups (routed, not hidden)

- **vec0 pre-filter recall** — build a minimal real `sqlite-vec` table and prove (a) no
  `revealed_at>bookmark` chunk can enter top-k and (b) spoiler chunks don't crowd out relevant
  in-frontier chunks. → new vector spike / coordinate with LIT-6.
- **Sub-chapter frontier** — the DAL is integer-ordinal; the partial-chapter reveal policy and any
  CFI→sub-ordinal extension are **LIT-12**.
- **Re-segmentation bulk renumber** — a two-phase (offset-to-temp-range, then final) transactional
  renumber that re-stamps dependent facts; specified here, coded with the ingestion pipeline (LIT-7).
- **Intra-chapter valid-time grain** — multiple state changes within one chapter can't be ordered by
  chapter ordinal alone; if needed, add a sub-chapter `order_idx` to the valid-time key (coordinate
  with LIT-6/LIT-12).
- **catalog ↔ memory lifecycle** — backup/restore-together, orphan cleanup, atomic delete across both
  files → **LIT-24**; hosted raw-text retention policy → **LIT-24 / productization**.
- **Extractor contract** — `revealed_at` correctness and explicit supersession are obligations of the
  extraction pipeline → **LIT-6**.

## Outcome

A two-axis bitemporal schema in per-book SQLite files behind a global catalog, read **only** through a
DAL whose spoiler filter is centralized in one funnel, enforced by a per-connection SQLite authorizer,
and made **referentially closed** so no read can surface an unmet entity. Raw chapter text is retained
locally and version-stamped for cheap re-extraction. Validated by a 31-check proof and two adversarial
passes — the first of which caught a real spoiler-leak blocker that is now fixed and re-proven. This
defines the storage + read contract that **every** downstream view (LIT-14/15), the extraction
pipeline (LIT-6), the reader frontier (LIT-12), and the spoiler-eval harness (LIT-8) build on.
**Accepted** for LIT-5, LIT-18, LIT-19; the routed follow-ups above harden the long tail.
