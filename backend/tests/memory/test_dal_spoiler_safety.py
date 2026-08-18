"""Spoiler-safety proof for the production memory store (porting the LIT-5 spike's executable proof,
ADR 0002/0007). This file covers the spoiler-frontier / referential-closure / supersession / isolation
/ no-bypass vectors; the re-extraction / retract-cascade / idempotency / migration / fail-closed
checks live in test_dal_lifecycle.py (also ported from the spike proof, ADR 0002 §6).

Written test-first (TDD): targeted app.memory.{store,dal} before they existed -> RED, then the modules
were brought into existence (safety cores lifted from the twice-reviewed spike per ADR 0007 D-A1) to
turn it GREEN. New ADR-0007 assertions (fail-closed FACT_TABLES incl book_meta + event_participants,
raw-book_meta denied, the Store whole-operation lock) are added here.
"""
import re
import sqlite3
import pytest

from app.memory.store import Store
from app.memory import dal as daltypes  # for FACT_TABLES / VALID_TIME_TABLES constants


# --------------------------------------------------------------------------- fixtures
def _build_karamazov(mem):
    """A small Brothers-Karamazov memory built through the DAL, mirroring the spike proof —
    deliberately seeds FUTURE entities/edges/state/events mis-stamped early (the referential attack)."""
    keys = {}
    for n in range(1, 7):
        k = f"karamazov:chap{n}.xhtml"
        keys[n] = k
        mem.add_chapter(k, revealed_at=n, href=f"chap{n}.xhtml", title=f"Chapter {n}", content_hash=f"h{n}")
        mem.add_raw(k, n, text=f"Raw prose of chapter {n}. " * 20, content_hash=f"h{n}")
        mem.add_summary(k, n, summary=f"v1 summary of chapter {n}.")
        mem.add_summary(k, n, summary=f"Rolling recap through chapter {n}.", kind="rolling-recap")
    k30 = "karamazov:chap30.xhtml"
    mem.add_chapter(k30, revealed_at=30, href="chap30.xhtml", title="Chapter 30", content_hash="h30")
    mem.add_raw(k30, 30, text="SPOILER raw text of the final chapter.", content_hash="h30")

    alyosha = mem.add_entity("Alexei Karamazov", "character", revealed_at=1)
    dmitri = mem.add_entity("Dmitri Karamazov", "character", revealed_at=1)
    katerina = mem.add_entity("Katerina Ivanovna", "character", revealed_at=2)
    mem.add_entity("Skotoprigonyevsk", "place", revealed_at=1)
    murderer = mem.add_entity("the true murderer", "character", revealed_at=30)   # FUTURE entity

    for a in ("Alyosha", "Alexei", "Alexei Fyodorovich", "the youngest brother"):
        mem.add_alias(alyosha, a, revealed_at=1)
    mem.add_alias(murderer, "the shadow", revealed_at=3)                          # mis-stamped early

    mem.add_edge(dmitri, alyosha, "family", "brothers", revealed_at=1)
    engaged = mem.add_edge(dmitri, katerina, "love", "engaged", revealed_at=2)
    mem.replace_edge(engaged, at=4, rel_type="rivalry", label="estranged")        # atomic, gap-free
    mem.add_edge(dmitri, murderer, "suspicion", "distrusts a figure", revealed_at=3)  # -> FUTURE entity

    s1 = mem.add_state(alyosha, revealed_at=1, status={"location": "monastery", "mood": "devout"})
    mem.replace_state(s1, at=4, status={"location": "town", "mood": "troubled"})
    mem.add_state(murderer, revealed_at=3, status={"location": "the lair"})       # mis-stamped early

    mem.add_event("Dmitri and Katerina become engaged", revealed_at=2, order_idx=1,
                  participants=[(dmitri, "subject"), (katerina, "subject")])
    shadowy = mem.add_event("A shadowy meeting", revealed_at=3, order_idx=1,
                            participants=[(dmitri, "witness"), (murderer, "subject")])
    murder_event = mem.add_event("THE MURDER IS SOLVED — spoiler", revealed_at=30, order_idx=1,
                                 participants=[(murderer, "subject")])
    rumored = mem.add_event("A rumored betrayal", revealed_at=2, order_idx=2,
                            participants=[(dmitri, "subject")])
    mem.end_event(rumored, at=4)

    mem.add_theme("faith vs doubt", "the central moral tension", revealed_at=1)

    mem.add_chunk(keys[1], 1, "Alyosha at the monastery with Father Zosima", [1.0, 0.0, 0.0])
    mem.add_chunk(keys[2], 2, "the engagement is announced", [0.6, 0.3, 0.0])
    mem.add_chunk(keys[4], 4, "a tense scene in town", [0.2, 0.8, 0.0])
    mem.add_chunk(k30, 30, "it was X who committed the murder", [0.0, 0.0, 1.0])  # spoiler chunk

    grushenka = mem.add_entity("Grushenka", "character", revealed_at=5)
    mem.add_alias(grushenka, "Grusha", revealed_at=5)

    # FUTURE chapter whose raw text is mis-stamped early (tests the live-chapter semijoin)
    mem.add_chapter("karamazov:c99.xhtml", revealed_at=40, href="c99.xhtml", content_hash="h99")
    mem.add_raw("karamazov:c99.xhtml", revealed_at=2, text="SPOILER mis-stamped raw text", content_hash="h99")

    # defense-in-depth: a stray FOREIGN-book row inside THIS file must never surface
    mem._ins("entities", book_id="OTHER_BOOK", canonical_name="Ghost from another book",
             type="character", revealed_at=1, schema_version=1, extractor_version="x",
             recorded_at=daltypes._now(), retracted_at=None)

    return dict(alyosha=alyosha, dmitri=dmitri, katerina=katerina, murderer=murderer,
                grushenka=grushenka, shadowy=shadowy, murder_event=murder_event,
                rumored=rumored, k30=k30, keys=keys)


