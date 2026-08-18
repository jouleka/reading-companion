"""Lifecycle / transaction-time / migration guards for the production memory store — the half of the
LIT-5 spike proof (ADR 0002 §6) the spoiler-frontier suite does not cover, plus the new ADR-0007 code:
re-extraction (no double-vision, identity-preserving), retract cascade, idempotent re-ingest, the
append-once signal, the forward-only migration walk + no-downgrade, and the fail-closed FACT_TABLES
negative path. These pin BLOCKER-class fixes (pass-2 #2/#3) against future regression.
"""
import sqlite3
import pytest

from app.memory import dal, migrations
from app.memory.store import Store


def _seed(store, bid="b"):
    with store.book(bid, meta=dict(title=bid.upper())) as mem:
        for n in (1, 2, 3):
            mem.add_chapter(f"{bid}:c{n}.xhtml", revealed_at=n, href=f"c{n}.xhtml", content_hash=f"h{n}")
            mem.add_raw(f"{bid}:c{n}.xhtml", n, text=f"prose {n} " * 10, content_hash=f"h{n}")
            mem.add_summary(f"{bid}:c{n}.xhtml", n, summary=f"v1 summary {n}")


# --- re-extraction (transaction-time) -------------------------------------------------------------
def test_reextract_summary_one_live_and_auditable(tmp_path):
    store = Store(data_dir=str(tmp_path))
    _seed(store)
    with store.book("b") as mem:
        mem.reextract_summary("b:c2.xhtml", revealed_at=2, new_summary="v2 better summary")
        live = [r["summary"] for r in mem.view(3).chapter_summaries() if r["chapter_key"] == "b:c2.xhtml"]
        assert live == ["v2 better summary"]                          # exactly one live row, the new one
        audit = [r for r in mem._audit_all("chapter_summaries")
                 if r["chapter_key"] == "b:c2.xhtml" and r["kind"] == "chapter"]
        assert len(audit) == 2                                        # history retained (v1 retracted + v2)


def test_reextract_entity_identity_preserving(tmp_path):
    """ADR 0002 pass-2 fix #3: re-extracting one of two SAME-NAME entities updates exactly that row in
    place (stable id, sub-graph FK intact) and never collapses the two."""
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")) as mem:
        mem.add_chapter("b:c1.xhtml", revealed_at=1, href="c1.xhtml", content_hash="h1")
        e1 = mem.add_entity("Smith", "character", revealed_at=1)
        e2 = mem.add_entity("Smith", "character", revealed_at=1)      # distinct same-name entity
        mem.add_alias(e1, "Johnny", revealed_at=1)
        mem.reextract_entity(e1, "John Smith", extractor_version="x2")
        chars = {r["entity_id"]: r["canonical_name"] for r in mem.view(1).characters()}
        assert chars[e1] == "John Smith"                             # updated in place
        assert chars[e2] == "Smith"                                  # the OTHER same-name entity untouched
        assert e1 != e2 and len(chars) == 2                          # not collapsed
        assert any(a["surface_form"] == "Johnny" for a in mem.view(1).aliases_of(e1))   # FK still resolves


def test_retract_chapter_cascades_out_of_search_and_raw(tmp_path):
    store = Store(data_dir=str(tmp_path))
    _seed(store)
    with store.book("b") as mem:
        mem.add_chapter("b:tmp.xhtml", revealed_at=7, href="tmp.xhtml", content_hash="tmp")
        mem.add_raw("b:tmp.xhtml", 7, text="orphan raw text about a river", content_hash="tmp")
        mem.add_chunk("b:tmp.xhtml", 7, "an orphan chunk about a river", [0.0, 1.0, 0.0])
        before = any("orphan chunk" in h[1] for h in mem.view(40).search([0.0, 1.0, 0.0], k=8))
        mem.retract_chapter("b:tmp.xhtml")
        after = any("orphan chunk" in h[1] for h in mem.view(40).search([0.0, 1.0, 0.0], k=8))
        assert before and not after                                  # chunk left the RAG path
        assert mem.view(40).raw_text("b:tmp.xhtml") is None          # raw text cascaded out too


def test_idempotent_reingest_is_noop(tmp_path):
    store = Store(data_dir=str(tmp_path))
    _seed(store)
    with store.book("b") as mem:
        before = len(mem._audit_all("chapters"))
        mem.add_chapter("b:c2.xhtml", revealed_at=2, href="c2.xhtml", content_hash="h2")  # unchanged
        assert len(mem._audit_all("chapters")) == before            # delta-skip: no new row


def test_chapter_is_ingested_is_bookmark_independent(tmp_path):
    """The append-once SIGNAL (ADR 0007 D-A3) is an ingestion fact, not a story read: True for a FUTURE
    chapter at this content_hash, and it is NOT a BookmarkView/funnel method."""
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")) as mem:
        with mem.transaction():
            mem.add_chapter("b:c30.xhtml", revealed_at=30, href="c30.xhtml", content_hash="h30")
            mem.mark_chapter_ingested("b:c30.xhtml", "h30")
        assert mem.chapter_is_ingested("b:c30.xhtml", "h30") is True   # future chapter, still True
        assert mem.chapter_is_ingested("b:c30.xhtml", "other") is False
    assert not hasattr(dal.BookmarkView, "chapter_is_ingested")        # never a view/funnel method


