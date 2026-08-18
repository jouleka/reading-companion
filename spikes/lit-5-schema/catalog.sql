-- LIT-18 — global catalog database (catalog.db)
-- =============================================================================
-- ONE small global DB for the whole app. Holds the shelf, the MUTABLE reading
-- state (bookmark, CFI), and the cost ledger. The per-book memory.db files are
-- IMMUTABLE ground truth (facts stamped with revealed_at) and deliberately do
-- NOT store "the current bookmark" — the bookmark is reading state, passed INTO
-- the DAL as an argument. This separation is what makes time-travel trivial
-- (pass a different integer) and keeps memory.db append-only.
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
  added_at          TEXT NOT NULL,
  last_opened_at    TEXT
);

-- Per-book reading position. bookmark = the spoiler frontier the DAL consumes.
-- It is the highest FULLY-READ chapter ordinal; LIT-12 maps the continuous reader
-- CFI onto this integer (and may later extend the DAL to a sub-chapter frontier).
CREATE TABLE IF NOT EXISTS reading_state (
  book_id           TEXT PRIMARY KEY REFERENCES books(book_id),
  bookmark          INTEGER NOT NULL DEFAULT 0,
  cfi               TEXT,                 -- precise reader position (LIT-12)
  ingest_progress   INTEGER NOT NULL DEFAULT 0,  -- highest chapter ordinal ingested
  updated_at        TEXT
);

-- Per-book spend (LIT-18 "cost-to-date" + feeds LIT-21 ceilings).
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
