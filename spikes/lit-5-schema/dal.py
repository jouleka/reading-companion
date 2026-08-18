#!/usr/bin/env python3
"""LIT-5 spike — the spoiler-safe Data Access Layer (DAL).  [rev 2 — post adversarial review]

The keystone of the whole product: EVERY view reads through this layer, so this is
where spoiler-safety becomes structurally unbypassable rather than a promise.

Four enforcement mechanisms, strongest first:

 1. SQLite AUTHORIZER (engine-level, PER CONNECTION).  Each connection denies
    SQLITE_READ on any fact table unless THIS connection's guard flag is engaged.
    Only `_select` (view reads) and `_writer` (ingestion) engage it. Result: code that
    grabs the raw connection and runs `SELECT * FROM entities` is DENIED by SQLite
    itself. The flag is per-connection, so a writer on book B can NOT unlock a raw
    read of book A (a global thread-local flag — the rev-1 bug — could).

 2. SINGLE FILTER FUNNEL.  Every view read goes through ONE method, `_select`, which
    ALWAYS appends the canonical spoiler clause:
        book_id = ? AND revealed_at <= ? AND retracted_at IS NULL
        [ AND (invalid_at IS NULL OR invalid_at > ?) ]   -- valid-time tables only

 3. REFERENTIAL CLOSURE.  The per-row filter is necessary but NOT sufficient: a row
    with revealed_at <= bookmark may REFERENCE an entity revealed in the future. So
    every entity-referencing read SEMIJOINS the visible-entity set, and chunk/summary
    reads semijoin the live-chapter set. No read can surface an unmet entity or an
    orphaned-chapter chunk. (This closed a BLOCKER found in adversarial review.)

 4. REQUIRED BOOKMARK.  Reads are only reachable via `MemoryDB.view(bookmark)`.

Honest scope (threat model): Python has no true `private`, and the authorizer guards
only THIS connection. A second `sqlite3.connect(path)` with no authorizer can read
everything; willfully setting the guard flag also bypasses. The guarantee is: no
ACCIDENTAL bypass through the DAL's own connection. In the app the DAL is the sole
owner of the connection. See ADR 0002 "threat model".

Stdlib only (sqlite3). The vector store is a JSON-in-a-column stand-in for sqlite-vec
`vec0`; the prototype proves the KNN candidate set inherits the same filter — it does
NOT prove vec0's pre-filter recall (routed to a vector spike).
"""
import json
import math
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION = 1
HERE = os.path.dirname(os.path.abspath(__file__))