def test_legacy_chapter_row_without_completion_marker_is_not_assumed_complete(tmp_path):
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")) as mem:
        mem.add_chapter("b:c1.xhtml", revealed_at=1, href="c1.xhtml", content_hash="h1")
        assert mem.chapter_is_ingested("b:c1.xhtml", "h1") is False
        with pytest.raises(RuntimeError, match="transaction"):
            mem.mark_chapter_ingested("b:c1.xhtml", "h1")


def test_v1_book_migrates_without_promoting_legacy_chapter_rows(tmp_path):
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")) as mem:
        mem.add_chapter("b:c1.xhtml", revealed_at=1, href="c1.xhtml", content_hash="h1")
    store.close()

    raw = sqlite3.connect(store._path("b"))
    raw.execute("DROP TABLE ingested_chapters")
    raw.execute("UPDATE book_meta SET schema_version = 1")
    raw.commit()
    raw.close()

    with Store(data_dir=str(tmp_path)).book("b") as mem:
        assert "cost_pending" in mem._columns("ingested_chapters")
        assert mem.chapter_is_ingested("b:c1.xhtml", "h1") is False


def test_v2_book_migrates_identity_validity_and_participant_reveal(tmp_path):
    """The real v2 shape has neither entities.invalid_at nor participant-link reveal stamps."""
    path = tmp_path / "books" / "b" / "memory.db"
    path.parent.mkdir(parents=True)
    raw = sqlite3.connect(path)
    migrations.ensure_baseline(raw)
    raw.execute(
        "INSERT INTO book_meta(book_id,title,schema_version,created_at) VALUES (?,?,?,?)",
        ("b", "B", 1, "now"),
    )
    raw.executescript(migrations.MIGRATIONS[2])
    raw.execute("UPDATE book_meta SET schema_version=2")
    raw.execute(
        "INSERT INTO entities(book_id,canonical_name,type,revealed_at,schema_version,recorded_at) "
        "VALUES ('b','Alex','character',2,2,'now')"
    )
    entity = raw.execute("SELECT entity_id FROM entities").fetchone()[0]
    raw.execute(
        "INSERT INTO events(book_id,revealed_at,order_idx,summary,schema_version,recorded_at) "
        "VALUES ('b',2,1,'Alex arrives.',2,'now')"
    )
    event = raw.execute("SELECT event_id FROM events").fetchone()[0]
    raw.execute(
        "INSERT INTO event_participants(event_id,entity_id,book_id,role) VALUES (?,?,?,?)",
        (event, entity, "b", "subject"),
    )
    raw.commit()
    raw.close()

    with Store(str(tmp_path)).book("b") as mem:
        assert "invalid_at" in mem._columns("entities")
        assert "revealed_at" in mem._columns("event_participants")
        participant = mem._audit_all("event_participants")[0]
        assert participant["revealed_at"] == 2
        assert mem.view(1).participants_of(event) == []
        assert [row["canonical_name"] for row in mem.view(2).participants_of(event)] == ["Alex"]


def test_schema_v3_open_fails_if_the_correction_table_is_missing(tmp_path):
    store = Store(str(tmp_path))
    with store.book("b", meta={"title": "B"}):
        pass
    store.close()
    raw = sqlite3.connect(store._path("b"))
    raw.execute("DROP TABLE entity_corrections")
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError, match="entity_corrections"):
        with Store(str(tmp_path)).book("b"):
            pass


def test_standalone_commit_failure_rolls_back_the_write(tmp_path):
    class CommitFailsOnce:
        def __init__(self, conn):
            self._conn = conn
            self._armed = True

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def commit(self):
            if self._armed:
                self._armed = False
                raise RuntimeError("fault injected commit failure")
            return self._conn.commit()

    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")) as mem:
        real_conn = mem._conn
        mem.__dict__["_conn"] = CommitFailsOnce(real_conn)
        try:
            with pytest.raises(RuntimeError, match="commit failure"):
                mem.add_entity("Must Roll Back", "character", revealed_at=1)
            assert real_conn.in_transaction is False
        finally:
            mem.__dict__["_conn"] = real_conn
        assert mem._audit_all("entities") == []


def test_caught_nested_write_failure_forces_outer_transaction_rollback(tmp_path):
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")) as mem:
        with pytest.raises(RuntimeError, match="nested write failure"):
            with mem.transaction():
                try:
                    mem.add_entity(None, "character", revealed_at=1)
                except sqlite3.IntegrityError:
                    pass
                mem.add_entity("Must Also Roll Back", "character", revealed_at=1)
        assert mem._audit_all("entities") == []


