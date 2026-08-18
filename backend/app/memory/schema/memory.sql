-- LIT-5 / LIT-18 / LIT-19 / LIT-20 — per-book memory database (memory.db) — v1 baseline.
-- Lifted near-verbatim from the twice-reviewed spike (spikes/lit-5-schema/schema.sql) per ADR 0007
-- D-A1; the only production addition is the append-once partial-UNIQUE index on chapter_summaries
-- (ADR 0007 D-A3/D-A6 — see the note at the bottom). All DDL is `IF NOT EXISTS` so the forward-only
-- migration runner (migrations.py) can apply this baseline idempotently on every open.
-- =============================================================================
-- BITEMPORAL ON TWO INDEPENDENT AXES:
--   valid-time   (STORY time, chapter ordinals): revealed_at / invalid_at  -> the spoiler filter + time-travel
--   transaction-time (INGESTION time): schema_version/extractor_version/recorded_at/retracted_at -> re-extraction/audit
-- THE SPOILER-SAFE READ (applied in ONE place, the DAL `_select` funnel) is:
--   book_id = :book AND revealed_at <= :bookmark
--   AND (invalid_at IS NULL OR invalid_at > :bookmark)   -- only on valid-time tables
--   AND retracted_at IS NULL                              -- current transaction-time view
-- REFERENTIAL CLOSURE: entity-referencing reads semijoin the visible-entity set; chapter-keyed reads
-- semijoin the live-chapter set. Later migrations add identity validity and participant-link reveal
-- stamps for bookmark-effective LIT-10 corrections; this file remains the v1 baseline by design.
-- =============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;        -- per-book writer + concurrent readers (LIT-22)

CREATE TABLE IF NOT EXISTS book_meta (
  book_id           TEXT PRIMARY KEY,
  title             TEXT NOT NULL,
  author            TEXT,
  source            TEXT,                 -- gutenberg | standard-ebooks | upload
  source_id         TEXT,
  file_hash         TEXT,                 -- sha256 of the imported .epub
  schema_version    INTEGER NOT NULL,
  created_at        TEXT NOT NULL,
  -- LIT-20: model identity PINNED at first ingestion (forces explicit re-extract/re-embed on change).
  extractor_model   TEXT,
  synth_model       TEXT,
  embed_model       TEXT,                 -- FULL embed identity (provider@base_url:model), not a bare name
  embed_dim         INTEGER,
  embed_canary      TEXT                  -- fingerprint of the embedder's output; catches a same-NAME space change
);

CREATE TABLE IF NOT EXISTS chapters (
  chapter_key       TEXT PRIMARY KEY,
  book_id           TEXT NOT NULL,
  revealed_at       INTEGER NOT NULL,
  href              TEXT NOT NULL,
  fragment          TEXT,
  title             TEXT,
  part_label        TEXT,                 -- grouping (e.g. "Part I"); not its own atom (ADR 0001 divider-merge)
  kind              TEXT NOT NULL DEFAULT 'body',
  content_hash      TEXT NOT NULL,        -- delta-skip: re-ingest of unchanged chapter is a no-op
  schema_version    INTEGER NOT NULL,
  extractor_version TEXT,
  recorded_at       TEXT NOT NULL,
  retracted_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_chapters_ordinal
  ON chapters(book_id, revealed_at) WHERE retracted_at IS NULL;

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
-- ADR 0007 D-A3/D-A6 append-once backstop: at most ONE live summary per (book, chapter, kind).
-- Compatible with reextract_summary (retract-then-insert) and the per-chapter chapter+rolling-recap pair.
CREATE UNIQUE INDEX IF NOT EXISTS ux_summaries_live
  ON chapter_summaries(book_id, chapter_key, kind) WHERE retracted_at IS NULL;

CREATE TABLE IF NOT EXISTS entities (
  entity_id         INTEGER PRIMARY KEY,
  book_id           TEXT NOT NULL,
  canonical_name    TEXT NOT NULL,
  type              TEXT NOT NULL,        -- character | place | faction | object
  revealed_at       INTEGER NOT NULL,
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
  CHECK (invalid_at IS NULL OR invalid_at > revealed_at)
);

CREATE TABLE IF NOT EXISTS events (
  event_id          INTEGER PRIMARY KEY,
  book_id           TEXT NOT NULL,
  revealed_at       INTEGER NOT NULL,
  order_idx         INTEGER NOT NULL,     -- ordering within a chapter
  summary           TEXT NOT NULL,
  kind              TEXT,
  invalid_at        INTEGER,
  schema_version    INTEGER NOT NULL,
  extractor_version TEXT,
  recorded_at       TEXT NOT NULL,
  retracted_at      TEXT,
  CHECK (invalid_at IS NULL OR invalid_at > revealed_at)
);

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

-- Canonical RAG chunks + portable embeddings. Production derives a guarded sqlite-vec virtual index
-- from these JSON vectors; this table remains the exact reference and recovery source of truth.
-- NOTE: append-once for chunks is the ingestion pipeline's early-return
-- (a chapter may legitimately have many chunks, so there is no per-chapter UNIQUE here).
CREATE TABLE IF NOT EXISTS chunks (
  chunk_id          INTEGER PRIMARY KEY,
  book_id           TEXT NOT NULL,
  chapter_key       TEXT NOT NULL REFERENCES chapters(chapter_key),
  revealed_at       INTEGER NOT NULL,
  text              TEXT NOT NULL,
  vec               TEXT NOT NULL,        -- canonical JSON float[]; vec0 is a derived local index
  embed_model       TEXT,                 -- LIT-20: embedding-model identity stamped on every vector
  embed_dim         INTEGER,
  retracted_at      TEXT
);

CREATE INDEX IF NOT EXISTS ix_entities_rev      ON entities(book_id, revealed_at);
CREATE INDEX IF NOT EXISTS ix_edges_rev         ON edges(book_id, revealed_at, invalid_at);
CREATE INDEX IF NOT EXISTS ix_events_rev        ON events(book_id, revealed_at, invalid_at);
CREATE INDEX IF NOT EXISTS ix_estate_rev        ON entity_state(book_id, entity_id, revealed_at, invalid_at);
CREATE INDEX IF NOT EXISTS ix_chunks_rev        ON chunks(book_id, revealed_at);
CREATE INDEX IF NOT EXISTS ix_summaries_rev     ON chapter_summaries(book_id, revealed_at);
