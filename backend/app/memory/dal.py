"""LIT-5 — the spoiler-safe Data Access Layer (DAL). Productionized from the twice-reviewed spike
(spikes/lit-5-schema/dal.py) per ADR 0007 D-A1: the safety cores (`_authorizer`, `_select` funnel,
`_WriterCtx`, referential-closure helpers, every BookmarkView read) are lifted near-verbatim.

Production deltas vs the spike (ADR 0007):
  * connection opened `check_same_thread=False` + `PRAGMA busy_timeout` (the per-book lock in
    store.Store — not sqlite3's thread guard — serializes access; D-A2);
  * schema created/upgraded via the forward-only `migrations` runner, not a hardcoded executescript;
  * the `executed_sql` trace hook is OPT-IN (`trace=True`) — excluded from prod by default (D-A1);
  * a fail-closed `FACT_TABLES` assertion at open: the explicit set must be a superset of every
    `revealed_at`-bearing base table, and every base table must be guarded or infra-allow-listed (D-A6).

Four enforcement mechanisms (strongest first): per-connection SQLite AUTHORIZER, single FILTER FUNNEL
(`_select`), REFERENTIAL CLOSURE (visible-entity / live-chapter semijoins), REQUIRED BOOKMARK.
Honest scope (threat model, ADR 0002): the authorizer guards only THIS connection; the Store makes the
DAL the sole owner of it, so there is no ACCIDENTAL bypass through the DAL's own connection.
"""
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone

from app.book_types import BOOK_PROFILE_SIGNALS, BOOK_TYPES
from app.language import normalize_content_language

from . import corrections, migrations, vectors

SCHEMA_VERSION = migrations.CURRENT_VERSION

# Every fact-bearing table. Reads of these are gated by the authorizer. EXPLICIT + code-owned
# (ADR 0007 D-A6): book_meta has no `revealed_at` but MUST stay guarded.
FACT_TABLES = {
    "book_meta", "chapters", "ingested_chapters", "raw_chapters", "chapter_summaries", "entities",
    "aliases", "edges", "events", "event_participants", "themes", "entity_state",
    "chunks", "entity_corrections", vectors.VEC0_TABLE,
}
# Tables whose facts can be superseded IN STORY TIME -> carry invalid_at.
VALID_TIME_TABLES = {"entities", "edges", "events", "themes", "entity_state"}
# The virtual table is guarded above. Only the exact, version-probed shadow set and derived metadata
# are infrastructure; similarly named or newly introduced tables fail closed.
INFRA_TABLES: set[str] = {"vector_index_meta", *vectors.VEC0_SHADOW_TABLES}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _authorizer(db, action, arg1, arg2, dbname, trigger):
    """Deny fact-table reads unless THIS db's connection is inside a sanctioned funnel."""
    if action == sqlite3.SQLITE_READ and arg1 in FACT_TABLES:
        if not db._engaged:
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class _WriterCtx:
    """A trusted, guard-engaged write scope on one MemoryDB.

    Standalone scopes commit as before. Inside ``MemoryDB.transaction()``, writes join the outer
    transaction instead, so a multi-method operation can commit or roll back as one unit.
    """
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db._assert_in_session()
        self._prev = self.db._engaged
        self.db._engaged = True
        return self

    def __exit__(self, exc_type, *rest):
        try:
            if self.db._transaction_active:
                if exc_type is not None:
                    self.db._transaction_failed = True
            elif exc_type is None:
                try:
                    self.db._conn.commit()
                except BaseException as commit_error:
                    try:
                        self.db._rollback_or_poison()
                    except BaseException:
                        raise RuntimeError(
                            "MemoryDB commit and rollback both failed; connection poisoned"
                        ) from commit_error
                    raise
            else:
                self.db._rollback_or_poison()
        finally:
            self.db._engaged = self._prev
        return False


class _TransactionCtx:
    """One explicit outer transaction that absorbs all nested ``_WriterCtx`` scopes."""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db._assert_in_session()
        if self.db._transaction_active:
            raise RuntimeError("nested MemoryDB transactions are not supported")
        self.db._conn.execute("BEGIN IMMEDIATE")
        self.db._transaction_active = True
        self.db._transaction_failed = False
        return self

    def __exit__(self, exc_type, *rest):
        nested_failure = self.db._transaction_failed
        try:
            if exc_type is not None or nested_failure:
                self.db._rollback_or_poison()
            else:
                try:
                    self.db._conn.commit()
                except BaseException as commit_error:
                    try:
                        self.db._rollback_or_poison()
                    except BaseException:
                        raise RuntimeError(
                            "MemoryDB commit and rollback both failed; connection poisoned"
                        ) from commit_error
                    raise
        finally:
            self.db._transaction_active = False
            self.db._transaction_failed = False
        if exc_type is None and nested_failure:
            raise RuntimeError("MemoryDB transaction rolled back after a nested write failure")
        return False


