# ADR 0007 — Backend service architecture (productionizing the validated cores into a FastAPI service)

**Status:** **Accepted** (2026-06-28) — survived **two** adversarial Opus review passes (pass 1: 5
reviewers, 3 BLOCKER-class + several HIGH; pass 2: 4 reviewers re-attacking the fixes, found genuine
regressions the pass-1 fixes introduced — all incorporated below). This is the *architecture decision*
for Step A of the MVP build; it does not yet ship code. Each spoiler-critical module it produces
(Store/DAL, segmentation + divider-merge, the spoiler gate, the LLM client) gets its own two-pass
review as it lands.
**Date:** 2026-06-28
**Ticket:** Build Step A (backend skeleton). Touches LIT-5/LIT-18/LIT-19 (store), LIT-6 (extraction), LIT-20 (LLM/embed), LIT-4 (segmentation + divider-merge), LIT-7 (ingestion), LIT-8 (gate), LIT-12 (frontier).
**Builds on:** ADR 0001–0006 (all Accepted). It **productionizes** them; it does **not** re-decide any
of their internals. Where an accepted invariant exists, this ADR carries it through unchanged — and
names where the production runtime (concurrency, real models, real vectors, re-segmentation) could
*break* it.
**Spike inputs:** a 6-agent structured productionization map (`map-backend-spikes`, 2026-06-28) over
`spikes/lit-{4,5,6,8,12,20}-*` + firsthand read of `spikes/lit-5-schema/dal.py`.

**Superseding implementation note (2026-07-14, LIT-34):** D-A4's vec0 gate is now satisfied by the
LIT-26 real-vector/10k-vector spike and the production parity/falsifiability suite. `vec0` is the
default; brute force remains the exact reference/fallback. The vec0 table is authorizer-guarded, its
known 0.1.9 shadows alone are infrastructure, and spoiler eligibility is metadata-prefiltered before
KNN. Canonical JSON vectors drive portable recovery and fail-closed index verification. Version 0.1.9
scans within its filter; this decision makes no sublinear performance claim.

---

## Context

The research phase is closed: 8 build-blockers Accepted, validated as stdlib-first reference
prototypes under `spikes/`. The prototypes prove the *designs* and contain reusable logic, but they
are not the product — they use sibling `sys.path` imports, a JSON-in-a-column vector stand-in,
hardcoded fixture paths, a lexical-embedding stub, a `set_trace_callback` test hook, and
`print`/`sys.exit` harnesses, and they are **single-threaded, single-file, with no re-segmentation and
no scrubber**.

Step A turns them into a real **Python + FastAPI** service (D10) without weakening a single
spoiler-safety invariant the spikes established, in a runtime that has the concurrency, real models,
real vectors, and re-segmentation the spikes never exercised. The map + two review passes surfaced the
places where the naïve port silently breaks safety; those drive this ADR:

1. **The DAL's `_engaged` guard flag is per-connection and only safe while a single caller holds it.**
   The authorizer denies raw fact reads unless `db._engaged` is set; `_select`/`_writer` flip it on and
   restore it. If two callers touch one `MemoryDB` connection and one is mid-write (`_engaged = True`)
   while another issues a raw `SELECT`, the raw read is **permitted** — an accidental bypass.
2. **`sqlite3` connections default to `check_same_thread=True`** and raise off-thread, so the
   "one long-lived connection per book reused across threadpool requests" model can't run on the
   verbatim spike code; `check_same_thread=False` then makes the per-book lock the **sole** serializer.
3. **`sqlite-vec` `vec0` pre-filter recall over the `revealed_at` range is UNPROVEN** (routed to a
   vector spike). Building RAG on `vec0` now means building on an unproven spoiler-recall foundation.
4. **The integer bookmark is a *derived ordinal*** persisted in `catalog.reading_state` (a different
   file from `memory.db`); a change to the chapter-atom numbering can desync it and leak future chapters.
5. **The ADR-0001 `<200`-word divider-MERGE is flagged, not implemented**, yet LIT-12's alignment + no
   -zero-length preconditions depend on it.
6. **The spike's append-once guard and `FACT_TABLES` authorizer set are load-bearing and easy to break
   in a port** (pass 2 caught both — see D-A3/D-A6).

## Decision

### D-A1 — Single backend package; pure safety cores lifted verbatim, the rest re-ported with named changes

One installable package under `backend/`. Two honest tiers of reuse:

- **(a) Lifted near-verbatim — pure, behaviour-defining safety logic** (a rewrite *is* a spoiler-gate
  change; gets its own two-pass review): the `_select` funnel, `_authorizer`, `_WriterCtx`, the
  referential-closure helpers and every `BookmarkView` read method; `resolve.py`; `frontier.py`; the
  spoiler-gate deterministic functions (`score_recap`, `validity_snapshot`, `cache_key`,
  `reveal_correctness_eval`, `supplied_facts`, `read_text_upto`, `PROLEPSIS_RE`, `SYNTH_SYSTEM`);
  `versioning.safe_swap`/`current_identity`; the explicit `FACT_TABLES`/`VALID_TIME_TABLES` sets
  (incl. `book_meta` — see D-A6).
- **(b) Re-ported with behaviour-relevant changes** (named, not hidden under "verbatim"): `llm/client.py`
  (Pydantic-validated structured output + resilience — D-A5); `memory/vectors.py` (the cosine ranker
  extracted from the fused `search()` — D-A4); `ingest/extraction/pipeline.py` (append-once guard
  re-sourced off the `_audit_all` hatch — D-A3). The `set_trace_callback`/`executed_sql` test
  instrumentation is **excluded** from the production lift (or gated behind a debug flag).

