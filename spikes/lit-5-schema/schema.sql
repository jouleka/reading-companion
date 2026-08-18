-- LIT-5 / LIT-18 / LIT-19 — per-book memory database (memory.db)
-- =============================================================================
-- ONE FILE PER BOOK (LIT-18). Physical isolation is the primary guarantee:
-- a KNN / SELECT in this file CANNOT return another book's rows, because they
-- live in a different database file. `book_id` is *also* carried on every fact
-- table as a reserved logical hook (LIT-5 exit criterion) so the exact same DAL
-- works unchanged if these files are ever collapsed into one multi-book DB
-- (hosted/multi-tenant future) or attached together.
--
-- BITEMPORAL ON TWO INDEPENDENT AXES:
--   * valid-time  (STORY time, measured in chapter ordinals):
--        revealed_at  = the chapter that first made this fact true
--        invalid_at   = the chapter at which the plot superseded it (NULL = still holds)
--     -> THIS pair is the spoiler filter and powers the time-travel scrubber.
--   * transaction-time (INGESTION time, when *we* wrote/changed the row):
--        schema_version, extractor_version = which schema/prompt/model produced it
--        recorded_at   = wall-clock the row was written
--        retracted_at  = when a re-extraction/correction superseded it (NULL = current)
--     -> THIS set powers LIT-19 re-extraction, migration, audit & rollback.
--
-- THE SPOILER-SAFE READ (applied in ONE place, the DAL `_select` funnel) is:
--     book_id = :book
--     AND revealed_at <= :bookmark
--     AND (invalid_at IS NULL OR invalid_at > :bookmark)   -- only on valid-time tables
--     AND retracted_at IS NULL                              -- current transaction-time view
--
-- INVARIANT: every fact-bearing table has a `revealed_at` column (uniformly named,
-- even where it means "first revealed") so the single filter clause is universal.
--
-- REFERENTIAL CLOSURE (added after adversarial review): the per-row filter is NOT
-- enough on its own — a row with revealed_at <= bookmark may REFERENCE an entity whose
-- own revealed_at is in the future. The DAL therefore semijoins every entity-referencing
-- read against the set of currently-visible entities, so the spoiler frontier is
-- referentially closed (no read can surface an unmet entity). event_participants is a
-- PURE link (no temporal stamps) so there is exactly one source of truth: event
-- visibility comes from `events`, entity visibility from `entities`.
-- =============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;        -- per-book writer + concurrent readers (LIT-22)

-- In-file identity. One row. `book_id` here is the source of truth for this file.
CREATE TABLE IF NOT EXISTS book_meta (
  book_id           TEXT PRIMARY KEY,
  title             TEXT NOT NULL,
  author            TEXT,
  source            TEXT,                 -- gutenberg | standard-ebooks | upload
  source_id         TEXT,
  file_hash         TEXT,                 -- sha256 of the imported .epub
  schema_version    INTEGER NOT NULL,
  created_at        TEXT NOT NULL,
  -- LIT-20: the model identity PINNED at first ingestion. A change to the extractor or embedding
  -- model is NOT silently mixed — it forces an explicit, costed re-extract / re-embed migration
  -- (synth/large model may change freely; see the safe-swap matrix in ADR 0005).
  extractor_model   TEXT,
  synth_model       TEXT,
  embed_model       TEXT,                 -- FULL embed identity (provider@base_url:model), not a bare name
  embed_dim         INTEGER,
  embed_canary      TEXT                  -- fingerprint of the embedder's output; catches a same-NAME space change
);

-- The chapter atom (LIT-4). chapter_key = {book_id}:{href}[#fragment] (content-identity,
-- never positional). revealed_at = 1-based ordinal among INCLUDED chapters (derived;
-- may renumber on re-segmentation, the key does not). No invalid_at: a chapter, once
-- read, is not story-superseded. Re-segmentation uses transaction-time (retracted_at).
CREATE TABLE IF NOT EXISTS chapters (
  chapter_key       TEXT PRIMARY KEY,
  book_id           TEXT NOT NULL,
  revealed_at       INTEGER NOT NULL,
  href              TEXT NOT NULL,
  fragment          TEXT,
  title             TEXT,
  part_label        TEXT,                 -- grouping (e.g. "Part I"); not its own atom
  kind              TEXT NOT NULL DEFAULT 'body',
  content_hash      TEXT NOT NULL,        -- delta-skip: re-ingest of unchanged chapter is a no-op
  schema_version    INTEGER NOT NULL,
  extractor_version TEXT,
  recorded_at       TEXT NOT NULL,
  retracted_at      TEXT
);
-- one live ordinal per book (retracted rows excused)
CREATE UNIQUE INDEX IF NOT EXISTS ux_chapters_ordinal
  ON chapters(book_id, revealed_at) WHERE retracted_at IS NULL;