@pytest.fixture
def store(tmp_path):
    return Store(data_dir=str(tmp_path), trace=True)


@pytest.fixture
def karamazov(store):
    meta = dict(title="The Brothers Karamazov", author="Dostoevsky", source="gutenberg", source_id="28054")
    with store.book("karamazov", meta=meta) as mem:
        ids = _build_karamazov(mem)
    return store, ids


# --------------------------------------------------------------------------- 1. spoiler block
def test_future_entity_hidden(karamazov):
    store, _ = karamazov
    with store.book("karamazov") as mem:
        chars = [r["canonical_name"] for r in mem.view(10).characters()]
    assert "the true murderer" not in chars


def test_future_event_hidden(karamazov):
    store, _ = karamazov
    with store.book("karamazov") as mem:
        assert all("MURDER IS SOLVED" not in r["summary"] for r in mem.view(10).timeline())


def test_knn_excludes_spoiler_chunk(karamazov):
    store, _ = karamazov
    with store.book("karamazov") as mem:
        hits = mem.view(10).search([0.0, 0.0, 1.0], k=3)
    assert all(h[2] <= 10 and "committed the murder" not in h[1] for h in hits)


# --------------------------------------------------------------------------- 2. referential closure
def test_referential_closure_hides_future_references(karamazov):
    store, ids = karamazov
    M = ids["murderer"]
    with store.book("karamazov") as mem:
        v10 = mem.view(10)
        assert M not in [r["dst_entity"] for r in v10.relationships()]
        assert v10.aliases_of(M) == []
        assert v10.current_state(M) is None
        assert "the true murderer" not in [r["canonical_name"] for r in v10.participants_of(ids["shadowy"])]
        # participants_of must ALSO gate the parent event
        assert v10.participants_of(ids["murder_event"]) == []
        assert mem.view(5).participants_of(ids["rumored"]) == []   # story-invalidated event


