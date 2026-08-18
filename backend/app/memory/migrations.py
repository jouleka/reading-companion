"""Forward-only schema migration runner for per-book memory.db (ADR 0007 D-A6).

One create/open path: a fresh book runs the v1 baseline (idempotent `IF NOT EXISTS` DDL), the caller
stamps `schema_version = BASELINE_VERSION`, then `migrate()` walks it through MIGRATIONS to
CURRENT_VERSION — so a brand-new book opened under CURRENT>1 gets every later migration too (not just
the baseline). An existing book is walked from its stored version. A stored version NEWER than the code
raises (fail-closed, no silent downgrade). No Alembic — too heavy for many small per-book files.
"""
import os

BASELINE_VERSION = 1           # the version the schema/memory.sql baseline DDL represents
CURRENT_VERSION = 6            # bump when adding a MIGRATIONS step
_HERE = os.path.dirname(os.path.abspath(__file__))

def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_v3(conn):
    """Add bookmark-effective identity correction support; shape-aware for repair/replay."""
    if "invalid_at" not in _columns(conn, "entities"):
        conn.execute("ALTER TABLE entities ADD COLUMN invalid_at INTEGER")
    if "revealed_at" not in _columns(conn, "event_participants"):
        conn.execute("ALTER TABLE event_participants ADD COLUMN revealed_at INTEGER")
    conn.execute(
        "UPDATE event_participants SET revealed_at=("
        "SELECT events.revealed_at FROM events WHERE events.event_id=event_participants.event_id "
        "AND events.book_id=event_participants.book_id) WHERE revealed_at IS NULL"
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS entity_corrections (
          correction_id          INTEGER PRIMARY KEY,
          book_id                 TEXT NOT NULL,
          kind                    TEXT NOT NULL,
          revealed_at             INTEGER NOT NULL,
          source_entity_ids_json  TEXT NOT NULL,
          target_entity_ids_json  TEXT NOT NULL,
          assignments_json        TEXT NOT NULL,
          reason                  TEXT,
          schema_version          INTEGER NOT NULL,
          recorded_at             TEXT NOT NULL,
          retracted_at            TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_entities_validity
          ON entities(book_id, revealed_at, invalid_at);
        CREATE INDEX IF NOT EXISTS ix_event_participants_reveal
          ON event_participants(book_id, event_id, revealed_at);
        CREATE INDEX IF NOT EXISTS ix_entity_corrections_reveal
          ON entity_corrections(book_id, revealed_at);
        CREATE TRIGGER IF NOT EXISTS ck_entities_valid_insert
          BEFORE INSERT ON entities
          WHEN NEW.invalid_at IS NOT NULL AND NEW.invalid_at <= NEW.revealed_at
          BEGIN SELECT RAISE(ABORT, 'entities invalid_at must be greater than revealed_at'); END;
        CREATE TRIGGER IF NOT EXISTS ck_entities_valid_update
          BEFORE UPDATE OF revealed_at,invalid_at ON entities
          WHEN NEW.invalid_at IS NOT NULL AND NEW.invalid_at <= NEW.revealed_at
          BEGIN SELECT RAISE(ABORT, 'entities invalid_at must be greater than revealed_at'); END;
        CREATE TRIGGER IF NOT EXISTS ck_event_participants_reveal_insert
          BEFORE INSERT ON event_participants
          WHEN NEW.revealed_at IS NULL
          BEGIN SELECT RAISE(ABORT, 'event participant revealed_at is required'); END;
        CREATE TRIGGER IF NOT EXISTS ck_event_participants_reveal_update
          BEFORE UPDATE OF revealed_at ON event_participants
          WHEN NEW.revealed_at IS NULL
          BEGIN SELECT RAISE(ABORT, 'event participant revealed_at is required'); END;
        """
    )


def _migrate_v4(conn):
    """Add advisory book-profile metadata; shape-aware for interrupted repair/replay."""
    additions = {
        "book_type": "TEXT NOT NULL DEFAULT 'novel'",
        "book_type_confidence": "REAL NOT NULL DEFAULT 0.0",
        "book_type_detector_version": "TEXT NOT NULL DEFAULT 'legacy-novel-v1'",
        "book_type_signals": "TEXT NOT NULL DEFAULT '[\"migrated_existing_store\"]'",
    }
    existing = _columns(conn, "book_meta")
    for column, definition in additions.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE book_meta ADD COLUMN {column} {definition}")


def _migrate_v5(conn):
    """Persist normalized EPUB content language; existing stores are explicitly undetermined."""
    if "content_language" not in _columns(conn, "book_meta"):
        conn.execute("ALTER TABLE book_meta ADD COLUMN content_language TEXT NOT NULL DEFAULT 'und'")


def _migrate_v6(conn):
    """Add durable metadata for the derived per-book vec0 index; the vtable is built lazily."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vector_index_meta (
          index_name            TEXT PRIMARY KEY CHECK (index_name = 'chunks_vec'),
          backend               TEXT NOT NULL CHECK (backend = 'sqlite-vec'),
          extension_version     TEXT NOT NULL,
          index_schema_version  INTEGER NOT NULL,
          dimensions            INTEGER NOT NULL CHECK (dimensions > 0)
        )
        """
    )


# version v -> SQL string or callable(conn) migrating a book from v-1 to v.
MIGRATIONS: dict[int, object] = {
    2: """
        CREATE TABLE IF NOT EXISTS ingested_chapters (
          chapter_key       TEXT PRIMARY KEY REFERENCES chapters(chapter_key),
          book_id           TEXT NOT NULL,
          content_hash      TEXT NOT NULL,
          cost_pending      INTEGER NOT NULL DEFAULT 0,
          extractor_model   TEXT,
          input_tokens      INTEGER,
          output_tokens     INTEGER,
          usd               REAL,
          completed_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_ingested_chapters_book
          ON ingested_chapters(book_id, chapter_key, content_hash);
    """,
    3: _migrate_v3,
    4: _migrate_v4,
    5: _migrate_v5,
    6: _migrate_v6,
}


def baseline_sql() -> str:
    with open(os.path.join(_HERE, "schema", "memory.sql"), encoding="utf-8") as f:
        return f.read()


def ensure_baseline(conn) -> None:
    """Create the v1 baseline schema (idempotent `IF NOT EXISTS`). Run before the authorizer is
    attached (DDL must not be blocked) and inside a write transaction. Does NOT insert book_meta."""
    conn.executescript(baseline_sql())


def migrate(conn) -> None:
    """Walk the book from its stored schema_version up to CURRENT_VERSION, applying each MIGRATIONS
    step and stamping the new version. Returns immediately if book_meta has no row yet (the caller must
    stamp BASELINE_VERSION first, then call migrate). Fail-closed on a version newer than the code."""
    row = conn.execute("SELECT schema_version FROM book_meta LIMIT 1").fetchone()
    if row is None:
        return
    stored = row[0]
    if stored > CURRENT_VERSION:
        raise RuntimeError(
            f"book schema_version {stored} is newer than this code ({CURRENT_VERSION}) — refusing "
            f"to open (no silent downgrade)")
    for v in range(stored + 1, CURRENT_VERSION + 1):
        step = MIGRATIONS[v]
        if callable(step):
            step(conn)
        else:
            conn.executescript(step)
        conn.execute("UPDATE book_meta SET schema_version = ?", (v,))