-- LIT-19 retention: bookmark-bounded raw chapter text as ground truth (RAG quotes +
-- cheap re-extraction). LOCAL-ONLY, gated by the local-first privacy posture; any
-- hosted/synced retention requires a separate explicit policy (see ADR + LIT-24).
-- Carries the transaction-time stamps so a re-segmentation can retract stale text and
-- so it is read through the SAME spoiler funnel as everything else (not the audit hatch).
CREATE TABLE IF NOT EXISTS raw_chapters (
  chapter_key       TEXT PRIMARY KEY REFERENCES chapters(chapter_key),
  book_id           TEXT NOT NULL,
  revealed_at       INTEGER NOT NULL,
  text              TEXT NOT NULL,
  char_count        INTEGER NOT NULL,
  content_hash      TEXT NOT NULL,
  recorded_at       TEXT NOT NULL,
  retracted_at      TEXT
);

CREATE TABLE IF NOT EXISTS chapter_summaries (
  summary_id        INTEGER PRIMARY KEY,
  chapter_key       TEXT NOT NULL REFERENCES chapters(chapter_key),
  book_id           TEXT NOT NULL,
  revealed_at       INTEGER NOT NULL,
  kind              TEXT NOT NULL DEFAULT 'chapter',  -- chapter | rolling-recap
  summary           TEXT NOT NULL,
  schema_version    INTEGER NOT NULL,
  extractor_version TEXT,
  recorded_at       TEXT NOT NULL,
  retracted_at      TEXT
);

-- Anything with a name + aliases needing coreference resolution: characters, places,
-- factions, notable objects. (Themes are different — separate table, no alias resolution.)
-- No invalid_at: an entity existing stays true once revealed; death/exit is entity_state.
CREATE TABLE IF NOT EXISTS entities (
  entity_id         INTEGER PRIMARY KEY,
  book_id           TEXT NOT NULL,
  canonical_name    TEXT NOT NULL,
  type              TEXT NOT NULL,        -- character | place | faction | object
  revealed_at       INTEGER NOT NULL,     -- first-revealed chapter (uniform column name)
  schema_version    INTEGER NOT NULL,
  extractor_version TEXT,
  recorded_at       TEXT NOT NULL,
  retracted_at      TEXT
);

CREATE TABLE IF NOT EXISTS aliases (
  alias_id          INTEGER PRIMARY KEY,
  entity_id         INTEGER NOT NULL REFERENCES entities(entity_id),
  book_id           TEXT NOT NULL,
  surface_form      TEXT NOT NULL,
  revealed_at       INTEGER NOT NULL,
  recorded_at       TEXT NOT NULL,
  retracted_at      TEXT
);

-- Character-graph relationships. Valid-time: a relationship genuinely changes with plot
-- (engaged -> estranged) -> the OLD row gets invalid_at, a NEW row is inserted. Never updated in place.
CREATE TABLE IF NOT EXISTS edges (
  edge_id           INTEGER PRIMARY KEY,
  book_id           TEXT NOT NULL,
  src_entity        INTEGER NOT NULL REFERENCES entities(entity_id),
  dst_entity        INTEGER NOT NULL REFERENCES entities(entity_id),
  rel_type          TEXT NOT NULL,        -- family | love | rivalry | allegiance | ...
  label             TEXT,
  revealed_at       INTEGER NOT NULL,
  invalid_at        INTEGER,              -- valid-time end (plot supersedes); NULL = still holds
  schema_version    INTEGER NOT NULL,
  extractor_version TEXT,
  recorded_at       TEXT NOT NULL,
  retracted_at      TEXT,
  CHECK (invalid_at IS NULL OR invalid_at > revealed_at)   -- reject inverted/zero-width windows
);

-- Timeline beats.
CREATE TABLE IF NOT EXISTS events (
  event_id          INTEGER PRIMARY KEY,
  book_id           TEXT NOT NULL,
  revealed_at       INTEGER NOT NULL,
  order_idx         INTEGER NOT NULL,     -- ordering within a chapter
  summary           TEXT NOT NULL,
  kind              TEXT,
  invalid_at        INTEGER,              -- e.g. a reported event later revealed to be false
  schema_version    INTEGER NOT NULL,
  extractor_version TEXT,
  recorded_at       TEXT NOT NULL,
  retracted_at      TEXT,
  CHECK (invalid_at IS NULL OR invalid_at > revealed_at)
);