```
backend/
  pyproject.toml                 # deps + tooling (uv-managed venv); ruff + pytest
  app/
    main.py                      # FastAPI app factory + lifespan (open catalog, register vector backend)
    config.py                    # pydantic-settings; loads repo-root .env via absolute __file__-derived path
    deps.py                      # FastAPI deps (Store, Catalog, LLMClient)
    api/{books,reading,views,ingest}.py
    memory/                      # LIT-5 keystone + LIT-19 + LIT-20 enforcement (the store)
      dal.py                     # MemoryDB + BookmarkView + _select + _authorizer + _WriterCtx (LIFTED a)
      vectors.py                 # ranking strategy: BruteForce (default) | Vec0 (gated) — ranks _select-filtered candidates
      store.py                   # per-book connection manager — sole owner; per-book threading.Lock; lease/evict
      migrations.py              # forward-only versioned migration runner, run on the owned conn under the lock
      schema/memory.sql          # per-book DDL baseline (v1) — lifted + LIT-20 columns + derived-table append-once indexes
    catalog/
      catalog.py                 # global catalog.db: books shelf + reading_state + cost_ledger (LIFTED catalog.sql); global lock
      schema/catalog.sql
    ingest/
      segmentation/{epub_segmenter,signals,models}.py   # LIFTED LIT-4 + divider-MERGE (D-A8)
      extraction/{schema,prompts,resolve,pipeline,chapter_text}.py  # LIT-6 (LIFTED a where pure)
    llm/{client,versioning}.py   # LIT-20; stub preserved; client re-ported (b)
    reader/frontier.py           # LIFTED a, pure
    eval/spoiler_gate/{structured,rag,synthesis,cache}.py  # LIT-8 — first-class, shared with runtime synth/cache
  tests/
    eval/test_spoiler_gate.py    # gate against a real store + falsifiability self-tests; BLOCKS merge
    ...                          # unit + fixture-EPUB regression + concurrency stress + the targeted tests named below
```

Per-book data + the global `catalog.db` live under a git-ignored `data/` dir
(`data/books/<book_id>/{memory.db,source.epub}`, `data/catalog.db`), configurable via settings.

### D-A2 — Connection ownership & concurrency: sole-owner + per-book lock over the *whole* operation

A **per-book `Store`** (`memory/store.py`) is the **sole** owner of each book's `sqlite3` connection
and the **only** path to a `MemoryDB`. The authorizer's guarantee holds only if the connection is
never used by two callers concurrently *and* the lock spans the whole logical operation (pass-1 B2):

- **One connection per book, opened with `check_same_thread=False`** (pass-1 HIGH). Because that
  disables sqlite3's own thread guard, the **per-book `threading.Lock` is the load-bearing safety +
  memory primitive**. Every connection (memory and catalog) sets `PRAGMA busy_timeout` so transient
  contention retries rather than raising (pass-2).
- **All access is through `with store.book(book_id) as view:`**, which acquires that book's lock and
  holds it for the **entire** read render (or write), then releases on exit. The `BookmarkView`/raw
  connection must not be retained past that context — and this is now **ENFORCED, not just asserted**
  (memory-review pass-1 HIGH): the handle records the owning thread (`_active_owner`) for the session;
  every DB access chokepoint (`_select`, `_WriterCtx.__enter__`) calls `_assert_in_session()`, so an
  escaped view/handle used off-session (a different thread, or after the block) **fails LOUD** with a
  `RuntimeError` instead of racing/deadlocking the `check_same_thread=False` connection. This makes a
  multi-statement view a single consistent snapshot and every `_engaged` flip atomic w.r.t. other
  callers. The inner
  `_select`/`_writer` `_engaged` save/restore nests correctly within one thread, so the lock is
  acquired exactly once at the boundary and **no reentrant lock is needed** (pass-2 confirmed). Reads
  on *different* books run concurrently; same-book ops are fully serialized.
- **Both readers and the ingestion worker contend on the same per-book `threading.Lock`.** The worker
  runs in the threadpool (dedicated worker thread / `run_in_executor`), **not** a bare event-loop task
  or `BackgroundTasks` (pass-1 B1).
- **Lookup-or-open is guarded** by a Store-level lock so two concurrent misses for one book resolve to
  **one** instance — never two connections to one file (shipped). **Lease + lock-coupled LRU eviction
  is DEFERRED** (memory-review MEDIUM, routed to LIT-22): the MVP keeps an *unbounded* per-process
  handle cache. Because handles are never evicted there is no evict-mid-op race, so the safety property
  the lease was meant to protect (no `close()` mid-op) holds trivially; the cost is unbounded handle
  retention, acceptable at single-reader MVP scale. (`Store.close()` does acquire each per-book lock
  before closing, so shutdown serializes against any in-flight session.)
- **Migrations run on the Store-owned connection, under the book's lock, before the book serves any
  request** (pass-1 MEDIUM); the authorizer is attached **after** the migration steps.
- **The raw connection is never handed out.** What closes the raw-`SELECT` bypass is this sole-owner
  rule (a raw `SELECT` doesn't go through `_select`, so the lock alone wouldn't stop it) — exactly ADR
  0002's "no accidental bypass through the DAL's own connection," now preserved under concurrency by the
  lock for DAL-path callers + sole-owner for the raw path. `_audit_all` stays out of every user-facing
  path (its only remaining use is migration/audit tooling — see D-A3).

