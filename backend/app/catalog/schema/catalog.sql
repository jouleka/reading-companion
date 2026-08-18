-- LIT-18 — global catalog database (catalog.db). Lifted near-verbatim from the spike
-- (spikes/lit-5-schema/catalog.sql) per ADR 0007 D-A1. All DDL is IF NOT EXISTS (idempotent on open).
-- =============================================================================
-- ONE small global DB for the whole app: the shelf, the MUTABLE reading state (bookmark/CFI/ingest),
-- and the cost ledger. The per-book memory.db files are IMMUTABLE ground truth and deliberately do NOT
-- store "the current bookmark" — the bookmark is reading state, passed INTO the DAL as an argument.
-- This separation makes time-travel trivial (pass a different integer) and keeps memory.db append-only.
-- =============================================================================

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS books (
  book_id           TEXT PRIMARY KEY,     -- same id stamped inside that book's memory.db
  title             TEXT NOT NULL,
  author            TEXT,
  source            TEXT,                 -- gutenberg | standard-ebooks | upload
  source_id         TEXT,
  file_hash         TEXT,                 -- sha256 of the imported .epub (dedupe / re-import detect)
  cover_path        TEXT,
  db_path           TEXT NOT NULL,        -- e.g. books/<book_id>/memory.db
  schema_version    INTEGER NOT NULL,
  incarnation       TEXT NOT NULL,        -- unique shelf lifetime; changes on delete/re-import
  added_at          TEXT NOT NULL,
  last_opened_at    TEXT                  -- reserved for the LIT route layer (no setter yet)
);

-- Per-book reading position. bookmark = the spoiler frontier the DAL consumes — the highest FULLY-READ
-- chapter ordinal (a MONOTONIC high-water mark; LIT-12 maps the continuous reader CFI onto it).
CREATE TABLE IF NOT EXISTS reading_state (
  book_id           TEXT PRIMARY KEY REFERENCES books(book_id),
  bookmark          INTEGER NOT NULL DEFAULT 0,
  cfi               TEXT,                 -- precise reader position (LIT-12/LIT-13)
  ingest_progress   INTEGER NOT NULL DEFAULT 0,  -- highest chapter ordinal ingested (monotonic)
  position_epoch    INTEGER NOT NULL DEFAULT 0,  -- LIT-17: increments on an explicit new reading pass
  updated_at        TEXT
);

-- Per-book spend (cost-to-date; feeds LIT-21 ceilings).
CREATE TABLE IF NOT EXISTS cost_ledger (
  entry_id          INTEGER PRIMARY KEY,
  book_id           TEXT NOT NULL REFERENCES books(book_id),
  chapter_ordinal   INTEGER,
  phase             TEXT,                 -- extraction | synthesis | embedding
  model             TEXT,
  input_tokens      INTEGER,
  output_tokens     INTEGER,
  usd               REAL,
  at                TEXT
);

CREATE INDEX IF NOT EXISTS ix_cost_book ON cost_ledger(book_id);

-- Pre-call reservations make ceilings race-safe. A process crash intentionally leaves the
-- reservation behind: unknown provider spend must continue to count until an operator reconciles it.
CREATE TABLE IF NOT EXISTS cost_reservations (
  reservation_id       TEXT PRIMARY KEY,
  book_id               TEXT NOT NULL REFERENCES books(book_id),
  chapter_ordinal       INTEGER,
  phase                 TEXT NOT NULL,
  model                 TEXT,
  reserved_input_tokens INTEGER NOT NULL,
  reserved_output_tokens INTEGER NOT NULL,
  reserved_usd          REAL NOT NULL,
  actual_input_tokens   INTEGER,
  actual_output_tokens  INTEGER,
  actual_usd            REAL,
  at                    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_cost_reservation_book ON cost_reservations(book_id);