def test_references_appear_once_entity_revealed(karamazov):
    store, ids = karamazov
    M = ids["murderer"]
    with store.book("karamazov") as mem:
        v40 = mem.view(40)
        assert M in [r["dst_entity"] for r in v40.relationships()]
        assert v40.aliases_of(M) != []
        assert v40.current_state(M) is not None
        assert "the true murderer" in [r["canonical_name"] for r in v40.participants_of(ids["murder_event"])]


# --------------------------------------------------------------------------- 3. supersession
def test_supersession_atomic_and_gapfree(karamazov):
    store, _ = karamazov
    with store.book("karamazov") as mem:
        def labels(bm):
            return [r["label"] for r in mem.view(bm).relationships()]
        assert "engaged" in labels(3) and "estranged" not in labels(3)
        assert "estranged" in labels(5) and "engaged" not in labels(5)
        pair = {"engaged", "estranged"}
        assert all(len(pair & set(labels(bm))) <= 1 for bm in range(1, 7))       # never both
        assert all(len(pair & set(labels(bm))) == 1 for bm in range(2, 7))       # no gap


# --------------------------------------------------------------------------- 4. time-travel
def test_time_travel(karamazov):
    store, ids = karamazov
    with store.book("karamazov") as mem:
        assert len(mem.view(1).characters()) < len(mem.view(3).characters())
        assert mem.view(2).bio(ids["alyosha"])["state"]["location"] == "monastery"
        assert mem.view(5).bio(ids["alyosha"])["state"]["location"] == "town"


# --------------------------------------------------------------------------- 5. multi-book isolation
def test_multibook_isolation(store):
    ma = dict(title="The Brothers Karamazov", author="Dostoevsky")
    mb = dict(title="Crime and Punishment", author="Dostoevsky")
    with store.book("karamazov", meta=ma) as mem:
        _build_karamazov(mem)
    with store.book("crime", meta=mb) as mem:
        k = "crime:chap1.xhtml"
        mem.add_chapter(k, revealed_at=1, href="chap1.xhtml", title="Chapter 1", content_hash="b1")
        mem.add_entity("Raskolnikov", "character", revealed_at=1)
        mem.add_chunk(k, 1, "Raskolnikov and the murder of the pawnbroker", [0.0, 0.0, 1.0])
    with store.book("karamazov") as mem:
        assert all("pawnbroker" not in h[1] for h in mem.view(40).search([0.0, 0.0, 1.0], k=5))
        assert "Ghost from another book" not in [r["canonical_name"] for r in mem.view(40).characters()]
    with store.book("crime") as mem:
        assert any("pawnbroker" in h[1] for h in mem.view(10).search([0.0, 0.0, 1.0], k=5))


# --------------------------------------------------------------------------- 6. no-bypass / authorizer
def test_raw_select_on_fact_table_denied(karamazov):
    store, _ = karamazov
    with store.book("karamazov") as mem:
        with pytest.raises(sqlite3.DatabaseError):
            mem._conn.execute("SELECT canonical_name FROM entities").fetchall()


def test_raw_select_on_fact_table_denied_inside_outer_transaction(karamazov):
    store, _ = karamazov
    with store.book("karamazov") as mem:
        with mem.transaction():
            with pytest.raises(sqlite3.DatabaseError):
                mem._conn.execute("SELECT canonical_name FROM entities").fetchall()


def test_raw_book_meta_read_denied(karamazov):
    """ADR 0007 P2-2: book_meta has no revealed_at but MUST stay authorizer-guarded."""
    store, _ = karamazov
    with store.book("karamazov") as mem:
        with pytest.raises(sqlite3.DatabaseError):
            mem._conn.execute("SELECT embed_model FROM book_meta").fetchall()