def test_outer_commit_failure_rolls_back_the_whole_transaction(tmp_path):
    class CommitFailsOnce:
        def __init__(self, conn):
            self._conn = conn
            self._armed = True

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def commit(self):
            if self._armed:
                self._armed = False
                raise RuntimeError("fault injected outer commit failure")
            return self._conn.commit()

    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")) as mem:
        real_conn = mem._conn
        mem.__dict__["_conn"] = CommitFailsOnce(real_conn)
        try:
            with pytest.raises(RuntimeError, match="outer commit failure"):
                with mem.transaction():
                    mem.add_entity("Must Roll Back", "character", revealed_at=1)
            assert real_conn.in_transaction is False
        finally:
            mem.__dict__["_conn"] = real_conn
        assert mem._audit_all("entities") == []


def test_commit_and_rollback_failure_poison_the_connection(tmp_path):
    class CommitAndRollbackFail:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def commit(self):
            raise RuntimeError("fault injected commit failure")

        def rollback(self):
            raise RuntimeError("fault injected rollback failure")

    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")) as mem:
        mem.__dict__["_conn"] = CommitAndRollbackFail(mem._conn)
        with pytest.raises(RuntimeError, match="commit and rollback both failed"):
            with mem.transaction():
                mem.add_entity("Never Visible", "character", revealed_at=1)
        with pytest.raises(RuntimeError, match="connection is poisoned"):
            mem.add_entity("Rejected", "character", revealed_at=1)


# --- migrations (forward-only) --------------------------------------------------------------------
def test_migration_walk_applies_to_a_fresh_book_under_current_gt_1(tmp_path, monkeypatch):
    """The create-path fix: a brand-new book opened under CURRENT>1 gets every migration, not just the
    baseline (ADR 0007 D-A6 — the test that ADR promised)."""
    monkeypatch.setattr(migrations, "CURRENT_VERSION", 2)
    monkeypatch.setattr(migrations, "MIGRATIONS", {2: "ALTER TABLE book_meta ADD COLUMN extra_col TEXT"})
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")) as mem:
        assert "extra_col" in mem._columns("book_meta")              # v2 ran on the FRESH book
        with mem._writer():
            assert mem._conn.execute("SELECT schema_version FROM book_meta").fetchone()[0] == 2


def test_no_silent_downgrade(tmp_path):
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")):
        pass
    store.close()
    raw = sqlite3.connect(store._path("b"))                          # stamp a FUTURE schema_version
    raw.execute("UPDATE book_meta SET schema_version = 999")
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError):
        with Store(data_dir=str(tmp_path)).book("b"):
            pass


def test_end_edge_invalidates_relationship(tmp_path):
    """end_edge (no-replacement story-time invalidation, e.g. a death) hides the edge at/after `at`."""
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")) as mem:
        for n in (1, 2, 3, 4, 5):
            mem.add_chapter(f"b:c{n}.xhtml", revealed_at=n, href=f"c{n}.xhtml", content_hash=f"h{n}")
        a = mem.add_entity("A", "character", revealed_at=1)
        b = mem.add_entity("B", "character", revealed_at=1)
        e = mem.add_edge(a, b, "allegiance", "allied", revealed_at=1)
        mem.end_edge(e, at=4)
        assert any(r["label"] == "allied" for r in mem.view(3).relationships())   # visible before `at`
        assert all(r["label"] != "allied" for r in mem.view(4).relationships())   # hidden at/after `at`


# --- fail-closed FACT_TABLES negative path (ADR 0007 D-A6) ----------------------------------------
def test_open_fails_closed_on_unguarded_fact_table(tmp_path):
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")):
        pass
    store.close()
    raw = sqlite3.connect(store._path("b"))                          # inject a revealed_at table NOT in FACT_TABLES
    raw.execute("CREATE TABLE secret_facts (id INTEGER PRIMARY KEY, book_id TEXT, revealed_at INTEGER)")
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError):
        with Store(data_dir=str(tmp_path)).book("b"):
            pass


def test_fact_tables_second_loop_fires_for_revealed_at_table(tmp_path, monkeypatch):
    """Pins the SECOND loop of _assert_fact_tables_closed: a revealed_at-bearing table that is infra-
    allow-listed (so loop 1 passes) must still fail the open because it is not in FACT_TABLES."""
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")):
        pass
    store.close()
    raw = sqlite3.connect(store._path("b"))
    raw.execute("CREATE TABLE infra_with_rev (id INTEGER PRIMARY KEY, book_id TEXT, revealed_at INTEGER)")
    raw.commit()
    raw.close()
    monkeypatch.setattr(dal, "INFRA_TABLES", {"infra_with_rev"})     # pass loop 1 -> loop 2 must catch it
    with pytest.raises(RuntimeError):
        with Store(data_dir=str(tmp_path)).book("b"):
            pass


def test_open_fails_closed_on_unknown_vec0_like_shadow_table(tmp_path):
    store = Store(data_dir=str(tmp_path), vector_backend="bruteforce")
    with store.book("b", meta=dict(title="B")):
        pass
    store.close()
    raw = sqlite3.connect(store._path("b"))
    raw.execute("CREATE TABLE chunks_vec_unexpected_shadow (payload BLOB)")
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError, match="unguarded base table"):
        with Store(data_dir=str(tmp_path), vector_backend="bruteforce").book("b"):
            pass