`catalog.db` (the single global file: shelf, `reading_state`, `cost_ledger`) is the one shared writer
that is *not* per-book isolated. It is owned by a `Catalog` service holding one `check_same_thread=False`
connection guarded by a **global catalog lock**, with `busy_timeout` set; the worker's `ingest_progress`
high-water write is monotonic (`MAX(existing, new)`) so a lost update can't move it backward (pass-2).

### D-A3 — Execution model: fast reads sync-under-lock; slow ingestion off the request path; append-once kept as a real guard

- **Reads** are fast synchronous `def` handlers (threadpool), wrapped in `with store.book(id) as view:`.
  The only LLM call on a read path is recap synthesis, which is cached (LIT-8 `cache_key`) and lazy (D5).
- **Ingestion** **never blocks a request and never holds the per-book lock across the LLM/IO call**
  (pass-1 HIGH). `PUT /position` updates the bookmark immediately and enqueues ingestion of
  newly-completed chapters into a **threadpool worker, one per book, serialized**. The worker: (1)
  reads inputs under the lock, (2) **releases the lock and does the LLM/embedding work**, (3)
  re-acquires the lock only for the short DB commit. `_engaged` is never True across an IO boundary.
- **Append-once is a real guard, kept — not removed** (pass-2 HIGH P2-1): `add_chapter`'s delta-skip
  only de-dups the *`chapters`* row, but the pipeline goes on to INSERT non-idempotent **derived** rows
  (entities/aliases/edges/events/themes/summaries/chunks). So the pipeline **keeps an append-once
  early-return** — re-sourced off the `_audit_all` hatch onto a **lock-held ingestion-side guarded
  existence read** that the chapter is already live at this `content_hash` → `{skipped: true}`. (NB:
  `content_hash`/`retracted_at` are bookmark-independent ingestion facts, *not* story facts, so this is
  **not** a `BookmarkView` funnel read — the funnel exposes neither; it is the same engaged-connection
  existence query `add_chapter` already runs at `dal.py:164-176`, surfaced to the pipeline as a skip
  signal. This is the spike's behaviour, just off the `_audit_all` table-dump.) Belt-and-suspenders: **partial UNIQUE indexes on the derived tables**
  (e.g. `chapter_summaries(book_id, chapter_key, kind) WHERE retracted_at IS NULL`, and the
  chunk/entity equivalents) so the DB *also* rejects a double-write — **not** a redundant constraint on
  `chapters` (whose `chapter_key` is already PK). LIT-7 hardens crash-resumability (`ingest_progress`
  high-water); `GET /ingest` reports status. A **double-`ingest_chapter` test** asserts exactly one
  live row per derived fact.

### D-A4 — Vector store: the spoiler funnel stays in the DAL; `vectors.py` only ranks; the seam is pinned

Pass-1 flagged that the LIT-5/LIT-8 proof covers the **fused** `BookmarkView.search()`. So:

- **The candidate-set filter stays inside `BookmarkView.search()`** — it calls `_select("chunks", …)`
  (`book_id + revealed_at + retracted_at`) + the `_live_chapters()` semijoin + the same-space
  (`embed_model`,`embed_dim`) SQL gate, exactly as the spike does, and returns the proven 4-tuple
  `(cosine, text, revealed_at, chapter_key)`. **The seam is explicit** (pass-2 P2-12):
  `vectors.rank(candidate_rows, query_vec, k) -> ranked list` owns the cosine, the in-Python
  dim-mismatch defense-in-depth skip, the deterministic tie-break sort, and the top-k truncation;
  `search()` owns the funnel filter + same-space gate and returns `rank()`'s output. `vectors.py`
  **holds no DB handle and issues no SQL.**
- **`BruteForce` (default, ships now):** ranks only the funnel-produced candidates. Its spoiler-safety
  is a **two-conjunct, two-test** acceptance criterion (pass-2 P2-8): (1) a **drop-filter falsifiability
  test** (monkeypatch-drop the chunks filter inside `search()` ⇒ the LIT-8 harness reports leaks,
  mirroring ADR 0004 Vector 1) proves the funnel filter is load-bearing; (2) a **no-DB-access test**
  asserts `vectors.py` takes pre-fetched rows only and holds no connection, **plus** an authorizer-deny
  test that a raw `SELECT … FROM chunks` from a non-funnel path is DENIED by the SQLite authorizer.
- **`Vec0` (gated):** adopted **only after** a dedicated vector spike proves (a) the `revealed_at`
  range pre-filter loses no recall; (b) `vec0` KNN runs under the `_select`-equivalent funnel so its
  candidate set is still DAL-filtered; (c) the authorizer either guards or is provably-safely-bypassed
  for vec0's shadow tables (`chunks_vec_*`) — which must be on the D-A6 infra allow-list — with
  `enable_load_extension` scoped to the Store-owned connection only.

### D-A5 — Extraction: Pydantic models as the schema source of truth; OpenAI native structured outputs; downstream stays dict

Pass-1 + pass-2 established the spike contract is **dict-of-strings** based (`pipeline.py` indexes
`extraction["entities"]`; `e["type"]`/`rel["rel_type"]` flow as bare strings into TEXT columns;
`validate()` compares against string sets). Resolution:

- **`schema.py` defines Pydantic models + `str`-subclass enums** (e.g. `class EntityType(str, Enum)`) as
  the single source of truth, with `model_config = ConfigDict(extra="forbid")`.
- **OpenAI structured output uses the SDK's native strict helper** (`responses.parse` /
  `chat.completions.parse` with `response_format=<PydanticModel>`), which applies the strict-mode
  transform the spike hand-wrote (recursively `additionalProperties:false` + **every** property in
  `required`, optionals modelled as nullable, `$defs` inlined) — raw `model_json_schema()` does **not**
  satisfy strict mode by itself (pass-2 HIGH P2-3). The model returns a validated Pydantic instance,
  handed downstream **as a dict via `model_dump(mode="json")`** so enums serialize to their string
  values and `resolve.py`/`pipeline.py`/the gate keep their **dict-of-strings contract unchanged**
  (pass-2 P2-4). Strict `json_schema` is **OpenAI-specific**; other OpenAI-compatible providers use
  `json_object`/tool mode + the same strict transform where supported, with **Pydantic validation as
  the backstop** (a malformed/looser output → re-ask → fails toward under-extraction, which is safe).
  `complete(system, user, tier, schema) -> (dict, usage)` surface is preserved. **Instructor is
  optional/deferred** — Pydantic gives the validation/schema; the library adds nothing for the MVP
  OpenAI path and would churn the proven pipeline.