def test_every_view_select_applies_the_filter(karamazov):
    store, ids = karamazov
    with store.book("karamazov") as mem:
        start = len(mem.executed_sql)
        v = mem.view(5)
        v.characters()
        v.relationships()
        v.timeline()
        v.themes()
        v.chapter_summaries()
        v.bio(ids["alyosha"])
        v.catch_me_up()
        v.search([1, 0, 0])
        v.raw_text(ids["keys"][1])
        v.participants_of(ids["shadowy"])
        v.aliases_of(ids["alyosha"])
        v.current_state(ids["alyosha"])
        spoiler_tables = daltypes.FACT_TABLES - {"book_meta"}
        reads = [s for s in mem.executed_sql[start:]
                 if s.lstrip().upper().startswith("SELECT") and any(t in s for t in spoiler_tables)
                 and "vector_index_meta" not in s]
    assert reads, "no view-path reads were traced"
    # Assert the OUTER funnel clause structurally, not just a 'revealed_at <=' substring (a referential
    # subquery has its own 'revealed_at<=' so the loose check would survive a dropped outer clause; pass-2
    # LOW). The trace expands bound params, and the outer _select uses SPACED operators ('book_id = ',
    # 'revealed_at <= ') whereas the _vis_entities/_live_chapters subqueries use unspaced ones
    # ('book_id=', 'revealed_at<='), so this regex matches ONLY the outer per-row clause.
    funnel_re = re.compile(r"book_id = .+? AND revealed_at <= .+? AND retracted_at IS NULL")
    vec0_re = re.compile(
        r"book_id = .+? AND revealed_at <= .+? AND retracted = 0 AND "
        r"chapter_revealed_at <= .+? AND chapter_retracted = 0"
    )
    missing = [s for s in reads if not (funnel_re.search(s) or vec0_re.search(s))]
    assert not missing, f"a view-path read is missing its outer spoiler prefilter: {missing}"


def test_view_requires_bookmark(karamazov):
    store, _ = karamazov
    with store.book("karamazov") as mem:
        with pytest.raises(TypeError):
            mem.view()
        with pytest.raises(ValueError):
            mem._select("entities", "*", bookmark=None)


# --------------------------------------------------------------------------- 7. schema / open guards
def test_inverted_validity_window_rejected(karamazov):
    store, ids = karamazov
    with store.book("karamazov") as mem:
        with pytest.raises(sqlite3.IntegrityError):
            mem.add_edge(ids["dmitri"], ids["alyosha"], "test", "bad", revealed_at=5, invalid_at=2)


def test_wrong_book_id_open_raises(store):
    with store.book("karamazov", meta=dict(title="The Brothers Karamazov")) as mem:
        _build_karamazov(mem)
    # opening the SAME file under a different book_id must raise (no silent fail-open-to-empty)
    with pytest.raises(ValueError):
        store._open_raw_as("karamazov", "WRONG_ID")


# --------------------------------------------------------------------------- 8. fail-closed FACT_TABLES (ADR 0007 D-A6)
def test_fact_tables_superset_assertion_holds(karamazov):
    """The explicit FACT_TABLES set must be a superset of every revealed_at-bearing table AND
    explicitly include book_meta and the LIT-10 correction audit."""
    store, _ = karamazov
    with store.book("karamazov") as mem:
        revealed_at_tables = {t for t in mem._base_tables() if "revealed_at" in mem._columns(t)}
        assert revealed_at_tables <= daltypes.FACT_TABLES
        assert {"book_meta", "event_participants", "entity_corrections"} <= daltypes.FACT_TABLES


# --------------------------------------------------------------------------- 9. vectors seam (ADR 0007 D-A4)
def test_vec0_canonical_recheck_fails_closed_if_the_funnel_is_bypassed(karamazov, monkeypatch):
    """The vec0 KNN prefilter is primary; its canonical funnel recheck also fails closed if broken."""
    store, _ = karamazov
    with store.book("karamazov") as mem:
        baseline = mem.view(10).search([0.0, 0.0, 1.0], k=5)
        assert all("committed the murder" not in h[1] for h in baseline)        # filter intact: no leak

        def leaky_select(table, cols, bookmark, where_extra="", params=(), order=""):
            prev = mem._engaged                                                 # TOTAL funnel-filter drop
            mem._engaged = True
            try:
                return mem._conn.execute(f"SELECT {cols} FROM {table} WHERE book_id = ?",
                                         [mem._book_id]).fetchall()
            finally:
                mem._engaged = prev
        monkeypatch.setattr(mem, "_select", leaky_select)
        with pytest.raises(RuntimeError, match="canonical spoiler filters"):
            mem.view(10).search([0.0, 0.0, 1.0], k=5)