# Every fact-bearing table. Reads of these are gated by the authorizer.
FACT_TABLES = {
    "book_meta", "chapters", "raw_chapters", "chapter_summaries", "entities",
    "aliases", "edges", "events", "event_participants", "themes", "entity_state",
    "chunks",
}
# Tables whose facts can be superseded IN STORY TIME -> carry invalid_at.
VALID_TIME_TABLES = {"edges", "events", "themes", "entity_state"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _authorizer(db, action, arg1, arg2, dbname, trigger):
    """Deny fact-table reads unless THIS db's connection is inside a sanctioned funnel."""
    if action == sqlite3.SQLITE_READ and arg1 in FACT_TABLES:
        if not db._engaged:
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class _WriterCtx:
    """A trusted, guard-engaged, committing transaction on one MemoryDB."""
    def __init__(self, db):
        self.db = db
    def __enter__(self):
        self._prev = self.db._engaged
        self.db._engaged = True
        return self
    def __exit__(self, exc_type, *rest):
        try:
            if exc_type is None:
                self.db._conn.commit()
            else:
                self.db._conn.rollback()
        finally:
            self.db._engaged = self._prev
        return False


class MemoryDB:
    """A single per-book memory.db. Reads are ONLY available via `.view(bookmark)`."""

    def __init__(self, path, book_id, title="", author="", source="", source_id="",
                 file_hash="", create=True):
        self._book_id = book_id
        self._engaged = False                            # PER-CONNECTION guard flag
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self.executed_sql = []                           # trace log (for the no-bypass test)
        self._conn.set_trace_callback(self.executed_sql.append)
        if create:
            with self._writer():
                self._conn.executescript(open(os.path.join(HERE, "schema.sql")).read())
                self._conn.execute(
                    "INSERT OR IGNORE INTO book_meta(book_id,title,author,source,source_id,"
                    "file_hash,schema_version,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (book_id, title, author, source, source_id, file_hash, SCHEMA_VERSION, _now()))
        # Authorizer is attached AFTER DDL so schema creation is not blocked. The closure
        # binds THIS db, so the guard flag is per-connection.
        self._conn.set_authorizer(lambda *a: _authorizer(self, *a))
        # Identity assert: the book_id we were opened with must match what is in the file,
        # else every view() would silently fail-open-to-empty (looks like "nothing ingested").
        with self._writer():
            row = self._conn.execute("SELECT book_id FROM book_meta LIMIT 1").fetchone()
        if row and row[0] != book_id:
            raise ValueError(f"book_id mismatch: opened as {book_id!r} but file holds {row[0]!r}")

    # ---- guarded execution primitives -------------------------------------
    def _writer(self):
        return _WriterCtx(self)

    def _select(self, table, cols, bookmark, where_extra="", params=(), order=""):
        """THE funnel. Every view read passes through here; the spoiler clause is
        appended unconditionally. A read with bookmark=None is a programming error."""
        if bookmark is None:
            raise ValueError("spoiler-safe read requires an explicit bookmark")
        if table not in FACT_TABLES:                     # contract: funnel reads fact tables only
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
        # delta-skip keyed on (content_hash AND revealed_at): identical content at the
        # same ordinal -> no-op. An ordinal-only change (re-segmentation) RE-STAMPS the
        # row in place rather than silently skipping (which would desync the frontier).
        with self._writer():
            row = self._conn.execute(
                "SELECT content_hash, revealed_at FROM chapters "
                "WHERE chapter_key=? AND book_id=? AND retracted_at IS NULL",
                (chapter_key, self._book_id)).fetchone()
            if row:
                if row[0] == content_hash and row[1] == revealed_at:
                    return chapter_key                      # true no-op
                self._conn.execute(
                    "UPDATE chapters SET revealed_at=?, content_hash=?, title=? "
                    "WHERE chapter_key=? AND book_id=?",
                    (revealed_at, content_hash, title, chapter_key, self._book_id))
                return chapter_key
        self._ins("chapters", chapter_key=chapter_key, book_id=self._book_id,
                  revealed_at=revealed_at, href=href, fragment=fragment, title=title,
                  part_label=part_label, kind=kind, content_hash=content_hash,
                  schema_version=SCHEMA_VERSION, extractor_version=extractor_version,
                  recorded_at=_now(), retracted_at=None)
        return chapter_key

    def add_raw(self, chapter_key, revealed_at, text, content_hash=""):
        return self._ins("raw_chapters", chapter_key=chapter_key, book_id=self._book_id,
                         revealed_at=revealed_at, text=text, char_count=len(text),
                         content_hash=content_hash, recorded_at=_now(), retracted_at=None)

    def add_summary(self, chapter_key, revealed_at, summary, kind="chapter",
                    extractor_version="x1"):
        return self._ins("chapter_summaries", chapter_key=chapter_key, book_id=self._book_id,
                         revealed_at=revealed_at, kind=kind, summary=summary,
                         schema_version=SCHEMA_VERSION, extractor_version=extractor_version,
                         recorded_at=_now(), retracted_at=None)

    def add_entity(self, canonical_name, type, revealed_at, extractor_version="x1"):
        return self._ins("entities", book_id=self._book_id, canonical_name=canonical_name,
                         type=type, revealed_at=revealed_at, schema_version=SCHEMA_VERSION,
                         extractor_version=extractor_version, recorded_at=_now(), retracted_at=None)

    def add_alias(self, entity_id, surface_form, revealed_at):
        return self._ins("aliases", entity_id=entity_id, book_id=self._book_id,
                         surface_form=surface_form, revealed_at=revealed_at,
                         recorded_at=_now(), retracted_at=None)

    def add_edge(self, src, dst, rel_type, label, revealed_at, invalid_at=None,
                 extractor_version="x1"):
        return self._ins("edges", book_id=self._book_id, src_entity=src, dst_entity=dst,
                         rel_type=rel_type, label=label, revealed_at=revealed_at,
                         invalid_at=invalid_at, schema_version=SCHEMA_VERSION,
                         extractor_version=extractor_version, recorded_at=_now(), retracted_at=None)

    def replace_edge(self, old_edge_id, at, rel_type, label, extractor_version="x2"):
        """ATOMIC, GAP-FREE story-time supersession: in ONE transaction close the prior
        relationship's validity window at `at` and open the replacement at revealed_at=at,
        so the relationship is never momentarily absent (no gap) and never doubled (no overlap)."""
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
            self._ins("event_participants", event_id=eid, entity_id=entity_id,
                      book_id=self._book_id, role=role)        # pure link, no temporal stamps
        return eid

    def end_event(self, event_id, at):
        """Story-time invalidation of an event (e.g. a rumour later disproved). The
        participant link carries no stamps, so event visibility (via the join) is the
        single source of truth — there is nothing to keep in sync."""
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
        # LIT-20: stamp the embedding-model identity on every vector so KNN never cosine-compares
        # across two embedding spaces. If the book is PINNED, the pin is AUTHORITATIVE: the chunk must
        # carry the pinned embed_model and the pinned dim, else we REJECT (a wrong/truncated-dim or
        # wrong-model vector is silent corruption). If unpinned (legacy/stub), back-compat: stamp what
        # was passed and infer the dim.
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
        return self._ins("chunks", book_id=self._book_id, chapter_key=chapter_key,
                         revealed_at=revealed_at, text=text, vec=json.dumps(vec),
                         embed_model=embed_model, embed_dim=embed_dim, retracted_at=None)

    # ---- LIT-20: per-book model pinning + safe-swap ------------------------
    def pin_models(self, extractor_model=None, synth_model=None, embed_model=None, embed_dim=None,
                   embed_canary=None):
        """Pin the model identity at FIRST ingestion (idempotent: COALESCE only sets columns still
        NULL). A later authorized change uses repin_embedding() (overwrite) inside the migration txn.
        REJECTS pinning an embed model when the book already holds UNSTAMPED (NULL embed_model) chunks —
        else default same-space search would silently hide those pre-pin vectors (re-embed instead)."""
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

    def repin_embedding(self, embed_model, embed_dim, embed_canary=None):
        """OVERWRITE the pinned embedding identity — only valid as part of a re-embed migration that
        also retracts+re-embeds the vectors (so book_meta and chunks stay consistent)."""
        can = json.dumps(embed_canary) if isinstance(embed_canary, (list, tuple)) else embed_canary
        with self._writer():
            self._conn.execute(
                "UPDATE book_meta SET embed_model=?, embed_dim=?, embed_canary=? WHERE book_id=?",
                (embed_model, embed_dim, can, self._book_id))

    def pinned_identity(self):
        with self._writer():
            r = self._conn.execute(
                "SELECT extractor_model, synth_model, embed_model, embed_dim, embed_canary "
                "FROM book_meta WHERE book_id=?", (self._book_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        if d.get("embed_canary"):                       # stored as JSON vector -> parse back to list
            try:
                d["embed_canary"] = json.loads(d["embed_canary"])
            except (ValueError, TypeError):
                pass
        return d

    # ---- LIT-19 re-extraction (transaction-time) --------------------------
    def _retract(self, table, where, params):
        """Generic transaction-time supersede: mark current rows retracted (book_id-scoped)."""
        with self._writer():
            self._conn.execute(
                f"UPDATE {table} SET retracted_at=? WHERE retracted_at IS NULL "
                f"AND book_id=? AND ({where})", (_now(), self._book_id, *params))

    def reextract_summary(self, chapter_key, revealed_at, new_summary, kind="chapter",
                          extractor_version="x2"):
        """Replace a summary with a better extractor's output. Old row RETRACTED (txn-time),
        not deleted: current reads see only the new row; audit still sees the history.
        `kind` parameterised so a rolling-recap can be re-extracted too."""
        self._retract("chapter_summaries", "chapter_key=? AND kind=?", (chapter_key, kind))
        return self.add_summary(chapter_key, revealed_at, new_summary, kind=kind,
                                extractor_version=extractor_version)

    def reextract_entity(self, entity_id, canonical_name, type=None, extractor_version="x2"):
        """Re-extract a SPECIFIC entity's attributes (better canonical name / corrected type)
        IN PLACE, keyed on the stable entity_id, so all aliases/edges/events/state FKs
        pointing at it stay valid: identity is preserved (no new id, no orphaned sub-graph,
        no collapsing two distinct same-named entities — the rev-2 bug). Re-extracting a
        DERIVED fact whose key is not an FK target (e.g. a chapter summary) uses the
        retract-then-insert pattern instead (see reextract_summary). Attribute-level history
        and entity merge/split (un-merge/re-merge) are entity-resolution concerns owned by
        LIT-10, not this primitive."""
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
        """Re-segmentation removed this chapter: retract it AND cascade to all its derived
        rows so nothing orphaned (chunk/summary/raw) survives. book_id-scoped throughout."""
        for table in ("chapters", "raw_chapters", "chapter_summaries", "chunks"):
            self._retract(table, "chapter_key=?", (chapter_key,))

    # ---- audit (explicitly bypasses the spoiler filter; for tooling only) --
    def _audit_all(self, table):
        """Deliberate, NAMED escape hatch for migration/audit tooling. Returns ALL rows
        including retracted + future. NOT a view; never used by the app's view reads."""
        with self._writer():
            return self._conn.execute(f"SELECT * FROM {table}").fetchall()

    def close(self):
        self._conn.close()


class BookmarkView:
    """A read-only window onto the memory AS OF a chapter ordinal. The only way views
    (catch-me-up, character graph, timeline, notes) and RAG touch the store."""

    def __init__(self, db, book_id, bookmark):
        if bookmark is None:
            raise ValueError("BookmarkView requires a bookmark")
        self._db = db
        self.book_id = book_id
        self.bookmark = bookmark

    def _sel(self, table, cols, where="", params=(), order=""):
        return self._db._select(table, cols, self.bookmark, where, params, order)

    # referential-closure helpers ------------------------------------------
    def _vis_entities(self):
        """Subquery (+params) selecting the entity_ids visible at this bookmark."""
        return ("SELECT entity_id FROM entities WHERE book_id=? AND revealed_at<=? "
                "AND retracted_at IS NULL", [self.book_id, self.bookmark])

    def _live_chapters(self):
        return ("SELECT chapter_key FROM chapters WHERE book_id=? AND revealed_at<=? "
                "AND retracted_at IS NULL", [self.book_id, self.bookmark])

    # --- structured reads (character graph, timeline, notes) ---------------
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
        """Live edges as of the bookmark. REFERENTIALLY CLOSED: both endpoints must be
        visible entities, so the graph scrubber (LIT-15) can never surface an unmet node."""
        sub, sp = self._vis_entities()
        return self._sel("edges", "edge_id, src_entity, dst_entity, rel_type, label, "
                         "revealed_at, invalid_at",
                         where=f"src_entity IN ({sub}) AND dst_entity IN ({sub})",
                         params=[*sp, *sp], order="revealed_at, edge_id")

    def timeline(self):
        return self._sel("events", "event_id, revealed_at, order_idx, summary, kind",
                         order="revealed_at, order_idx, event_id")

    def participants_of(self, event_id):
        """Visible participant entities of an event (for swimlanes). Gated on BOTH the
        parent event's visibility (revealed_at/invalid_at/retracted_at — single source of
        truth in `events`) AND each participant entity's visibility. So a future or
        story-invalidated event never leaks its cast."""
        return self._sel(
            "entities", "entity_id, canonical_name, type",
            where="EXISTS (SELECT 1 FROM events ev WHERE ev.event_id=? AND ev.book_id=? "
                  "AND ev.revealed_at<=? AND ev.retracted_at IS NULL "
                  "AND (ev.invalid_at IS NULL OR ev.invalid_at>?)) "
                  "AND entity_id IN (SELECT entity_id FROM event_participants "
                  "WHERE event_id=? AND book_id=?)",
            params=[event_id, self.book_id, self.bookmark, self.bookmark, event_id, self.book_id],
            order="revealed_at, entity_id")

    def events_for(self, entity_id):
        """Visible events a (visible) entity participates in."""
        return self._sel(
            "events", "event_id, revealed_at, summary",
            where="EXISTS (SELECT 1 FROM entities e WHERE e.entity_id=? AND e.book_id=? "
                  "AND e.revealed_at<=? AND e.retracted_at IS NULL) "
                  "AND event_id IN (SELECT event_id FROM event_participants "
                  "WHERE entity_id=? AND book_id=?)",
            params=[entity_id, self.book_id, self.bookmark, entity_id, self.book_id],
            order="revealed_at, event_id")

    def themes(self):
        return self._sel("themes", "theme_id, name, description, revealed_at",
                         order="revealed_at, theme_id")

    def current_state(self, entity_id):
        """The single live state of a VISIBLE entity as of the bookmark. Deterministic
        tie-break (state_id DESC) so a same-chapter correction is reproducible."""
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
        """Spoiler-safe retrieval of retained raw text (LIT-19) — through the funnel, not
        the audit hatch. Like its sibling chapter-keyed tables (chunks, summaries) it ALSO
        semijoins the live-chapter set, so a row whose own revealed_at<=bookmark but whose
        parent chapter is in the future cannot leak."""
        csub, cp = self._live_chapters()
        rows = self._sel("raw_chapters", "text",
                         where=f"chapter_key = ? AND chapter_key IN ({csub})",
                         params=[chapter_key, *cp])
        return rows[0]["text"] if rows else None

    def bio(self, entity_id):
        """Spoiler-safe character bio. Returns None for a not-yet-visible entity."""
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

    # --- catch-me-up (HERO) ------------------------------------------------
    def catch_me_up(self):
        recap = self._sel("chapter_summaries", "summary, revealed_at",
                          where="kind = ?", params=["rolling-recap"],
                          order="revealed_at DESC, summary_id DESC")
        return {
            "as_of_chapter": self.bookmark,
            "recap": recap[0]["summary"] if recap else None,
            "cast_size": len(self.characters()),
            "open_threads": len(self.relationships()),   # referentially closed -> accurate
        }

    # --- RAG / KNN (the vector path inherits the SAME filter) --------------
    def search(self, query_vec, k=3, embed_model=None):
        """Spoiler-safe semantic search. Candidate set is pulled through `_select` (so book_id +
        revealed_at + retracted_at apply) AND restricted to chunks whose parent chapter is still live
        (no orphaned-chapter leak), then ranked by cosine. LIT-20: if `embed_model` is given, ONLY
        chunks stamped with that same model are compared (cosine across embedding spaces is
        meaningless); a query whose model matches no stored chunk returns nothing rather than garbage."""
        # LIT-20: same-space by default. If no embed_model is given, resolve the book's PINNED model
        # so the safe behaviour isn't opt-in (an unpinned legacy/stub store falls back to comparing all
        # — acceptable only because such stores hold a single embedder). Dim is filtered in SQL.
        if embed_model is None:
            pin = self._db.pinned_identity() if hasattr(self._db, "pinned_identity") else None
            embed_model = pin["embed_model"] if (pin and pin.get("embed_model")) else None
        csub, cp = self._live_chapters()
        where, params = f"chapter_key IN ({csub})", list(cp)
        if embed_model is not None:
            where += " AND embed_model = ? AND embed_dim = ?"
            params += [embed_model, len(query_vec)]
        rows = self._sel("chunks", "chunk_id, chapter_key, revealed_at, text, vec, embed_model, embed_dim",
                         where=where, params=params)
        scored = []
        for r in rows:
            v = json.loads(r["vec"])
            if len(v) != len(query_vec):
                continue                                # dim mismatch -> not comparable (defense-in-depth)
            scored.append((_cosine(query_vec, v), r["text"], r["revealed_at"], r["chapter_key"]))
        scored.sort(reverse=True, key=lambda t: (t[0], t[3]))
        return scored[:k]


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
