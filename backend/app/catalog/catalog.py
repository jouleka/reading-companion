"""The global catalog.db service (ADR 0002 D14, ADR 0007 D-A2/Inv 12).

The catalog is the ONE shared writer that is not per-book isolated, so it is owned by a single
`check_same_thread=False` connection serialized by a global lock, in WAL with `busy_timeout`. The
integer `bookmark` (and `ingest_progress`) is a MONOTONIC high-water mark made atomic IN SQL
(`UPDATE ... SET bookmark = MAX(bookmark, ?)`), so a backward update (re-reading — LIT-17) never lowers
the spoiler frontier EVEN if a second Catalog instance/process touches the file (the in-process lock
alone would lose that race — catalog-review HIGH). The bookmark is int-and-range-guarded (defense in
depth with the DAL) so a malformed/negative value can never be persisted and later fed to `view()`.

cfi semantics: the integer bookmark is THIS layer's canonical monotonic authority; `cfi` is stored as
the LATEST reported reader position (resume UX) and may move backward on a re-read — it is NOT the ADR
D-A10 "high-water CFI". The future LIT-12/13 frontier layer that derives an integer from a CFI MUST take
`max(cfi_to_bookmark(cfi), stored_bookmark)` (never lower the stored high-water) — exactly D-A10's rule.
"""
import math
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _baseline_sql():
    with open(os.path.join(_HERE, "schema", "catalog.sql"), encoding="utf-8") as f:
        return f.read()