-- Event<->entity links for swimlanes (whose lane an event sits in). PURE LINK, no
-- temporal stamps: event visibility comes from `events`, entity visibility from
-- `entities` (single source of truth — see REFERENTIAL CLOSURE note in the header).
CREATE TABLE IF NOT EXISTS event_participants (
  event_id          INTEGER NOT NULL REFERENCES events(event_id),
  entity_id         INTEGER NOT NULL REFERENCES entities(entity_id),
  book_id           TEXT NOT NULL,
  role              TEXT,
  PRIMARY KEY (event_id, entity_id)
);

CREATE TABLE IF NOT EXISTS themes (
  theme_id          INTEGER PRIMARY KEY,
  book_id           TEXT NOT NULL,
  name              TEXT NOT NULL,
  description       TEXT,
  revealed_at       INTEGER NOT NULL,
  invalid_at        INTEGER,
  schema_version    INTEGER NOT NULL,
  extractor_version TEXT,
  recorded_at       TEXT NOT NULL,
  retracted_at      TEXT,
  CHECK (invalid_at IS NULL OR invalid_at > revealed_at)
);

-- SCORE-style explicit per-entity evolving state (location/condition/allegiance at chapter N).
-- Powers "where things stand" + spoiler-safe bio. A new state supersedes the prior (invalid_at).
CREATE TABLE IF NOT EXISTS entity_state (
  state_id          INTEGER PRIMARY KEY,
  entity_id         INTEGER NOT NULL REFERENCES entities(entity_id),
  book_id           TEXT NOT NULL,
  revealed_at       INTEGER NOT NULL,
  invalid_at        INTEGER,
  status_json       TEXT NOT NULL,
  schema_version    INTEGER NOT NULL,
  extractor_version TEXT,
  recorded_at       TEXT NOT NULL,
  retracted_at      TEXT,
  CHECK (invalid_at IS NULL OR invalid_at > revealed_at)
);

-- RAG chunks + embeddings. In the real build this is a sqlite-vec `vec0` virtual table.
-- book_id partitions cleanly (per-file isolation gives it for free). The revealed_at
-- spoiler frontier is a RANGE over a high-cardinality integer; whether vec0 applies it
-- as a partition key vs an auxiliary metadata range filter — and the RECALL impact of
-- filtering pre- vs post-ANN — is NOT yet proven and is routed to a dedicated vector
-- spike (see ADR 0002 "open follow-ups"). The prototype stores the vector as JSON and
-- cosines over the DAL-filtered candidate set: this proves the KNN path inherits the
-- exact same spoiler + book filter, but NOT vec0's pre-filter recall behaviour.
CREATE TABLE IF NOT EXISTS chunks (
  chunk_id          INTEGER PRIMARY KEY,
  book_id           TEXT NOT NULL,
  chapter_key       TEXT NOT NULL REFERENCES chapters(chapter_key),
  revealed_at       INTEGER NOT NULL,
  text              TEXT NOT NULL,
  vec               TEXT NOT NULL,        -- JSON float[]  (stand-in for vec0 in the prototype)
  embed_model       TEXT,                 -- LIT-20: embedding-model identity stamped on every vector;
  embed_dim         INTEGER,              -- cosine across two embed spaces is meaningless -> KNN must
  retracted_at      TEXT                  -- only compare same (embed_model,embed_dim); a change forces re-embed.
);

-- Filter-supporting indexes (every read is book_id + revealed_at bounded).
CREATE INDEX IF NOT EXISTS ix_entities_rev      ON entities(book_id, revealed_at);
CREATE INDEX IF NOT EXISTS ix_edges_rev         ON edges(book_id, revealed_at, invalid_at);
CREATE INDEX IF NOT EXISTS ix_events_rev        ON events(book_id, revealed_at, invalid_at);
CREATE INDEX IF NOT EXISTS ix_estate_rev        ON entity_state(book_id, entity_id, revealed_at, invalid_at);
CREATE INDEX IF NOT EXISTS ix_chunks_rev        ON chunks(book_id, revealed_at);
CREATE INDEX IF NOT EXISTS ix_summaries_rev     ON chapter_summaries(book_id, revealed_at);