- **A parity test** proves the new validation path is equivalent to the spike's: the same gold +
  adversarial extraction objects yield **identical accept/reject** and **identical `model_dump()`
  dicts** as `extract_schema.validate()`; the unchanged dict contract downstream is then covered by the
  LIT-8 gate. (The validation swap is **not** "verified by the LIT-8 gate" — the gate is a spoiler
  gate, not a validation-equivalence oracle; pass-2 P2-7.)
- **Embeddings are configured independently** of completion (LIT-20), via the OpenAI-compatible
  embeddings endpoint, **routed around Instructor entirely**. `embed()` never lies about which embedder
  ran (`embed_identity()`; stub stamps `stub:lexical-stub-256`, surfaced as real logging).
- **Resilience:** timeouts + bounded retry/backoff on 429/5xx via the SDK. The **offline `stub`** is
  retained for CI/determinism. **Resolution layer-4 stays force-off** unless a real semantic embedder
  is configured.

### D-A6 — Schema: forward-only migrations, fail-closed *explicit* authorizer set, DB-enforced append-once on derived tables

- **`memory/schema/memory.sql` is the v1 baseline** (lifted DDL + LIT-20 `book_meta`/`chunks` columns +
  the **derived-table** partial-UNIQUE append-once indexes of D-A3). `migrations.py` is forward-only;
  a stored version newer than the code raises. **One create/open code path** (pass-1 MEDIUM): a new book
  is created from the v1 baseline, stamped `schema_version = 1`, **then walked through the same
  migration list to CURRENT** (the spike's hardcoded `schema_version = SCHEMA_VERSION` INSERT is not
  carried verbatim). Sound at CURRENT==1 (empty walk) and CURRENT>1 (pass-2 confirmed). A test creates a
  new book under CURRENT>1 and asserts every CURRENT column/table exists.
- **The authorizer set is an explicit, code-owned `FACT_TABLES` that INCLUDES the non-`revealed_at`
  guarded tables `book_meta` and `event_participants`** (pass-2 HIGH P2-2, confirmed firsthand against
  `schema.sql` — both lack `revealed_at` but MUST stay guarded: `book_meta` holds the pinned-model/
  `file_hash` row, `event_participants` the swimlane links; this is exactly the spike's `FACT_TABLES`).
  "Has a `revealed_at` column" is used **only** to
  compute `VALID_TIME_TABLES` (those that also have `invalid_at`) and to drive a **fail-closed superset
  assertion at open**: the declared `FACT_TABLES` must be a *superset* of every `revealed_at`-bearing
  base table, and every base table must be either a declared fact table or on an explicit infra
  allow-list (`sqlite_*`, the migration log, vec0 shadow tables) — else the open **fails**. The
  authorizer therefore stays **default-deny** for fact tables and is never silently auto-narrowed
  (deriving the set *from* the schema would fail open — rejected). Tests: a raw read of `book_meta` is
  DENIED; a fact table missing from `FACT_TABLES` fails the open rather than becoming readable.

### D-A7 — Config & secrets: `pydantic-settings`, absolute repo-root `.env`, fail-loud with an explicit predicate

`config.py` is a `pydantic-settings` `BaseSettings` whose `env_file` is an **absolute,
`__file__`-derived path to the repo-root `.env`** (`Path(__file__).resolve().parents[2] / ".env"` —
pass-2 confirmed `app/`→`backend/`→repo-root). Fields: `OPENAI_API_KEY`, `EMBED_PROVIDER`, optional
model/base-url overrides, `ANTHROPIC_API_KEY`, `DATA_DIR`, `VECTOR_BACKEND` (default `vec0` since
LIT-34; `bruteforce` retains the exact reference/fallback),
`ALLOW_STUB` (default `false`). **Fail-loud predicate is explicit** (pass-2 P2-14): at startup, if no
real LLM/embed provider resolves and `ALLOW_STUB` is not set, the app **hard-fails** (default-deny);
the stub path warns in tests (where `ALLOW_STUB=true`) and hard-fails a non-test deploy — one source of
truth, no warn-vs-fail ambiguity. The key is never written to a tracked file, a commit, or memory; a
committed `.env.example` documents the contract.

### D-A8 — Divider-merge implemented in segmentation, absorbing the divider's span (LIT-4 prerequisite)

A `<200`-word, label-only Part/Book divider (Karamazov "PART I" / "Book II") is **merged into the
following chapter**: the merged atom **absorbs the divider's span** — its CFI/char-range **start moves
back to the divider's anchor** (pass-1 MEDIUM) so the atom list stays contiguous and a reader in the
"PART I" text is inside a mapped atom; the merged `chapter_key` is the **following body chapter's**
content-identity key; the Part is captured in the existing accepted column **`part_label`** (pass-1:
not a renamed `part_group`); the divider's spine file yields zero own atoms and ADR-0001's per-body-file
coverage assertion treats it as **covered-by-its-successor**. `revealed_at` = the 1-based ordinal among
included, post-merge atoms. **Acceptance:** the segmenter re-runs LIT-12's `assert_aligned` against the
real Karamazov atoms and proves contiguity (1..N, zero-length-free).

### D-A9 — The spoiler gate is a first-class package, shared by runtime, with a pinned `read_text` contract

`eval/spoiler_gate/` lives in the app (not `tests/`) because its deterministic functions
(`score_recap`, `validity_snapshot`, `cache_key`, `supplied_facts`) are imported by the **runtime**
synthesis + recap-cache paths, so production enforcement and the eval use **identical** logic. The
pytest entrypoint runs the full harness + falsifiability self-tests + the D-A4 BruteForce tests against
a real store and **blocks merge**.

**The runtime gate's argument contract is pinned** (pass-2 HIGH P2-5 — `read_text` drives the
reader-parity DROP block that ADR 0004 rev-2 fixed from fail-open):