class MemoryDB:
    """A single per-book memory.db. Reads are ONLY available via `.view(bookmark)`. Owned solely by
    store.Store, which serializes all access under a per-book lock (ADR 0007 D-A2)."""

    def __init__(self, path, book_id, *, meta=None, create=True, trace=False,
                 vector_backend="vec0"):
        self._book_id = book_id
        if vector_backend not in {"vec0", "bruteforce"}:
            raise ValueError("vector_backend must be 'vec0' or 'bruteforce'")
        self._vector_backend = vector_backend
        self._vec0_extension_version = None
        self._engaged = False                            # PER-CONNECTION guard flag
        self._transaction_active = False
        self._transaction_failed = False
        self._poisoned = False
        # The thread permitted to touch this connection. store.Store sets it to the current thread for
        # the duration of each `with store.book()` session and clears it on exit; off-session access
        # (an escaped view/handle) then fails LOUD instead of racing the sole connection (ADR 0007 D-A2).
        self._active_owner = threading.get_ident()
        # cached_statements=0: sqlite's AUTHORIZER runs only at statement-PREPARE time, and Python's
        # sqlite3 caches prepared statements by exact SQL string — so a guarded read prepared while
        # _engaged (e.g. _audit_all's `SELECT * FROM <fact table>`) would afterwards raw-execute with
        # NO authorizer check, off-session, until LRU eviction (pass-2 gate review, PROVED: 38 rows
        # incl. every future entity). Disabling the cache makes the authorizer run on EVERY execution;
        # at per-book scale the re-prepare cost is negligible.
        self._conn = sqlite3.connect(path, check_same_thread=False, cached_statements=0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA foreign_keys = ON")
        existing_vec0 = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND (name=? OR name LIKE 'chunks_vec_%') LIMIT 1", (vectors.VEC0_TABLE,)
        ).fetchone()
        # Brute force can open a previously indexed store, so it must load the module needed to parse
        # that schema. Configured vec0 always loads it and never silently switches backend.
        if vector_backend == "vec0" or existing_vec0:
            self._vec0_extension_version = vectors.load_extension(self._conn)
        self.executed_sql = []                           # trace log (opt-in; used by the no-bypass test)
        if trace:
            self._conn.set_trace_callback(self.executed_sql.append)
        # Schema via the forward-only migration runner. ONE create path (ADR 0007 D-A6): baseline DDL ->
        # stamp BASELINE_VERSION on a fresh book -> walk to CURRENT, so a new book opened under CURRENT>1
        # also gets every later migration (not just the baseline).
        with self._writer():
            migrations.ensure_baseline(self._conn)
            exists = self._conn.execute("SELECT book_id FROM book_meta LIMIT 1").fetchone()
            if exists is None:
                if not create:
                    raise ValueError(f"no book at {path!r} (create=False)")
                m = meta or {}
                self._conn.execute(
                    "INSERT INTO book_meta(book_id,title,author,source,source_id,file_hash,"
                    "schema_version,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (book_id, m.get("title") or book_id, m.get("author"), m.get("source"),
                     m.get("source_id"), m.get("file_hash"), migrations.BASELINE_VERSION, _now()))
            migrations.migrate(self._conn)               # walk stored -> CURRENT (fresh and existing)
        # Identity assert: the opened book_id must match the file, else view() would fail-open-to-empty.
        with self._writer():
            row = self._conn.execute("SELECT book_id FROM book_meta LIMIT 1").fetchone()
        if row and row[0] != book_id:
            raise ValueError(f"book_id mismatch: opened as {book_id!r} but file holds {row[0]!r}")
        # Initialization is a trusted write path. Runtime KNN reads are authorizer-guarded below.
        if self._vector_backend == "vec0" and migrations.CURRENT_VERSION >= 6:
            with self._writer():
                self._ensure_vec0_index_locked()
        # Authorizer attached AFTER DDL/migration/index initialization so schema creation is not blocked.
        self._conn.set_authorizer(lambda *a: _authorizer(self, *a))
        # Fail-closed authorizer-set assertion (ADR 0007 D-A6).
        self._assert_fact_tables_closed()

    # ---- schema-shape helpers (used by the fail-closed assertion + tests) -----
    def _base_tables(self):
        with self._writer():
            return {r[0] for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}

    def _columns(self, table):
        with self._writer():
            return {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}

    def _assert_fact_tables_closed(self):
        """Every base table must be a declared fact table or explicitly infra-allow-listed, and every
        `revealed_at`-bearing table must be in FACT_TABLES — else fail the open (default-deny)."""
        tables = self._base_tables()
        for t in tables:                       # default-deny: every base table must be guarded or infra
            if t not in FACT_TABLES and t not in INFRA_TABLES:
                raise RuntimeError(
                    f"unguarded base table {t!r}: not in FACT_TABLES nor INFRA_TABLES (ADR 0007 D-A6 fail-closed)")
        for t in tables:                       # and every revealed_at-bearing table must be a fact table
            if "revealed_at" in self._columns(t) and t not in FACT_TABLES:
                raise RuntimeError(f"fact table {t!r} (has revealed_at) missing from FACT_TABLES")
        if migrations.CURRENT_VERSION >= 3:
            if "entity_corrections" not in tables:
                raise RuntimeError("schema v3 is missing the guarded entity_corrections table")
            if "invalid_at" not in self._columns("entities"):
                raise RuntimeError("schema v3 is missing entities.invalid_at")
            if "revealed_at" not in self._columns("event_participants"):
                raise RuntimeError("schema v3 is missing event_participants.revealed_at")
        if migrations.CURRENT_VERSION >= 4:
            required = {
                "book_type",
                "book_type_confidence",
                "book_type_detector_version",
                "book_type_signals",
            }
            if not required <= self._columns("book_meta"):
                raise RuntimeError("schema v4 is missing the book profile columns")
        if migrations.CURRENT_VERSION >= 5 and "content_language" not in self._columns("book_meta"):
            raise RuntimeError("schema v5 is missing book_meta.content_language")
        if migrations.CURRENT_VERSION >= 6:
            required = {"index_name", "backend", "extension_version",
                        "index_schema_version", "dimensions"}
            if "vector_index_meta" not in tables or not required <= self._columns("vector_index_meta"):
                raise RuntimeError("schema v6 is missing vector_index_meta")
        vec_objects = tables.intersection({vectors.VEC0_TABLE, *vectors.VEC0_SHADOW_TABLES})
        if vec_objects and vec_objects != {vectors.VEC0_TABLE, *vectors.VEC0_SHADOW_TABLES}:
            raise RuntimeError("partial vec0 schema detected; refusing to open")

    # ---- derived vec0 index ------------------------------------------------
    def _vec0_tables_locked(self):
        return {row[0] for row in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}

    def _canonical_vec_rows_locked(self, *, dimension=None, live_only=False):
        where = "c.book_id=?"
        params = [self._book_id]
        if dimension is not None:
            where += " AND c.embed_dim=?"
            params.append(dimension)
        if live_only:
            where += " AND c.retracted_at IS NULL AND ch.retracted_at IS NULL"
        rows = self._conn.execute(
            "SELECT c.chunk_id,c.chapter_key,c.revealed_at,c.vec,c.embed_model,c.embed_dim,"
            "ch.revealed_at AS chapter_revealed_at,c.retracted_at,ch.retracted_at AS "
            "chapter_retracted_at FROM chunks c JOIN chapters ch ON ch.chapter_key=c.chapter_key "
            f"AND ch.book_id=c.book_id WHERE {where} ORDER BY c.chunk_id", params,
        ).fetchall()
        result = []
        for row in rows:
            try:
                value = json.loads(row["vec"])
                serialized = vectors.serialize(value)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"chunk {row['chunk_id']} has an invalid stored embedding") from exc
            if row["embed_dim"] != len(value):
                raise RuntimeError(f"chunk {row['chunk_id']} embedding dimension metadata mismatch")
            result.append({
                "chunk_id": row["chunk_id"], "chapter_key": row["chapter_key"],
                "revealed_at": row["revealed_at"], "embedding": serialized,
                "embed_model": row["embed_model"] or vectors.NULL_MODEL,
                "embed_dim": row["embed_dim"],
                "chapter_revealed_at": row["chapter_revealed_at"],
                "retracted": int(row["retracted_at"] is not None),
                "chapter_retracted": int(row["chapter_retracted_at"] is not None),
            })
        return result

    def _vec0_dimension_locked(self, rows, dimension_hint=None):
        pin = self._conn.execute(
            "SELECT embed_dim FROM book_meta WHERE book_id=?", (self._book_id,)
        ).fetchone()[0]
        dimensions = {row["embed_dim"] for row in rows}
        if dimension_hint is not None:
            dimensions.add(dimension_hint)
        if pin is not None:
            dimensions.add(pin)
        if not dimensions:
            return None
        if any(isinstance(dim, bool) or not isinstance(dim, int) or dim < 1 for dim in dimensions):
            raise RuntimeError("vec0 index dimension is invalid")
        if len(dimensions) != 1:
            raise RuntimeError(f"vec0 index dimension mismatch: {sorted(dimensions)}")
        return next(iter(dimensions))

    def _vec0_insert(self, row):
        self._conn.execute(
            "INSERT INTO chunks_vec(rowid,embedding,book_id,chapter_key,revealed_at,retracted,"
            "chapter_revealed_at,chapter_retracted,embed_model) VALUES (?,?,?,?,?,?,?,?,?)",
            (row["chunk_id"], row["embedding"], self._book_id, row["chapter_key"],
             row["revealed_at"], row["retracted"], row["chapter_revealed_at"],
             row["chapter_retracted"], row["embed_model"]),
        )

    def _ensure_vec0_index_locked(self, dimension_hint=None):
        tables = self._vec0_tables_locked()
        has_table = vectors.VEC0_TABLE in tables
        present_shadows = {table for table in tables if table.startswith("chunks_vec_")}
        meta = self._conn.execute(
            "SELECT index_name,backend,extension_version,index_schema_version,dimensions "
            "FROM vector_index_meta"
        ).fetchall()
        live_rows = self._canonical_vec_rows_locked(live_only=True)
        dimension = self._vec0_dimension_locked(live_rows, dimension_hint)
        if has_table and dimension is None and len(meta) == 1:
            dimension = meta[0]["dimensions"]
        rows = self._canonical_vec_rows_locked(dimension=dimension) if dimension is not None else []
        if not has_table:
            if present_shadows or meta:
                raise RuntimeError("partial vec0 schema or metadata detected; refusing to open")
            if dimension is None:
                return
            self._conn.execute(vectors.create_table_sql(dimension))
            self._conn.execute(
                "INSERT INTO vector_index_meta(index_name,backend,extension_version,"
                "index_schema_version,dimensions) VALUES (?,?,?,?,?)",
                (vectors.VEC0_TABLE, "sqlite-vec", self._vec0_extension_version,
                 vectors.VEC0_INDEX_SCHEMA_VERSION, dimension),
            )
            for row in rows:
                self._vec0_insert(row)
            created_shadows = {table for table in self._vec0_tables_locked()
                               if table.startswith("chunks_vec_")}
            if created_shadows != vectors.VEC0_SHADOW_TABLES:
                raise RuntimeError("sqlite-vec shadow schema mismatch; refusing to open")
            return
        if present_shadows != vectors.VEC0_SHADOW_TABLES or len(meta) != 1:
            raise RuntimeError("partial vec0 schema or metadata detected; refusing to open")
        stored = meta[0]
        if (stored["index_name"] != vectors.VEC0_TABLE or stored["backend"] != "sqlite-vec"
                or stored["extension_version"] != self._vec0_extension_version
                or stored["index_schema_version"] != vectors.VEC0_INDEX_SCHEMA_VERSION
                or stored["dimensions"] != dimension):
            raise RuntimeError("vec0 schema metadata mismatch; refusing to open")
        columns = tuple(row[1] for row in self._conn.execute("PRAGMA table_info(chunks_vec)"))
        if columns != vectors.VEC0_COLUMNS:
            raise RuntimeError("vec0 virtual table column mismatch; refusing to open")
        actual = [tuple(row) for row in self._conn.execute(
            "SELECT rowid,embedding,book_id,chapter_key,revealed_at,retracted,"
            "chapter_revealed_at,chapter_retracted,embed_model FROM chunks_vec ORDER BY rowid")]
        expected = [
            (row["chunk_id"], row["embedding"], self._book_id, row["chapter_key"],
             row["revealed_at"], row["retracted"], row["chapter_revealed_at"],
             row["chapter_retracted"], row["embed_model"])
            for row in rows
        ]
        if actual != expected:
            raise RuntimeError("vec0 index content mismatch; refusing silent fallback")

    def _vec0_search(self, query_vec, bookmark, k, embed_model):
        self._assert_in_session()
        if isinstance(k, bool) or not isinstance(k, int) or k < 0:
            raise ValueError("search k must be a non-negative integer")
        if k == 0:
            return []
        if embed_model == "":
            return []  # canonical chunks use NULL for the unpinned model; empty is not that space
        query = vectors.normalize(query_vec)
        with self._writer():
            if vectors.VEC0_TABLE not in self._vec0_tables_locked():
                return []
            dimension = self._conn.execute(
                "SELECT dimensions FROM vector_index_meta WHERE index_name=?",
                (vectors.VEC0_TABLE,),
            ).fetchone()[0]
        if len(query) != dimension:
            return []
        sql = ("SELECT rowid FROM chunks_vec WHERE embedding MATCH ? AND k = ? AND book_id = ? "
               "AND revealed_at <= ? AND retracted = 0 AND chapter_revealed_at <= ? "
               "AND chapter_retracted = 0")
        params = [vectors.serialize(query), k, self._book_id, bookmark, bookmark]
        if embed_model is not None:
            sql += " AND embed_model = ?"
            params.append(embed_model)
        sql += " ORDER BY distance"
        previous = self._engaged
        self._engaged = True
        try:
            ids = [row[0] for row in self._conn.execute(sql, params).fetchall()]
        finally:
            self._engaged = previous
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        live = ("SELECT chapter_key FROM chapters WHERE book_id=? AND revealed_at<=? "
                "AND retracted_at IS NULL")
        where = f"chunk_id IN ({placeholders}) AND chapter_key IN ({live})"
        params = [*ids, self._book_id, bookmark]
        if embed_model is not None:
            where += " AND embed_model=? AND embed_dim=?"
            params.extend([embed_model, len(query)])
        rows = self._select(
            "chunks", "chunk_id,chapter_key,revealed_at,text,vec,embed_model,embed_dim",
            bookmark, where_extra=where, params=params,
        )
        if {row["chunk_id"] for row in rows} != set(ids):
            raise RuntimeError("vec0 candidate metadata disagrees with canonical spoiler filters")
        return vectors.rank(rows, query, k)

    # ---- guarded execution primitives -------------------------------------
    def _assert_in_session(self):
        """Fail LOUD if this connection is touched off its store.book() session (a different thread, or
        an escaped view used after the `with` block). With check_same_thread=False the per-book lock is
        the SOLE serializer; an off-lock touch would otherwise race/deadlock the connection (ADR 0007
        D-A2). Checked at the two chokepoints every DB access passes through (_select, _WriterCtx)."""
        if self._poisoned:
            raise RuntimeError("MemoryDB connection is poisoned after a rollback failure; reopen the Store")
        if self._active_owner != threading.get_ident():
            raise RuntimeError(
                "MemoryDB used outside its store.book() session — the per-book lock is not held by "
                "this thread (ADR 0007 D-A2: never retain the view/connection past the context)")

    def _writer(self):
        return _WriterCtx(self)

    def _rollback_or_poison(self):
        try:
            self._conn.rollback()
        except BaseException:
            self._poisoned = True
            try:
                self._conn.close()
            except BaseException:
                pass
            raise

    def transaction(self):
        """Group multiple DAL writes into one all-or-nothing SQLite transaction.

        The Store's per-book lock must already be held. Public writer methods keep using their normal
        ``_writer()`` scopes; while this context is active those scopes do not commit independently.
        """
        return _TransactionCtx(self)

    def _select(self, table, cols, bookmark, where_extra="", params=(), order=""):
        """THE funnel. Every view read passes through here; the spoiler clause is appended
        unconditionally. A read with bookmark=None is a programming error."""
        self._assert_in_session()
        # Fail-closed bookmark TYPE guard (memory-review pass-2 BLOCKER): revealed_at is an
        # INTEGER-affinity column, so a non-numeric TEXT bookmark (e.g. '', 'abc', a malformed CFI
        # projection) would make `revealed_at <= ?` TRUE for every row via storage-class ordering — a
        # total spoiler leak. Require an int ordinal; bool is an int subclass and is a bug here.
        if not isinstance(bookmark, int) or isinstance(bookmark, bool):
            raise ValueError(f"spoiler-safe read requires an int bookmark ordinal, got {bookmark!r}")
        if table not in FACT_TABLES:
            raise ValueError(f"_select target {table!r} is not a fact table")
        clause = "book_id = ? AND revealed_at <= ? AND retracted_at IS NULL"
        p = [self._book_id, bookmark]
        if table in VALID_TIME_TABLES:
            clause += " AND (invalid_at IS NULL OR invalid_at > ?)"
            p.append(bookmark)
        if where_extra:
            clause += f" AND ({where_extra})"
            p.extend(params)
        sql = f"SELECT {cols} FROM {table} WHERE {clause}"
        if order:
            sql += f" ORDER BY {order}"
        prev = self._engaged
        self._engaged = True
        try:
            return self._conn.execute(sql, p).fetchall()
        finally:
            self._engaged = prev

    def view(self, bookmark):
        """The ONLY public read entry point. Returns a frontier-bound view."""
        return BookmarkView(self, self._book_id, bookmark)

    # ---- ingestion (trusted writer side) ----------------------------------
    def _ins(self, table, **cols):
        keys = ",".join(cols)
        qs = ",".join("?" * len(cols))
        with self._writer():
            cur = self._conn.execute(f"INSERT INTO {table}({keys}) VALUES ({qs})",
                                     tuple(cols.values()))
            return cur.lastrowid

    def add_chapter(self, chapter_key, revealed_at, href, title="", fragment="",
                    part_label="", kind="body", content_hash="", extractor_version="x1"):
        with self._writer():
            row = self._conn.execute(
                "SELECT content_hash, revealed_at FROM chapters "
                "WHERE chapter_key=? AND book_id=? AND retracted_at IS NULL",
                (chapter_key, self._book_id)).fetchone()
            if row:
                if row[0] == content_hash and row[1] == revealed_at:
                    return chapter_key                      # true no-op (delta-skip)
                self._conn.execute(
                    "UPDATE chapters SET revealed_at=?, content_hash=?, title=? "
                    "WHERE chapter_key=? AND book_id=?",
                    (revealed_at, content_hash, title, chapter_key, self._book_id))
                if self._vector_backend == "vec0" and vectors.VEC0_TABLE in self._vec0_tables_locked():
                    self._conn.execute(
                        "UPDATE chunks_vec SET chapter_revealed_at=? WHERE chapter_key=?",
                        (revealed_at, chapter_key))
                return chapter_key
        self._ins("chapters", chapter_key=chapter_key, book_id=self._book_id,
                  revealed_at=revealed_at, href=href, fragment=fragment, title=title,
                  part_label=part_label, kind=kind, content_hash=content_hash,
                  schema_version=SCHEMA_VERSION, extractor_version=extractor_version,
                  recorded_at=_now(), retracted_at=None)
        return chapter_key

    def chapter_is_ingested(self, chapter_key, content_hash):
        """LIT-7 completion marker for the ingestion pipeline.

        Only explicit v2 markers count. A legacy ``chapters`` row alone is deliberately NOT promoted:
        pre-LIT-7 complete and partial rows cannot be distinguished safely and require re-import/rebuild.
        """
        with self._writer():
            row = self._conn.execute(
                "SELECT 1 FROM ingested_chapters WHERE chapter_key=? AND book_id=? AND content_hash=?",
                (chapter_key, self._book_id, content_hash)).fetchone()
        return row is not None

    def _chapter_completion(self, chapter_key, ordinal, content_hash=None):
        with self._writer():
            row = self._conn.execute(
                "SELECT i.cost_pending,i.extractor_model,i.input_tokens,i.output_tokens,i.usd,"
                "i.content_hash AS marker_hash,c.content_hash AS chapter_hash,"
                "c.revealed_at AS chapter_revealed_at,r.revealed_at AS raw_revealed_at,"
                "r.text,r.content_hash AS raw_hash FROM ingested_chapters i "
                "JOIN chapters c ON c.chapter_key=i.chapter_key AND c.book_id=i.book_id "
                "LEFT JOIN raw_chapters r ON r.chapter_key=c.chapter_key AND r.book_id=c.book_id "
                "AND r.retracted_at IS NULL WHERE i.chapter_key=? AND i.book_id=? "
                "AND c.retracted_at IS NULL",
                (chapter_key, self._book_id)).fetchone()
        if row is None:
            return None
        actual_hash = hashlib.sha256((row["text"] or "").encode("utf-8")).hexdigest()[:16]
        if (row["chapter_revealed_at"] != ordinal
                or row["raw_revealed_at"] not in (None, ordinal)
                or row["marker_hash"] != row["chapter_hash"]
                or row["chapter_hash"] != actual_hash
                or row["raw_hash"] not in (None, actual_hash)
                or content_hash not in (None, actual_hash)):
            return None
        cost = None
        if row["cost_pending"]:
            cost = {"model": row["extractor_model"], "input_tokens": row["input_tokens"] or 0,
                    "output_tokens": row["output_tokens"] or 0, "usd": row["usd"] or 0.0}
        return {"cost": cost}

    def chapter_completion(self, chapter_key, ordinal, content_hash):
        """Return a receipt only when key, ordinal, and recomputed stored text hash all agree."""
        return self._chapter_completion(chapter_key, ordinal, content_hash)

    def completion_frontier(self, atoms):
        """Current contiguous durable v2 frontier for an already-loaded authoritative atom list."""
        frontier = 0
        for atom in atoms:
            if self._chapter_completion(atom["key"], atom["ordinal"]) is None:
                break
            frontier = atom["ordinal"]
        return frontier

    def mark_chapter_ingested(self, chapter_key, content_hash, *, cost=None):
        """Write the completion marker last; forbidden outside the chapter's outer transaction."""
        if not self._transaction_active:
            raise RuntimeError("chapter completion marker requires an active MemoryDB transaction")
        cost = cost or {}
        return self._ins("ingested_chapters", chapter_key=chapter_key, book_id=self._book_id,
                         content_hash=content_hash, cost_pending=int(bool(cost)),
                         extractor_model=cost.get("model"), input_tokens=cost.get("input_tokens"),
                         output_tokens=cost.get("output_tokens"), usd=cost.get("usd"), completed_at=_now())

    def chapter_live(self, chapter_key):
        """Is this chapter_key live at ANY content_hash? Key-only guarded existence read (sibling of
        chapter_is_ingested) — lets the LIT-6 ingestion pipeline FAIL LOUD on a changed-content re-ingest
        instead of crashing mid-write (add_chapter would UPDATE the row, then add_raw hits the
        raw_chapters PK). An ingestion fact, not a story fact -> not a BookmarkView funnel read. ADR 0007
        D-A3: ingest_chapter is first-ingest only. Atomic re-extraction (retract the chapter + all its
        derived rows, freeing the chapter_key) is LIT-19 and not yet built — retract_chapter alone does
        NOT free the chapter_key PK, so retract-then-reingest is not a working remedy."""
        with self._writer():
            row = self._conn.execute(
                "SELECT 1 FROM chapters WHERE chapter_key=? AND book_id=? AND retracted_at IS NULL",
                (chapter_key, self._book_id)).fetchone()
        return row is not None

    def add_raw(self, chapter_key, revealed_at, text, content_hash=""):
        return self._ins("raw_chapters", chapter_key=chapter_key, book_id=self._book_id,
                         revealed_at=revealed_at, text=text, char_count=len(text),
                         content_hash=content_hash, recorded_at=_now(), retracted_at=None)

    def add_summary(self, chapter_key, revealed_at, summary, kind="chapter", extractor_version="x1"):
        return self._ins("chapter_summaries", chapter_key=chapter_key, book_id=self._book_id,
                         revealed_at=revealed_at, kind=kind, summary=summary,
                         schema_version=SCHEMA_VERSION, extractor_version=extractor_version,
                         recorded_at=_now(), retracted_at=None)

    def add_entity(self, canonical_name, type, revealed_at, extractor_version="x1"):
        return self._ins("entities", book_id=self._book_id, canonical_name=canonical_name,
                         type=type, revealed_at=revealed_at, invalid_at=None,
                         schema_version=SCHEMA_VERSION,
                         extractor_version=extractor_version, recorded_at=_now(), retracted_at=None)

    def add_alias(self, entity_id, surface_form, revealed_at):
        return self._ins("aliases", entity_id=entity_id, book_id=self._book_id,
                         surface_form=surface_form, revealed_at=revealed_at,
                         recorded_at=_now(), retracted_at=None)

    def add_edge(self, src, dst, rel_type, label, revealed_at, invalid_at=None, extractor_version="x1"):
        return self._ins("edges", book_id=self._book_id, src_entity=src, dst_entity=dst,
                         rel_type=rel_type, label=label, revealed_at=revealed_at,
                         invalid_at=invalid_at, schema_version=SCHEMA_VERSION,
                         extractor_version=extractor_version, recorded_at=_now(), retracted_at=None)

    def replace_edge(self, old_edge_id, at, rel_type, label, extractor_version="x2"):
        """ATOMIC, GAP-FREE story-time supersession in ONE transaction."""
        with self._writer():
            old = self._conn.execute(
                "SELECT src_entity, dst_entity FROM edges WHERE edge_id=? AND book_id=?",
                (old_edge_id, self._book_id)).fetchone()
            if not old:
                raise ValueError("no such edge in this book")
            self._conn.execute("UPDATE edges SET invalid_at=? WHERE edge_id=? AND book_id=?",
                               (at, old_edge_id, self._book_id))
            cur = self._conn.execute(
                "INSERT INTO edges(book_id,src_entity,dst_entity,rel_type,label,revealed_at,"
                "invalid_at,schema_version,extractor_version,recorded_at,retracted_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (self._book_id, old["src_entity"], old["dst_entity"], rel_type, label, at,
                 None, SCHEMA_VERSION, extractor_version, _now(), None))
            return cur.lastrowid

    def end_edge(self, edge_id, at):
        """Genuine end with NO replacement (e.g. a character dies). book_id-scoped."""
        with self._writer():
            self._conn.execute("UPDATE edges SET invalid_at=? WHERE edge_id=? AND book_id=?",
                               (at, edge_id, self._book_id))

    def add_event(self, summary, revealed_at, order_idx, kind="beat", invalid_at=None,
                  participants=None, extractor_version="x1"):
        eid = self._ins("events", book_id=self._book_id, revealed_at=revealed_at,
                        order_idx=order_idx, summary=summary, kind=kind, invalid_at=invalid_at,
                        schema_version=SCHEMA_VERSION, extractor_version=extractor_version,
                        recorded_at=_now(), retracted_at=None)
        for (entity_id, role) in (participants or []):
            self.add_event_participant(eid, entity_id, role, revealed_at)
        return eid

    def add_event_participant(self, event_id, entity_id, role, revealed_at):
        """Add a bookmark-effective participant link (LIT-10 correction copies use this too)."""
        return self._ins("event_participants", event_id=event_id, entity_id=entity_id,
                         book_id=self._book_id, role=role, revealed_at=revealed_at)

    def end_event(self, event_id, at):
        """Story-time invalidation of an event (e.g. a rumour later disproved)."""
        with self._writer():
            self._conn.execute("UPDATE events SET invalid_at=? WHERE event_id=? AND book_id=?",
                               (at, event_id, self._book_id))

    def add_theme(self, name, description, revealed_at, extractor_version="x1"):
        return self._ins("themes", book_id=self._book_id, name=name, description=description,
                         revealed_at=revealed_at, schema_version=SCHEMA_VERSION,
                         extractor_version=extractor_version, recorded_at=_now(), retracted_at=None)

    def add_state(self, entity_id, revealed_at, status, invalid_at=None, extractor_version="x1"):
        return self._ins("entity_state", entity_id=entity_id, book_id=self._book_id,
                         revealed_at=revealed_at, invalid_at=invalid_at,
                         status_json=json.dumps(status), schema_version=SCHEMA_VERSION,
                         extractor_version=extractor_version, recorded_at=_now(), retracted_at=None)

    def replace_state(self, old_state_id, at, status, extractor_version="x2"):
        """Atomic, gap-free state transition (mirror of replace_edge)."""
        with self._writer():
            old = self._conn.execute(
                "SELECT entity_id FROM entity_state WHERE state_id=? AND book_id=?",
                (old_state_id, self._book_id)).fetchone()
            if not old:
                raise ValueError("no such state in this book")
            self._conn.execute("UPDATE entity_state SET invalid_at=? WHERE state_id=? AND book_id=?",
                               (at, old_state_id, self._book_id))
            cur = self._conn.execute(
                "INSERT INTO entity_state(entity_id,book_id,revealed_at,invalid_at,status_json,"
                "schema_version,extractor_version,recorded_at,retracted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (old["entity_id"], self._book_id, at, None, json.dumps(status),
                 SCHEMA_VERSION, extractor_version, _now(), None))
            return cur.lastrowid

    def add_chunk(self, chapter_key, revealed_at, text, vec, embed_model=None, embed_dim=None):
        # LIT-20: stamp embedding identity; if PINNED, the pin is authoritative (reject a mismatch).
        pin = self.pinned_identity()
        if pin and pin.get("embed_model"):
            em = embed_model if embed_model is not None else pin["embed_model"]
            ed = embed_dim if embed_dim is not None else len(vec)
            if em != pin["embed_model"] or len(vec) != pin["embed_dim"] or ed != pin["embed_dim"]:
                raise ValueError(f"chunk embed identity ({em}, dim {len(vec)}) != book pin "
                                 f"({pin['embed_model']}, dim {pin['embed_dim']}) — re-embed required, not mixed")
            embed_model, embed_dim = em, ed
        else:
            embed_dim = embed_dim if embed_dim is not None else len(vec)
        if self._vector_backend == "vec0":
            normalized = vectors.normalize(vec)
            if embed_dim != len(normalized):
                raise ValueError("chunk embed_dim does not match its vector length")
        with self._writer():
            if self._vector_backend == "vec0":
                self._ensure_vec0_index_locked(dimension_hint=len(vec))
            chapter = self._conn.execute(
                "SELECT revealed_at,retracted_at FROM chapters WHERE chapter_key=? AND book_id=?",
                (chapter_key, self._book_id)).fetchone()
            cur = self._conn.execute(
                "INSERT INTO chunks(book_id,chapter_key,revealed_at,text,vec,embed_model,embed_dim,"
                "retracted_at) VALUES (?,?,?,?,?,?,?,?)",
                (self._book_id, chapter_key, revealed_at, text, json.dumps(vec), embed_model,
                 embed_dim, None))
            if self._vector_backend == "vec0" and chapter:
                self._vec0_insert({
                    "chunk_id": cur.lastrowid, "chapter_key": chapter_key,
                    "revealed_at": revealed_at, "embedding": vectors.serialize(vec),
                    "embed_model": embed_model or vectors.NULL_MODEL,
                    "chapter_revealed_at": chapter["revealed_at"],
                    "retracted": 0,
                    "chapter_retracted": int(chapter["retracted_at"] is not None),
                })
            return cur.lastrowid

    # ---- LIT-20: per-book model pinning + safe-swap ------------------------
    def pin_models(self, extractor_model=None, synth_model=None, embed_model=None, embed_dim=None,
                   embed_canary=None):
        """Pin model identity at FIRST ingestion (idempotent COALESCE). REJECTS pinning an embed model
        when the book already holds UNSTAMPED (NULL embed_model) chunks (else default same-space search
        would silently hide those pre-pin vectors)."""
        if embed_model is not None:
            with self._writer():
                n = self._conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE book_id=? AND embed_model IS NULL "
                    "AND retracted_at IS NULL", (self._book_id,)).fetchone()[0]
            if n:
                raise ValueError(f"cannot pin embed model: {n} unstamped (pre-pin) chunk(s) exist — "
                                 f"pin BEFORE embedding, or re-embed them under the pinned model")
        can = json.dumps(embed_canary) if isinstance(embed_canary, (list, tuple)) else embed_canary
        with self._writer():
            self._conn.execute(
                "UPDATE book_meta SET extractor_model=COALESCE(extractor_model,?), "
                "synth_model=COALESCE(synth_model,?), embed_model=COALESCE(embed_model,?), "
                "embed_dim=COALESCE(embed_dim,?), embed_canary=COALESCE(embed_canary,?) WHERE book_id=?",
                (extractor_model, synth_model, embed_model, embed_dim, can, self._book_id))
            if self._vector_backend == "vec0" and embed_dim is not None:
                self._ensure_vec0_index_locked(dimension_hint=embed_dim)

    def repin_embedding(self, embed_model, embed_dim, embed_canary=None):
        """OVERWRITE the pinned embedding identity — only valid inside a re-embed migration that has
        ALREADY retracted (and will re-embed) the old-space vectors. Fail-closed (memory-review pass-2):
        REFUSE if any LIVE chunk remains under a different (or unstamped) embed_model, because default
        same-space search would then silently hide every such chunk — the silent-RAG-loss hazard
        pin_models guards against. Correct order: retract old-space chunks -> repin -> re-embed."""
        with self._writer():
            stale = self._conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE book_id=? AND retracted_at IS NULL "
                "AND (embed_model IS NULL OR embed_model != ? OR embed_dim != ?)",
                (self._book_id, embed_model, embed_dim)).fetchone()[0]
        if stale:
            raise ValueError(f"cannot repin to {embed_model!r}: {stale} live chunk(s) under a different "
                             f"embed model remain — retract + re-embed them first (re-embed migration order)")
        can = json.dumps(embed_canary) if isinstance(embed_canary, (list, tuple)) else embed_canary
        with self._writer():
            self._conn.execute(
                "UPDATE book_meta SET embed_model=?, embed_dim=?, embed_canary=? WHERE book_id=?",
                (embed_model, embed_dim, can, self._book_id))
            if self._vector_backend == "vec0":
                row = self._conn.execute(
                    "SELECT dimensions FROM vector_index_meta WHERE index_name=?",
                    (vectors.VEC0_TABLE,)).fetchone()
                if row is not None and row[0] != embed_dim:
                    self._conn.execute("DROP TABLE chunks_vec")
                    self._conn.execute("DELETE FROM vector_index_meta")
                self._ensure_vec0_index_locked(dimension_hint=embed_dim)

    def pinned_identity(self):
        with self._writer():
            r = self._conn.execute(
                "SELECT extractor_model, synth_model, embed_model, embed_dim, embed_canary "
                "FROM book_meta WHERE book_id=?", (self._book_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        if d.get("embed_canary"):
            try:
                d["embed_canary"] = json.loads(d["embed_canary"])
            except (ValueError, TypeError):
                pass
        return d

    # ---- LIT-9: advisory book profile -------------------------------------
    def set_book_profile(self, *, book_type, confidence, detector_version, signals):
        if book_type not in BOOK_TYPES:
            raise ValueError(f"unsupported book type {book_type!r}")
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("book profile confidence must be between 0 and 1")
        if not isinstance(detector_version, str) or not detector_version.strip():
            raise ValueError("book profile detector version is required")
        signals = tuple(signals)
        if len(signals) > 12 or any(signal not in BOOK_PROFILE_SIGNALS for signal in signals):
            raise ValueError("book profile signals must use the bounded detector vocabulary")
        with self._writer():
            self._conn.execute(
                "UPDATE book_meta SET book_type=?,book_type_confidence=?,"
                "book_type_detector_version=?,book_type_signals=? WHERE book_id=?",
                (book_type, confidence, detector_version, json.dumps(list(signals)), self._book_id),
            )

    def book_profile(self):
        with self._writer():
            row = self._conn.execute(
                "SELECT book_type,book_type_confidence,book_type_detector_version,book_type_signals "
                "FROM book_meta WHERE book_id=?",
                (self._book_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("book profile metadata is missing")
        try:
            signals = json.loads(row["book_type_signals"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("book profile signals are malformed") from exc
        if row["book_type"] not in BOOK_TYPES or not isinstance(signals, list):
            raise RuntimeError("book profile metadata is invalid")
        return {
            "book_type": row["book_type"],
            "confidence": row["book_type_confidence"],
            "detector_version": row["book_type_detector_version"],
            "signals": signals,
        }

    # ---- LIT-23: source content language ---------------------------------
    def set_content_language(self, content_language):
        normalized = normalize_content_language(content_language)
        supplied = (content_language.strip().replace("_", "-").lower()
                    if isinstance(content_language, str) else "")
        if normalized == "und" and supplied != "und":
            raise ValueError("content language must be a valid BCP-47-shaped tag or 'und'")
        with self._writer():
            self._conn.execute(
                "UPDATE book_meta SET content_language=? WHERE book_id=?",
                (normalized, self._book_id),
            )

    def content_language(self):
        with self._writer():
            row = self._conn.execute(
                "SELECT content_language FROM book_meta WHERE book_id=?", (self._book_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("content language metadata is missing")
        language = normalize_content_language(row["content_language"])
        if language != row["content_language"]:
            raise RuntimeError("content language metadata is malformed")
        return language

    # ---- LIT-19 re-extraction (transaction-time) --------------------------
    def _retract(self, table, where, params):
        with self._writer():
            chunk_ids = []
            if self._vector_backend == "vec0" and table == "chunks":
                chunk_ids = [row[0] for row in self._conn.execute(
                    f"SELECT chunk_id FROM chunks WHERE retracted_at IS NULL "
                    f"AND book_id=? AND ({where})", (self._book_id, *params))]
            self._conn.execute(
                f"UPDATE {table} SET retracted_at=? WHERE retracted_at IS NULL "
                f"AND book_id=? AND ({where})", (_now(), self._book_id, *params))
            if chunk_ids:
                placeholders = ",".join("?" for _ in chunk_ids)
                self._conn.execute(
                    f"UPDATE chunks_vec SET retracted=1 WHERE rowid IN ({placeholders})", chunk_ids)

    def reextract_summary(self, chapter_key, revealed_at, new_summary, kind="chapter",
                          extractor_version="x2"):
        self._retract("chapter_summaries", "chapter_key=? AND kind=?", (chapter_key, kind))
        return self.add_summary(chapter_key, revealed_at, new_summary, kind=kind,
                                extractor_version=extractor_version)

    def reextract_entity(self, entity_id, canonical_name, type=None, extractor_version="x2"):
        sets = "canonical_name=?, extractor_version=?, recorded_at=?"
        params = [canonical_name, extractor_version, _now()]
        if type is not None:
            sets = "canonical_name=?, type=?, extractor_version=?, recorded_at=?"
            params = [canonical_name, type, extractor_version, _now()]
        with self._writer():
            self._conn.execute(f"UPDATE entities SET {sets} WHERE entity_id=? AND book_id=?",
                               (*params, entity_id, self._book_id))
        return entity_id

    def retract_chapter(self, chapter_key):
        # Canonical fact retractions and derived-index deletion commit or roll back together.
        with self._writer():
            if self._vector_backend == "vec0" and vectors.VEC0_TABLE in self._vec0_tables_locked():
                self._conn.execute(
                    "UPDATE chunks_vec SET retracted=1,chapter_retracted=1 WHERE chapter_key=?",
                    (chapter_key,))
            stamp = _now()
            for table in ("chapters", "raw_chapters", "chapter_summaries", "chunks"):
                self._conn.execute(
                    f"UPDATE {table} SET retracted_at=? WHERE retracted_at IS NULL "
                    "AND book_id=? AND chapter_key=?", (stamp, self._book_id, chapter_key))

    # ---- LIT-10 bookmark-effective entity corrections -------------------
    def entity_correction_inventory(self, entity_ids, effective_at):
        return corrections.correction_inventory(self, entity_ids, effective_at)

    def entity_correction_history(self, effective_at):
        return corrections.correction_history(self, effective_at)

    def replace_entity(self, entity_id, *, effective_at, canonical_name, reason):
        return corrections.replace_entity(
            self,
            entity_id,
            effective_at=effective_at,
            canonical_name=canonical_name,
            reason=reason,
        )

    def split_entity(self, entity_id, *, effective_at, replacements, alias_assignments,
                     edge_assignments, event_assignments, reason=""):
        return corrections.split_entity(
            self,
            entity_id,
            effective_at=effective_at,
            replacements=replacements,
            alias_assignments=alias_assignments,
            edge_assignments=edge_assignments,
            event_assignments=event_assignments,
            reason=reason,
        )

    def merge_entities(self, entity_ids, *, effective_at, canonical_name, state, reason="",
                       event_roles=None):
        return corrections.merge_entities(
            self,
            entity_ids,
            effective_at=effective_at,
            canonical_name=canonical_name,
            state=state,
            reason=reason,
            event_roles=event_roles,
        )

    # ---- audit (explicitly bypasses the spoiler filter; for tooling only) --
    def _audit_all(self, table):
        """Deliberate, NAMED escape hatch for migration/audit tooling. NOT a view; never used by the
        app's view reads (ADR 0007 D-A2)."""
        with self._writer():
            return self._conn.execute(f"SELECT * FROM {table}").fetchall()

    def close(self):
        self._conn.close()


class BookmarkView:
    """A read-only window onto the memory AS OF a chapter ordinal. The only way views and RAG touch
    the store. Must be USED only while the Store's per-book lock is held (ADR 0007 D-A2)."""

    def __init__(self, db, book_id, bookmark):
        # Fail-closed at the view boundary too (defense in depth with _select): an int ordinal only —
        # a non-int (None, '', 'abc', float, bool) would otherwise leak the whole book (pass-2 BLOCKER).
        if not isinstance(bookmark, int) or isinstance(bookmark, bool):
            raise ValueError(f"bookmark must be an int chapter ordinal, got {bookmark!r}")
        self._db = db
        self.book_id = book_id
        self.bookmark = bookmark

    def _sel(self, table, cols, where="", params=(), order=""):
        return self._db._select(table, cols, self.bookmark, where, params, order)

    # referential-closure helpers ------------------------------------------
    def _vis_entities(self):
        return ("SELECT entity_id FROM entities WHERE book_id=? AND revealed_at<=? "
                "AND retracted_at IS NULL AND (invalid_at IS NULL OR invalid_at>?)",
                [self.book_id, self.bookmark, self.bookmark])

    def _live_chapters(self):
        return ("SELECT chapter_key FROM chapters WHERE book_id=? AND revealed_at<=? "
                "AND retracted_at IS NULL", [self.book_id, self.bookmark])

    # --- structured reads --------------------------------------------------
    def chapters(self):
        return self._sel("chapters", "chapter_key, revealed_at, title, part_label",
                         order="revealed_at, chapter_key")

    def characters(self):
        return self._sel("entities", "entity_id, canonical_name, type, revealed_at",
                         where="type = ?", params=("character",), order="revealed_at, entity_id")

    def entities_of_type(self, t):
        return self._sel("entities", "entity_id, canonical_name, type, revealed_at",
                         where="type = ?", params=(t,), order="revealed_at, entity_id")

    def aliases_of(self, entity_id):
        sub, sp = self._vis_entities()
        return self._sel("aliases", "surface_form, revealed_at",
                         where=f"entity_id = ? AND entity_id IN ({sub})",
                         params=[entity_id, *sp], order="revealed_at, alias_id")

    def relationships(self):
        sub, sp = self._vis_entities()
        return self._sel("edges", "edge_id, src_entity, dst_entity, rel_type, label, "
                         "revealed_at, invalid_at",
                         where=f"src_entity IN ({sub}) AND dst_entity IN ({sub})",
                         params=[*sp, *sp], order="revealed_at, edge_id")

    def timeline(self):
        return self._sel("events", "event_id, revealed_at, order_idx, summary, kind",
                         order="revealed_at, order_idx, event_id")

    def participants_of(self, event_id):
        return self._sel(
            "entities", "entity_id, canonical_name, type",
            where="EXISTS (SELECT 1 FROM events ev WHERE ev.event_id=? AND ev.book_id=? "
                  "AND ev.revealed_at<=? AND ev.retracted_at IS NULL "
                  "AND (ev.invalid_at IS NULL OR ev.invalid_at>?)) "
                  "AND entity_id IN (SELECT entity_id FROM event_participants "
                  "WHERE event_id=? AND book_id=? AND revealed_at<=?)",
            params=[event_id, self.book_id, self.bookmark, self.bookmark,
                    event_id, self.book_id, self.bookmark],
            order="revealed_at, entity_id")

    def events_for(self, entity_id):
        return self._sel(
            "events", "event_id, revealed_at, summary",
            where="EXISTS (SELECT 1 FROM entities e WHERE e.entity_id=? AND e.book_id=? "
                  "AND e.revealed_at<=? AND e.retracted_at IS NULL "
                  "AND (e.invalid_at IS NULL OR e.invalid_at>?)) "
                  "AND event_id IN (SELECT event_id FROM event_participants "
                  "WHERE entity_id=? AND book_id=? AND revealed_at<=?)",
            params=[entity_id, self.book_id, self.bookmark, self.bookmark,
                    entity_id, self.book_id, self.bookmark],
            order="revealed_at, event_id")

    def themes(self):
        return self._sel("themes", "theme_id, name, description, revealed_at",
                         order="revealed_at, theme_id")

    def current_state(self, entity_id):
        sub, sp = self._vis_entities()
        rows = self._sel("entity_state", "state_id, revealed_at, status_json",
                         where=f"entity_id = ? AND entity_id IN ({sub})",
                         params=[entity_id, *sp], order="revealed_at DESC, state_id DESC")
        return rows[0] if rows else None

    def chapter_summaries(self):
        csub, cp = self._live_chapters()
        return self._sel("chapter_summaries", "chapter_key, revealed_at, summary",
                         where=f"kind = ? AND chapter_key IN ({csub})",
                         params=["chapter", *cp], order="revealed_at, summary_id")

    def raw_text(self, chapter_key):
        csub, cp = self._live_chapters()
        rows = self._sel("raw_chapters", "text",
                         where=f"chapter_key = ? AND chapter_key IN ({csub})",
                         params=[chapter_key, *cp])
        return rows[0]["text"] if rows else None

    def bio(self, entity_id):
        ent = self._sel("entities", "canonical_name, type, revealed_at",
                        where="entity_id = ?", params=[entity_id])
        if not ent:
            return None
        st = self.current_state(entity_id)
        return {
            "name": ent[0]["canonical_name"],
            "type": ent[0]["type"],
            "first_seen": ent[0]["revealed_at"],
            "aliases": [r["surface_form"] for r in self.aliases_of(entity_id)],
            "state": json.loads(st["status_json"]) if st else None,
            "appears_in_events": [r["event_id"] for r in self.events_for(entity_id)],
        }

    def catch_me_up(self):
        recap = self._sel("chapter_summaries", "summary, revealed_at",
                          where="kind = ?", params=["rolling-recap"],
                          order="revealed_at DESC, summary_id DESC")
        return {
            "as_of_chapter": self.bookmark,
            "recap": recap[0]["summary"] if recap else None,
            "cast_size": len(self.characters()),
            "open_threads": len(self.relationships()),
        }

    # --- RAG / KNN (the vector path inherits the SAME filter) --------------
    def search(self, query_vec, k=3, embed_model=None):
        """Spoiler-safe semantic search. Candidate set pulled through `_select` (book_id + revealed_at
        + retracted_at) AND restricted to live chapters, then ranked by cosine. Same embedding space
        only (LIT-20). The candidate FILTER stays here in the funnel; ADR 0007 D-A4 extracts only the
        ranker into vectors.py in a later step (this is the lifted spike behaviour)."""
        if embed_model is None:
            pin = self._db.pinned_identity() if hasattr(self._db, "pinned_identity") else None
            embed_model = pin["embed_model"] if (pin and pin.get("embed_model")) else None
        if self._db._vector_backend == "vec0":
            return self._db._vec0_search(query_vec, self.bookmark, k, embed_model)
        csub, cp = self._live_chapters()
        where, params = f"chapter_key IN ({csub})", list(cp)
        if embed_model is not None:
            where += " AND embed_model = ? AND embed_dim = ?"
            params += [embed_model, len(query_vec)]
        rows = self._sel("chunks", "chunk_id, chapter_key, revealed_at, text, vec, embed_model, embed_dim",
                         where=where, params=params)
        # The candidate FILTER (above) is the funnel's job; ranking is delegated to the DB-free
        # vectors.rank backend (ADR 0007 D-A4) — a regression there cannot leak (the filter is upstream).
        return vectors.rank(rows, query_vec, k)