class Catalog:
    def __init__(self, db_path, schema_version_default=1):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._schema_version_default = schema_version_default
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA journal_mode = WAL")   # cross-connection durability for the one shared file
        self._conn.execute("PRAGMA foreign_keys = ON")
        with self._lock:
            self._conn.executescript(_baseline_sql())
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(books)")}
            if "incarnation" not in columns:
                self._conn.execute("ALTER TABLE books ADD COLUMN incarnation TEXT")
            for row in self._conn.execute(
                    "SELECT book_id FROM books WHERE incarnation IS NULL OR incarnation=''"
            ).fetchall():
                self._conn.execute(
                    "UPDATE books SET incarnation=? WHERE book_id=?", (uuid.uuid4().hex, row["book_id"])
                )
            state_columns = {r[1] for r in self._conn.execute("PRAGMA table_info(reading_state)")}
            if "position_epoch" not in state_columns:
                self._conn.execute(
                    "ALTER TABLE reading_state ADD COLUMN position_epoch INTEGER NOT NULL DEFAULT 0"
                )
            try:
                duplicates = self._conn.execute(
                    "SELECT book_id,chapter_ordinal FROM cost_ledger "
                    "WHERE phase='extraction' AND chapter_ordinal IS NOT NULL "
                    "GROUP BY book_id,chapter_ordinal HAVING COUNT(*) > 1"
                ).fetchall()
                for duplicate in duplicates:
                    rows = self._conn.execute(
                        "SELECT entry_id,model,input_tokens,output_tokens,usd FROM cost_ledger "
                        "WHERE book_id=? AND chapter_ordinal=? AND phase='extraction' ORDER BY entry_id",
                        (duplicate["book_id"], duplicate["chapter_ordinal"]),
                    ).fetchall()
                    receipts = {
                        (row["model"], row["input_tokens"], row["output_tokens"], row["usd"])
                        for row in rows
                    }
                    if len(receipts) != 1:
                        raise RuntimeError(
                            "irreconcilable duplicate extraction costs for "
                            f"{duplicate['book_id']!r} chapter {duplicate['chapter_ordinal']}"
                        )
                    self._conn.execute(
                        "DELETE FROM cost_ledger WHERE book_id=? AND chapter_ordinal=? "
                        "AND phase='extraction' AND entry_id<>?",
                        (duplicate["book_id"], duplicate["chapter_ordinal"], rows[0]["entry_id"]),
                    )
                self._conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_cost_extraction_book_chapter "
                    "ON cost_ledger(book_id,chapter_ordinal) "
                    "WHERE phase='extraction' AND chapter_ordinal IS NOT NULL"
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    @staticmethod
    def _check_int(value, what="bookmark"):
        # int-and-range guard (defense in depth): a non-int/bool/negative/oversized value must never be
        # persisted — it would later be fed to MemoryDB.view() (a leak) or fail deep in the sqlite bind.
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{what} must be an int ordinal, got {value!r}")
        if value < 0 or value >= 2 ** 63:
            raise ValueError(f"{what} must be in [0, 2**63), got {value!r}")

    # ---- shelf ------------------------------------------------------------
    def add_book(self, book_id, *, title, author=None, source=None, source_id=None,
                 file_hash=None, cover_path=None, db_path=None, schema_version=None):
        """Add a book to the shelf + create its reading_state row (bookmark 0). Raises if the book_id
        already exists — a re-import yields a NEW book_id, never a duplicate (ADR D-A10)."""
        db_path = db_path or f"books/{book_id}/memory.db"
        schema_version = self._schema_version_default if schema_version is None else schema_version
        with self._lock:
            try:
                # Both INSERTs are ONE atomic unit: a failure on EITHER rolls back so we never leave an
                # orphaned shelved book with no reading_state (catalog-review HIGH).
                self._conn.execute(
                    "INSERT INTO books(book_id,title,author,source,source_id,file_hash,cover_path,"
                    "db_path,schema_version,incarnation,added_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (book_id, title, author, source, source_id, file_hash, cover_path, db_path,
                     schema_version, uuid.uuid4().hex, _now()))
                self._conn.execute("INSERT INTO reading_state(book_id) VALUES (?)", (book_id,))
                self._conn.commit()
            except sqlite3.IntegrityError as e:
                self._conn.rollback()
                raise ValueError(f"book {book_id!r} already in the catalog") from e
            except Exception:
                self._conn.rollback()
                raise

    def get_book(self, book_id):
        with self._lock:
            r = self._conn.execute("SELECT * FROM books WHERE book_id=?", (book_id,)).fetchone()
        return dict(r) if r else None

    def set_schema_version(self, book_id, schema_version):
        """Mirror a successfully opened/migrated per-book store into the recovery catalog metadata.

        A zero-row update is expected during import because memory.db is created before the shelf row.
        """
        self._check_int(schema_version, "schema_version")
        if schema_version == 0:
            raise ValueError("schema_version must be positive")
        with self._lock:
            changed = self._conn.execute(
                "UPDATE books SET schema_version=? WHERE book_id=? AND schema_version<=?",
                (schema_version, book_id, schema_version),
            )
            if changed.rowcount == 0:
                row = self._conn.execute(
                    "SELECT schema_version FROM books WHERE book_id=?",
                    (book_id,),
                ).fetchone()
                if row is not None and row["schema_version"] > schema_version:
                    self._conn.rollback()
                    raise RuntimeError("catalog schema_version is newer than the opened book store")
            self._conn.commit()

    def list_books(self):
        with self._lock:
            rows = self._conn.execute("SELECT * FROM books ORDER BY added_at, book_id").fetchall()
        return [dict(r) for r in rows]

    def update_book_metadata(self, book_id, *, title, author=None):
        """Repair display metadata without replacing a book or disturbing its reading frontier."""
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be a non-empty string")
        if author is not None and not isinstance(author, str):
            raise ValueError("author must be a string or null")
        normalized_title = " ".join(title.split())[:512]
        normalized_author = " ".join(author.split())[:512] if author and author.strip() else None
        with self._lock:
            changed = self._conn.execute(
                "UPDATE books SET title=?, author=? WHERE book_id=?",
                (normalized_title, normalized_author, book_id),
            )
            if changed.rowcount == 0:
                self._conn.rollback()
                raise ValueError(f"unknown book {book_id!r}")
            self._conn.commit()

    def remove_book(self, book_id):
        """Remove the book from the catalog (children first, FK-safe). The per-book memory.db file
        deletion is the LIT-24 lifecycle's job, not the catalog's."""
        with self._lock:
            self._conn.execute("DELETE FROM cost_reservations WHERE book_id=?", (book_id,))
            self._conn.execute("DELETE FROM cost_ledger WHERE book_id=?", (book_id,))
            self._conn.execute("DELETE FROM reading_state WHERE book_id=?", (book_id,))
            self._conn.execute("DELETE FROM books WHERE book_id=?", (book_id,))
            self._conn.commit()

    # ---- reading state (monotonic high-water) -----------------------------
    def get_state(self, book_id):
        with self._lock:
            r = self._conn.execute(
                "SELECT bookmark,cfi,ingest_progress,position_epoch,updated_at "
                "FROM reading_state WHERE book_id=?",
                (book_id,)).fetchone()
        return dict(r) if r else None

    def set_position(self, book_id, cfi, bookmark, *, expected_epoch=0):
        """Persist the reader position. bookmark is a MONOTONIC high-water mark made atomic IN SQL
        (MAX(bookmark, ?)) so it never regresses — even if a second instance/process touches the file
        (the in-process lock alone would lose that race). cfi is the latest reported position (resume
        UX; may move backward — see the module-docstring cfi note)."""
        self._check_int(bookmark)
        self._check_int(expected_epoch, "position_epoch")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reading_state SET bookmark = MAX(bookmark, ?), cfi = ?, updated_at = ? "
                "WHERE book_id = ? AND position_epoch = ?",
                (bookmark, cfi, _now(), book_id, expected_epoch))
            if cur.rowcount == 0:
                epoch_row = self._conn.execute(
                    "SELECT position_epoch FROM reading_state WHERE book_id=?", (book_id,)
                ).fetchone()
                self._conn.rollback()   # release the empty write tx — don't leak the WAL lock (pass-2 HIGH)
                if epoch_row is not None:
                    raise ValueError("position epoch changed; the reading position was reset")
                raise ValueError(f"unknown book {book_id!r}")
            row = self._conn.execute(
                "SELECT bookmark,cfi,ingest_progress,position_epoch "
                "FROM reading_state WHERE book_id=?", (book_id,)
            ).fetchone()
            self._conn.commit()
        return dict(row)

    def reset_position(self, book_id, *, expected_epoch):
        """Start a new reading pass without deleting memory or ingest receipts.

        The epoch change makes every delayed position report from the previous pass stale.
        """
        self._check_int(expected_epoch, "position_epoch")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reading_state SET bookmark=0, cfi=NULL, "
                "position_epoch=position_epoch+1, updated_at=? "
                "WHERE book_id=? AND position_epoch=? AND position_epoch < ?",
                (_now(), book_id, expected_epoch, 2**63 - 1),
            )
            if cur.rowcount == 0:
                epoch_row = self._conn.execute(
                    "SELECT position_epoch FROM reading_state WHERE book_id=?", (book_id,)
                ).fetchone()
                self._conn.rollback()
                if epoch_row is None:
                    raise ValueError(f"unknown book {book_id!r}")
                if epoch_row["position_epoch"] >= 2**63 - 1:
                    raise ValueError("position epoch is exhausted")
                raise ValueError("position epoch changed; the reading position was reset")
            row = self._conn.execute(
                "SELECT bookmark,cfi,ingest_progress,position_epoch "
                "FROM reading_state WHERE book_id=?", (book_id,)
            ).fetchone()
            self._conn.commit()
        return dict(row)

    def set_ingest_progress(self, book_id, n):
        """Highest chapter ordinal ingested — monotonic max IN SQL (a re-driven/older write can't move
        it back, cross-instance-safe)."""
        self._check_int(n, "ingest_progress")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reading_state SET ingest_progress = MAX(ingest_progress, ?), updated_at = ? "
                "WHERE book_id = ?", (n, _now(), book_id))
            if cur.rowcount == 0:
                self._conn.rollback()   # release the empty write tx — don't leak the WAL lock (pass-2 HIGH)
                raise ValueError(f"unknown book {book_id!r}")
            self._conn.commit()

    def finalize_ingest(self, book_id, n, *, cost=None, incarnation=None):
        """Idempotently reconcile one committed memory chapter into the catalog.

        Cost insertion and monotonic progress advancement share one ``BEGIN IMMEDIATE`` transaction.
        Retrying a durable memory completion receipt therefore repairs either catalog crash window
        without duplicating the successful extraction cost.
        """
        self._check_int(n, "ingest_progress")
        if cost is not None:
            self._check_cost_value(cost.get("input_tokens", 0), "input_tokens")
            self._check_cost_value(cost.get("output_tokens", 0), "output_tokens")
            usd = cost.get("usd", 0.0)
            self._check_usd(usd)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                if incarnation is not None:
                    current = self._conn.execute(
                        "SELECT 1 FROM books WHERE book_id=? AND incarnation=?", (book_id, incarnation)
                    ).fetchone()
                    if current is None:
                        raise ValueError(f"stale catalog incarnation for book {book_id!r}")
                if cost is not None:
                    self._conn.execute(
                        "INSERT INTO cost_ledger(book_id,chapter_ordinal,phase,model,input_tokens,"
                        "output_tokens,usd,at) VALUES (?,?, 'extraction',?,?,?,?,?) "
                        "ON CONFLICT(book_id,chapter_ordinal) WHERE phase='extraction' "
                        "AND chapter_ordinal IS NOT NULL DO UPDATE SET model=excluded.model, "
                        "input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens, "
                        "usd=excluded.usd",
                        (book_id, n, cost.get("model"), cost.get("input_tokens", 0),
                         cost.get("output_tokens", 0), cost.get("usd", 0.0), _now()))
                    # Every successful chunk for this chapter is aggregated into the one durable
                    # receipt above. Remove its now-reconciled reservations in this same transaction.
                    self._conn.execute(
                        "DELETE FROM cost_reservations WHERE book_id=? AND chapter_ordinal=? "
                        "AND phase='extraction'", (book_id, n)
                    )
                cur = self._conn.execute(
                    "UPDATE reading_state SET ingest_progress = MAX(ingest_progress, ?), updated_at = ? "
                    "WHERE book_id = ?", (n, _now(), book_id))
                if cur.rowcount == 0:
                    raise ValueError(f"unknown book {book_id!r}")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def high_water(self, book_id):
        """The persisted monotonic bookmark (the ceiling the route-layer scrubber clamps to; ADR D-A10)."""
        st = self.get_state(book_id)
        return st["bookmark"] if st else None

    # ---- cost ledger ------------------------------------------------------
    def record_cost(self, book_id, *, phase, model, input_tokens=0, output_tokens=0, usd=0.0,
                    chapter_ordinal=None):
        # usd must be numeric: SQLite affinity would otherwise store a stringified usd as TEXT and SUM()
        # would silently drop it -> under-counted spend (feeds LIT-21 ceilings). Fail loud instead.
        self._check_cost_value(input_tokens, "input_tokens")
        self._check_cost_value(output_tokens, "output_tokens")
        self._check_usd(usd)
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO cost_ledger(book_id,chapter_ordinal,phase,model,input_tokens,"
                    "output_tokens,usd,at) VALUES (?,?,?,?,?,?,?,?)",
                    (book_id, chapter_ordinal, phase, model, input_tokens, output_tokens, usd, _now()))
                self._conn.commit()
            except sqlite3.IntegrityError as e:                 # FK: unknown book — match add_book's contract
                self._conn.rollback()
                raise ValueError(f"unknown book {book_id!r}") from e

    @staticmethod
    def _check_cost_value(value, what):
        if (not isinstance(value, int) or isinstance(value, bool) or value < 0
                or value >= 2 ** 63):
            raise ValueError(f"{what} must be a non-negative SQLite int, got {value!r}")

    @staticmethod
    def _check_usd(value, what="usd"):
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value) or value < 0):
            raise ValueError(f"{what} must be a finite non-negative number, got {value!r}")

    def reserve_cost(self, book_id, *, phase, model, input_tokens, output_tokens, usd,
                     max_input_tokens, max_output_tokens, max_usd, chapter_ordinal=None):
        """Atomically reserve a worst-case completion against per-book ceilings.

        Ledger usage and every outstanding reservation participate in the same ``BEGIN IMMEDIATE``
        check, so concurrent calls cannot each observe the same remaining budget. Reservations are
        durable and fail closed across a crash until settled or explicitly reconciled.
        """
        for value, what in ((input_tokens, "input_tokens"), (output_tokens, "output_tokens"),
                            (max_input_tokens, "max_input_tokens"),
                            (max_output_tokens, "max_output_tokens")):
            self._check_cost_value(value, what)
        self._check_usd(usd)
        self._check_usd(max_usd, "max_usd")
        reservation_id = uuid.uuid4().hex
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                if self._conn.execute("SELECT 1 FROM books WHERE book_id=?", (book_id,)).fetchone() is None:
                    raise ValueError(f"unknown book {book_id!r}")
                ledger = self._conn.execute(
                    "SELECT COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0),"
                    "COALESCE(SUM(usd),0.0) FROM cost_ledger WHERE book_id=?", (book_id,)
                ).fetchone()
                pending = self._conn.execute(
                    "SELECT COALESCE(SUM(reserved_input_tokens),0),"
                    "COALESCE(SUM(reserved_output_tokens),0),COALESCE(SUM(reserved_usd),0.0) "
                    "FROM cost_reservations WHERE book_id=?", (book_id,)
                ).fetchone()
                projected = (ledger[0] + pending[0] + input_tokens,
                             ledger[1] + pending[1] + output_tokens,
                             ledger[2] + pending[2] + usd)
                limits = (max_input_tokens, max_output_tokens, max_usd)
                names = ("input-token", "output-token", "USD")
                for name, value, limit in zip(names, projected, limits):
                    if value > limit:
                        raise RuntimeError(
                            f"book {name} cost ceiling reached ({value:g} projected > {limit:g})"
                        )
                self._conn.execute(
                    "INSERT INTO cost_reservations(reservation_id,book_id,chapter_ordinal,phase,model,"
                    "reserved_input_tokens,reserved_output_tokens,reserved_usd,at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (reservation_id, book_id, chapter_ordinal, phase, model, input_tokens,
                     output_tokens, usd, _now()),
                )
                self._conn.commit()
                return reservation_id
            except Exception:
                self._conn.rollback()
                raise

    def note_reservation_actual(self, book_id, reservation_id, *, input_tokens, output_tokens, usd):
        """Replace a reservation's estimate with known actual usage while keeping it durable."""
        self._check_cost_value(input_tokens, "input_tokens")
        self._check_cost_value(output_tokens, "output_tokens")
        self._check_usd(usd)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cost_reservations SET reserved_input_tokens=?,reserved_output_tokens=?,"
                "reserved_usd=?,actual_input_tokens=?,actual_output_tokens=?,actual_usd=? "
                "WHERE reservation_id=? AND book_id=?",
                (input_tokens, output_tokens, usd, input_tokens, output_tokens, usd,
                 reservation_id, book_id),
            )
            if cur.rowcount != 1:
                self._conn.rollback()
                raise ValueError(f"unknown cost reservation {reservation_id!r}")
            self._conn.commit()

    def settle_cost(self, book_id, reservation_id, *, phase=None):
        """Move one completed/abandoned reservation into the ledger exactly once."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM cost_reservations WHERE reservation_id=? AND book_id=?",
                    (reservation_id, book_id),
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown cost reservation {reservation_id!r}")
                self._conn.execute(
                    "INSERT INTO cost_ledger(book_id,chapter_ordinal,phase,model,input_tokens,"
                    "output_tokens,usd,at) VALUES (?,?,?,?,?,?,?,?)",
                    (book_id, row["chapter_ordinal"], phase or row["phase"], row["model"],
                     row["reserved_input_tokens"], row["reserved_output_tokens"],
                     row["reserved_usd"], _now()),
                )
                self._conn.execute("DELETE FROM cost_reservations WHERE reservation_id=?",
                                   (reservation_id,))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def discard_cost_reservation(self, book_id, reservation_id):
        """Delete a proven-zero reservation (used only when the provider reports zero usage)."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM cost_reservations WHERE reservation_id=? AND book_id=?",
                (reservation_id, book_id),
            )
            if cur.rowcount != 1:
                self._conn.rollback()
                raise ValueError(f"unknown cost reservation {reservation_id!r}")
            self._conn.commit()

    def _delete_reservations_locked(self, book_id, reservation_ids):
        for reservation_id in reservation_ids:
            cur = self._conn.execute(
                "DELETE FROM cost_reservations WHERE reservation_id=? AND book_id=?",
                (reservation_id, book_id),
            )
            if cur.rowcount != 1:
                raise ValueError(f"unknown cost reservation {reservation_id!r}")

    def get_cost_reservations(self, book_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT reservation_id,chapter_ordinal,phase,model,reserved_input_tokens,"
                "reserved_output_tokens,reserved_usd,actual_input_tokens,actual_output_tokens,"
                "actual_usd,at FROM cost_reservations WHERE book_id=? ORDER BY at,reservation_id",
                (book_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def cost_reservation_ids(self, book_id, *, phase=None, chapter_ordinal=None):
        clauses, params = ["book_id=?"], [book_id]
        if phase is not None:
            clauses.append("phase=?")
            params.append(phase)
        if chapter_ordinal is not None:
            clauses.append("chapter_ordinal=?")
            params.append(chapter_ordinal)
        with self._lock:
            rows = self._conn.execute(
                "SELECT reservation_id FROM cost_reservations WHERE " + " AND ".join(clauses)
                + " ORDER BY at,reservation_id", params
            ).fetchall()
        return [row[0] for row in rows]

    def total_cost(self, book_id):
        with self._lock:
            r = self._conn.execute("SELECT COALESCE(SUM(usd), 0.0) FROM cost_ledger WHERE book_id=?",
                                   (book_id,)).fetchone()
        return r[0]

    def get_costs(self, book_id):
        """All cost-ledger entries for a book (insertion order), for cost display / LIT-21 ceilings."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT chapter_ordinal, phase, model, input_tokens, output_tokens, usd, at "
                "FROM cost_ledger WHERE book_id=? ORDER BY entry_id", (book_id,)).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        with self._lock:
            self._conn.close()