- All four — `supplied_facts`, `score_recap`, `validity_snapshot`, `cache_key` — receive the **clamped,
  version-current `effective_bookmark`**, computed **once** at the route boundary and threaded through
  **both** the view read and the synthesis/gate path (never re-sourced from the raw request; pass-2 P2-6).
- Runtime synthesis MUST call `score_recap(db, effective_bookmark, recap, read_text=read_text_upto(db,
  effective_bookmark))`, where `read_text` is assembled **only** from `raw_chapters` with
  `revealed_at <= effective_bookmark` (read through the funnel under the lock). `read_text` is a
  **required** argument on the runtime wrapper (no `None` default) so an omission fails loud rather than
  silently over-blocking.
- A **falsifiability test** asserts the runtime wrapper, given `read_text` covering chapters past the
  bookmark, is **rejected/raises** (not silently passed) — mirroring ADR 0004's "future Town is caught"
  on the runtime entrypoint.

Recap synthesis uses the anti-foreshadow `SYNTH_SYSTEM` + deterministic future-entity/prolepsis checks
+ the LLM-judge `references_future` hard-gate, fail-closed. The deterministic **NLI/span
event-grounding** gate (past-tense no-name future event) is a build follow-up.

### D-A10 — Reading position: high-water CFI canonical, the integer is its derived projection, the scrubber is clamped

Pass-1 + pass-2 fixed three position issues:

- **The persisted reading position is the *high-water* CFI** — the furthest-read position ever (not the
  reader's possibly-backward *current* position), in `catalog.reading_state.cfi`. The integer `bookmark`
  is its **derived projection** (`frontier.cfi_to_bookmark` against the current atom bounds), stamped
  with the **atom-set version** (segmentation/`schema_version`) that produced it. This resolves the
  pass-2 CFI-vs-monotonic contradiction (P2-10): because the stored CFI is the max-ever, re-deriving the
  integer is genuinely monotonic, and on a renumber it takes `max(re_derived, prior_high_water)` so it
  can never un-reveal below the prior high-water. Backward paging (current < high-water) is **LIT-17**
  and never lowers the persisted high-water.
- **B3 (renumber → stale-bookmark leak) is closed by a version check, not an immutability assumption**
  (pass-2 P2-9): the stored bookmark's `atom_set_version` is checked on **every read** and reads **fail
  closed on mismatch**; a renumber re-derives within the same migration (under the lock, before serving).
  In the MVP there is simply **no re-segmentation code path** — a re-import of an already-shelved book is
  rejected / yields a new `book_id`, never silently renumbers an existing store. A test forces an
  `atom_set_version` mismatch (stamp under v1, bump the version, assert reads fail closed) so this
  backstop is exercised in CI even though no renumber occurs in the MVP.
- **The scrubber bookmark is clamped server-side** to the **version-current re-derived high-water**:
  `effective_bookmark = min(requested, high_water)`, rejecting/clamping `requested > high_water`, never
  trusting the client (pass-1 HIGH; pass-2 P2-1 qualifier — the ceiling is the version-checked
  high-water, not a raw stored integer). A falsifiability test asserts `GET /graph?bookmark=99` returns
  the high-water view (or 4xx). The clamp applies to **every** route that takes a bookmark (views,
  search, and any future per-bookmark recap), not just `/graph`.

### D-A11 — API surface (sketch; detailed in the endpoints step)

`POST /api/books` (import), `GET /api/books`, `DELETE /api/books/{id}`, `GET/PUT
/api/books/{id}/position`, `GET /api/books/{id}/catch-me-up`, `GET /api/books/{id}/graph` (clamped
explicit `bookmark` for the LIT-15 scrubber), `GET /api/books/{id}/{timeline,notes,search}`, `GET
/api/books/{id}/ingest`, and a route serving the EPUB bytes for the LIT-13 reader. Every read route
reads through `with store.book(id) as view:` and passes the clamped `effective_bookmark` to both the
view and any gate/synthesis call.

### D-A12 — Dependencies & tooling

`uv`-managed venv. Runtime: `fastapi`, `uvicorn[standard]`, `pydantic`, `pydantic-settings`, `openai`
(SDK; `base_url`-configurable, native structured-output strict helper), `lxml` (hardened EPUB parse),
`python-multipart`, `sqlite-vec` (required since LIT-34). Gated: `instructor` (optional, deferred). Dev: `pytest`,
`ruff`.

## Invariants this architecture must preserve (carried from ADR 0001–0006, hardened by both passes)

1. **Single `_select` funnel** appends `book_id = ? AND revealed_at <= ? AND retracted_at IS NULL`
   (+ valid-time clause) on every fact read. (D-A2/D-A4)
2. **Per-connection authorizer** attached after migration, denying raw fact reads; made safe under
   concurrency by the per-book lock held for the whole operation (DAL path) + sole-owner (raw path);
   `_engaged` never True across IO. Sole connection owner. (D-A2/D-A3)
3. **Referential closure** preserved because the filter stays in `BookmarkView.search`, not the ranker.
   (D-A4)
4. **Reading position**: high-water CFI canonical; the integer is its monotonic derived projection,
   re-derived on any atom-set change with `max(re_derived, prior_high_water)`, **fail-closed** on
   version mismatch on every read; the scrubber bookmark is clamped server-side to the version-current
   high-water on every bookmark-taking route. (D-A10)
5. **Append-once / bitemporal**; supersession via `invalid_at` (atomic `replace_*`) or `retracted_at`;
   never destructive. Append-once kept as a pipeline early-return **and** DB-enforced by partial-UNIQUE
   indexes on the **derived** tables (`WHERE retracted_at IS NULL`). (D-A3/D-A6)
6. **Embed-pin**: per-book pinned `(embed_model, embed_dim, canary)`. The SAFETY property is enforced +
   tested in the DAL: once a book is pinned, `add_chunk` REJECTS a cross-space (wrong model/dim) vector,
   the late-pin guard fires on unstamped chunks, and KNN is same-space only (canary cosine `< 0.999` ⇒
   `FORCE_RE_EMBED`). The production "always pin BEFORE the first real chunk" guarantee is the **LIT-6
   ingestion pipeline's responsibility** (it pins via LIT-20 at first ingestion); the DAL deliberately
   retains a back-compat UNPINNED path for the offline stub / CI, so there is **no** hard Store-boundary
   rejection of an unpinned `add_chunk` (corrected from the earlier pass-2 wording, which over-claimed a
   Store guard + test that do not ship; memory-review MEDIUM). (D-A4/D-A5)
7. **Recap-cache key** = `(book_id, effective_bookmark, validity_snapshot, synth_model,
   recap_prompt_version, atom_set_version)` — keys on synth model + prompt version (ADR 0005) **and**
   the atom-set version (so a renumber forces a miss).
8. **Frontier preconditions**: 1:1 alignment, no zero-length atom, monotonic high-water — satisfiable
   because the divider-merge absorbs the divider span (D-A8); `assert_aligned` runs.
9. **Authorizer set is explicit + fail-closed**: code-owned `FACT_TABLES` **including `book_meta` and
   `event_participants`** (the two guarded tables with no `revealed_at`), asserted a superset of all
   `revealed_at`-bearing tables at open; unknown base tables fail the open or
   are explicitly infra-allow-listed, never left raw-readable. (D-A6)
10. **Spoiler gate is the merge gate**; runtime + eval share one implementation; the runtime gate takes
    the clamped `effective_bookmark` and a **required** `read_text` of chapters `<= effective_bookmark`.
    (D-A9)
11. **Extraction stays bookmark-agnostic**: `revealed_at` = chapter ordinal at ingest; roster from
    `view(ordinal-1)`; no future/whole-book context. (D-A3/D-A5)
12. **Catalog isolation**: `catalog.db` is the only shared writer; one owned connection under a global
    catalog lock, `busy_timeout` set, `ingest_progress` written as a monotonic `MAX`. (D-A2)

## Consequences

**Positive.** Spoiler-safety survives the move to a concurrent service by an explicit
sole-owner-+-whole-operation-lock model, with the runtime hazards named and closed. RAG ships correct
because the filter never leaves the DAL funnel; `vec0` is de-risked in isolation against explicit
criteria. Pure safety cores are lifted verbatim; the re-ported modules (`llm/client.py`, `vectors.py`,
the pipeline append-once) are named so their per-module reviews are scoped honestly. The position model
is renumber-safe **given the version check fires** (and is exercised by a forced-mismatch test even
though the MVP has no renumber); the scrubber cannot leak **given the high-water ceiling is the
version-current re-derived bookmark** (guaranteed in the MVP by the absence of a re-segmentation path).

**Negative / cost.** Per-book serialization caps same-book parallelism (acceptable: single reader per
book; cross-book stays parallel). Whole-view locking lengthens lock-held windows (sub-ms; no LLM under
the lock). Brute-force cosine is O(N)/query; sqlite-vec 0.1.9 also scans within its metadata filter,
so the production swap improves SQL integration but does not yet claim sublinear search. The
in-process ingestion worker is a single point of failure until LIT-7/22 harden it. `catalog.db` is the
one un-isolated shared writer (mitigated by a global lock + `busy_timeout` + monotonic high-water).
Generating strict schemas from Pydantic + a per-provider mode is more code than the spike's one path,
but honest about provider portability.

## Alternatives considered

- **Adopt `sqlite-vec vec0` now** — rejected in the 2026-06-28 decision pending proof; superseded by
  LIT-26's proof and LIT-34's production adoption on 2026-07-14.
- **Split the cosine ranker out of the funnel** — rejected as default; the filter stays in `search()`,
  the ranker only ranks pre-fetched rows (tested by drop-filter + no-DB-access probes).
- **Adopt Instructor / re-port the pipeline to Pydantic instances** — deferred; Pydantic for
  validation/schema, dict downstream, zero pipeline churn.
- **Derive `FACT_TABLES` from the live schema** — rejected (fails *open*, and drops `book_meta`);
  explicit set + fail-closed superset assertion instead.
- **`UNIQUE(book_id, chapter_key, content_hash)` on `chapters`** — rejected (redundant with the PK and
  guards nothing); append-once belongs on the derived tables + a pipeline early-return.
- **Current-CFI canonical** — rejected; high-water CFI canonical so the derived integer is genuinely
  monotonic and renumber-safe.
- **`BackgroundTasks` for ingestion / fully-async shared connection / Alembic / single global DB / Rust**
  — rejected as before.

## Routed / deferred (named, not silently dropped)

- **`vec0` range-prefilter recall + shadow-table authorizer behaviour** → dedicated vector spike;
  `BruteForce` is the runtime until green.
- **Deterministic NLI/span event-grounding gate** for synthesis → build follow-up (ADR 0004 HIGH #1).
- **Cheap-tier extraction recall** (gpt-4o-mini missed Zossima) → prompt/model tuning (LIT-9); fails safe.
- **Transactional/idempotent/resumable ingestion + bulk re-segmentation renumber + the CFI→bookmark
  re-derivation execution** → LIT-7; **per-book write serialization / WAL contention** → LIT-22.
- **catalog↔memory lifecycle** (atomic cross-file delete, backup-together, orphan scan) → LIT-24.
- **Malformed/adversarial EPUB / no-ToC heading split / non-Latin legacy front / `linear="no"`** →
  LIT-11 / LIT-23.

## Pass-1 review — findings incorporated (5 reviewers, all FIX_THEN_ACCEPT)

| # | Sev | Finding | Resolution |
|---|-----|---------|------------|
| B1 | BLOCKER | Worker doesn't take the read lock → `_engaged` bypass | D-A3: locked threadpool worker; `BackgroundTasks` rejected |
| B2 | BLOCKER | Lock scope undefined | D-A2: lock held for the whole `with store.book()` op |
| B3 | BLOCKER | Renumber → stale bookmark leak | D-A10: high-water CFI canonical, version-checked, fail-closed |
| H | HIGH | `check_same_thread=True` crashes threadpool | D-A2: `check_same_thread=False` + per-book lock as the primitive |
| H | HIGH | LRU evict-mid-op / double-open | D-A2: lease/refcount + lock-coupled eviction + guarded open |
| H | HIGH | Scrubber unbounded | D-A10: server-side clamp on every bookmark route |
| H | HIGH | Ingestion LLM under the lock | D-A3: LLM outside the lock; `_engaged` never across IO |
| H | HIGH | Instructor changes the dict contract ("unchanged" over-claim) | D-A5: Pydantic source → dict via `model_dump`; Instructor deferred |
| H | HIGH | Strict `json_schema` is OpenAI-only | D-A5: honest provider matrix + Pydantic-validate backstop |
| H | HIGH | BruteForce "proven" over-claim | D-A4: filter stays in funnel; testable acceptance criterion |
| M | MED | Migration second/unguarded connection | D-A2/D-A6: on the owned conn under the lock; authorizer after |
| M | MED | `add_chapter` 2-tx append-once | D-A3/D-A6: derived-table partial-UNIQUE + pipeline guard |
| M | MED | `part_group` renames `part_label` | D-A8: use `part_label` |
| M | MED | Divider-merge under-specified | D-A8: absorb divider start anchor; successor key; coverage taught |
| M | MED | "Derive FACT_TABLES from schema" fails open | D-A6: explicit set + fail-closed superset assertion |
| M | MED | cache_key misses a renumber | Inv 7: add `atom_set_version` |
| M | MED | New-book vs existing migration path | D-A6: create v1, stamp v1, walk to CURRENT |
| M | MED | `pydantic-settings` loads CWD `.env` | D-A7: absolute `__file__`-derived path + fail-loud |
| M | MED | `_audit_all` used by ingestion | D-A3: pipeline guard re-sourced off the hatch |
| L | LOW | trace scaffolding lifted verbatim | D-A1: excluded |
| L | LOW | unpinned same-space safety | Inv 6: pin-before-chunk obligation + test |
| L | LOW | "lock closes the bypass" over-credits the lock | D-A2: reworded (sole-owner closes the raw path) |
| L | LOW | synth_model cache-key not an invariant | Inv 7 restates it |
| L | LOW | "lifted verbatim" lumps cores + re-ports | D-A1: split (a)/(b) |
| L | LOW | vec0 authorizer/shadow-table | D-A4: vec0-spike acceptance criteria |
| — | — | "provably safe" wording | Downgraded throughout |

## Pass-2 review — findings incorporated (4 reviewers re-attacking the fixes, all FIX_THEN_ACCEPT)

Pass 2 confirmed sound: `parents[2]`→repo root, lock non-reentrancy, `check_same_thread=False` + serialized access, the one-create-path, and the divider-anchor availability. Genuine regressions the pass-1 fixes introduced, now fixed:

| # | Sev | Reg? | Finding | Resolution |
|---|-----|------|---------|------------|
| P2-1 | HIGH | yes | Append-once on the wrong table; removing the pipeline guard re-allows duplicate **derived** rows | D-A3/D-A6: keep a pipeline early-return (off `_audit_all`) + partial-UNIQUE on derived tables; not on `chapters`; double-ingest test |
| P2-2 | HIGH | yes | "FACT_TABLES := has revealed_at" drops `book_meta` → un-guarded | D-A6: explicit set **including `book_meta`**; "has revealed_at" only drives VALID_TIME + the assertion; raw-`book_meta`-denied test |
| P2-3 | HIGH | yes | `model_json_schema()` ≠ OpenAI strict (no `additionalProperties:false`, optionals dropped from `required`) | D-A5: OpenAI native strict `parse` helper / strict transform; `ConfigDict(extra="forbid")`; schema-shape test |
| P2-4 | MED | yes | default `model_dump()` returns enum members, breaking the string contract | D-A5: `str`-subclass enums + `model_dump(mode="json")`; parity test |
| P2-5 | HIGH | yes | Shared runtime gate `read_text` source unspecified → re-opens rev-2 fail-open | D-A9: pinned contract — `read_text=read_text_upto(effective_bookmark)`, required arg, reject-past-bookmark test |
| P2-6 | MED | yes | gate `bm` not bound to clamped `effective_bookmark` | D-A9/Inv 10: clamp computed once, threaded through view + gate; clamp on every bookmark route |
| P2-7 | HIGH | yes | "re-verified against the LIT-8 gate" — no code ships; wrong instrument | D-A5: reworded to a future parity test; gate covers only the dict contract |
| P2-8 | MED | yes | D-A4 acceptance criterion only half-testable | D-A4: two probes — drop-filter (conjunct 1) + no-DB-access/authorizer-deny (conjunct 2) |
| P2-9 | MED | yes | "segmentation immutable" asserted; B3 backstop dormant/untested | D-A10: B3 closed by the per-read version check (not immutability); forced-mismatch test; re-import yields a new `book_id` |
| P2-10 | MED | yes | CFI "canonical" vs "monotonic high-water" contradictory on rewind | D-A10: store the **high-water** CFI; derive integer from it; `max(re_derived, prior)` on renumber |
| P2-11 | MED | no | `catalog.db` concurrency unspecified | D-A2/Inv 12: global catalog lock, `busy_timeout`, monotonic `MAX` high-water |
| P2-12 | LOW | yes | search() split return/sort/limit/dim ownership unstated | D-A4: `rank()` owns cosine+dim-skip+sort+limit; `search()` owns filter+gate+4-tuple |
| P2-13 | LOW | yes | fail-closed-on-mismatch dormant in MVP | D-A10: named as a crash/partial-migration backstop + non-vacuity test |
| P2-14 | LOW | no | fail-loud predicate unspecified, conflicts with "warns" | D-A7: explicit `ALLOW_STUB`/default-deny; warn in test, hard-fail non-test |
| P2-15 | LOW | yes | "Store guarantees pin-before-chunk" unenforced | Inv 6: named obligation + unpinned-`add_chunk`-rejected test |
| P2-16 | LOW | yes | DECISIONS D21 records the superseded pre-pass-1 design | D21 reconciled (Instructor deferred, explicit fail-closed FACT_TABLES, `part_label`) before Accept |

## Review status

- [x] Adversarial Opus review — pass 1 (5 reviewers; FIX_THEN_ACCEPT; findings + resolutions above).
- [x] Incorporate pass-1 findings.
- [x] Adversarial Opus review — pass 2 (4 reviewers re-attacking the fixes; FIX_THEN_ACCEPT; genuine
  regressions found + resolved above; structural posture independently confirmed sound).
- [x] Incorporate pass-2 findings.
- [x] **Empirical verification (2026-06-28)** before any build code: the spike-code-grounded findings
  (P2-1/2-2/2-3/2-4) verified **firsthand** in `schema.sql`/`pipeline.py`/`extract_schema.py` (not via
  agent summaries); the external-library claims verified by **running** real `pydantic 2.13.4` /
  `openai 2.44.0` / `sqlite-vec 0.1.9` in the new `backend/.venv`: `ConfigDict(extra="forbid")` emits
  `additionalProperties:false`, a nullable-no-default field stays in `required`, a `str`-enum dumps to a
  string by default (a plain `Enum` does **not** — confirming the P2-4 hazard and the str-enum fix),
  and `chat.completions.parse`/`responses.parse`/`to_strict_json_schema` all exist. Two ADR
  wording-bugs found by the firsthand read and fixed here: the append-once skip is an **ingestion-side
  guarded existence read** (not a `BookmarkView` funnel read — the funnel can't see `content_hash`),
  and `FACT_TABLES` must also include **`event_participants`** (the second guarded table with no
  `revealed_at`).
- [x] Mark **Accepted** and finalize DECISIONS D21.