def test_raw_chunks_select_denied(karamazov):
    """D-A4 conjunct 2: even a rogue ranker that tried to read chunks directly is authorizer-DENIED."""
    store, _ = karamazov
    with store.book("karamazov") as mem:
        with pytest.raises(sqlite3.DatabaseError):
            mem._conn.execute("SELECT text FROM chunks").fetchall()


# --------------------------------------------------------------------------- 10. dropped-guard restoration
def test_raw_text_future_parent_mis_stamped_hidden(karamazov):
    """Direct guard for ADR 0002 pass-2 fix #2: a raw_chapters row whose OWN revealed_at<=bookmark but
    whose parent chapter is in the future is hidden by the live-chapter semijoin (the c99 attack seed)."""
    store, ids = karamazov
    with store.book("karamazov") as mem:
        v = mem.view(10)
        assert v.raw_text("karamazov:c99.xhtml") is None            # future-parent mis-stamped -> hidden
        assert v.raw_text(ids["keys"][1]) is not None               # a genuinely-read chapter -> present


def test_bio_excludes_future_and_invalidated_events(karamazov):
    """bio()->events_for() must gate event visibility: a story-invalidated event the entity is in does
    not surface, while a genuinely-visible one does (the EXISTS event-visibility gate, behaviourally)."""
    store, ids = karamazov
    with store.book("karamazov") as mem:
        ev = mem.view(10).bio(ids["dmitri"])["appears_in_events"]
        assert ids["rumored"] not in ev                             # invalidated@4 -> hidden at bm 10
        assert ids["shadowy"] in ev                                 # visible event the entity is in


def test_non_integer_bookmark_raises_not_leaks(tmp_path):
    """ADR 0007 pass-2 BLOCKER: a non-int bookmark must FAIL CLOSED, never leak. A TEXT bookmark on the
    INTEGER-affinity revealed_at column would otherwise make `revealed_at <= ?` true for every row."""
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")) as mem:
        mem.add_entity("future char", "character", revealed_at=99)
        for bad in ("", "abc", "2x", 1.5, True, None):
            with pytest.raises((ValueError, TypeError)):
                mem.view(bad).characters()                          # must raise at view() or _select
        assert mem.view(0).characters() == []                      # valid int 0 = nothing revealed yet
        assert [r["canonical_name"] for r in mem.view(99).characters()] == ["future char"]


def test_search_excludes_chunk_on_future_chapter(tmp_path):
    """Isolates the search-path live-chapter semijoin from the per-row revealed_at filter: a chunk whose
    OWN revealed_at<=bookmark but whose parent chapter is FUTURE must still be excluded from KNN."""
    store = Store(data_dir=str(tmp_path))
    with store.book("b", meta=dict(title="B")) as mem:
        mem.add_chapter("b:c1.xhtml", revealed_at=1, href="c1.xhtml", content_hash="h1")
        mem.add_chapter("b:future.xhtml", revealed_at=40, href="future.xhtml", content_hash="hf")
        mem.add_chunk("b:c1.xhtml", 1, "safe chunk", [1.0, 0.0, 0.0])
        mem.add_chunk("b:future.xhtml", 2, "SPOILER mis-stamped chunk", [0.0, 0.0, 1.0])  # rev<=bm, future parent
        hits = mem.view(10).search([0.0, 0.0, 1.0], k=5)
    assert all("mis-stamped" not in h[1] for h in hits), "live-chapter semijoin must exclude it"
