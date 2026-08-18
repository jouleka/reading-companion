#!/usr/bin/env python3
"""LIT-5 spike — worked examples + executable proof of every exit criterion.  [rev 2]

Run:  python3 spikes/lit-5-schema/demo.py
Exits non-zero if any guarantee fails. Stdlib only.

Rev 2 adds the proofs demanded by the adversarial review:
  - REFERENTIAL CLOSURE: a visible row referencing a future entity does NOT leak it.
  - PER-CONNECTION guard: a writer on book B cannot unlock raw reads of book A.
  - RAW-TEXT read path (LIT-19) through the funnel, future text hidden.
  - RETRACTION CASCADE: a retracted chapter's chunks/summaries disappear.
  - NO DOUBLE-VISION: re-extracting an entity/recap leaves exactly one live row.
  - SCHEMA GUARDS: CHECK rejects inverted windows; wrong book_id on open raises.
  - DOCUMENTED BOUNDARY: a 2nd raw connection can read (the honest limit).
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dal  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def build_book_a(path):
    """A small Brothers-Karamazov-flavoured memory, built entirely through the DAL."""
    db = dal.MemoryDB(path, "karamazov", title="The Brothers Karamazov",
                      author="Dostoevsky", source="gutenberg", source_id="28054")
    keys = {}
    for n in range(1, 7):
        k = f"karamazov:chap{n}.xhtml"
        keys[n] = k
        db.add_chapter(k, revealed_at=n, href=f"chap{n}.xhtml", title=f"Chapter {n}", content_hash=f"h{n}")
        db.add_raw(k, n, text=f"Raw prose of chapter {n}. " * 20, content_hash=f"h{n}")
        db.add_summary(k, n, summary=f"v1 summary of chapter {n}.")
        db.add_summary(k, n, summary=f"Rolling recap through chapter {n}.", kind="rolling-recap")
    k30 = "karamazov:chap30.xhtml"
    db.add_chapter(k30, revealed_at=30, href="chap30.xhtml", title="Chapter 30", content_hash="h30")
    db.add_raw(k30, 30, text="SPOILER raw text of the final chapter.", content_hash="h30")

    alyosha = db.add_entity("Alexei Karamazov", "character", revealed_at=1)
    dmitri = db.add_entity("Dmitri Karamazov", "character", revealed_at=1)
    katerina = db.add_entity("Katerina Ivanovna", "character", revealed_at=2)
    db.add_entity("Skotoprigonyevsk", "place", revealed_at=1)
    murderer = db.add_entity("the true murderer", "character", revealed_at=30)   # FUTURE entity

    for a in ("Alyosha", "Alexei", "Alexei Fyodorovich", "the youngest brother"):
        db.add_alias(alyosha, a, revealed_at=1)
    # an alias attached to the FUTURE entity, but mis-stamped early (the referential attack)
    db.add_alias(murderer, "the shadow", revealed_at=3)

    db.add_edge(dmitri, alyosha, "family", "brothers", revealed_at=1)
    engaged = db.add_edge(dmitri, katerina, "love", "engaged", revealed_at=2)
    db.replace_edge(engaged, at=4, rel_type="rivalry", label="estranged")   # atomic, gap-free
    # an edge from a VISIBLE entity to the FUTURE entity (the referential-leak blocker)
    db.add_edge(dmitri, murderer, "suspicion", "distrusts a mysterious figure", revealed_at=3)

    s1 = db.add_state(alyosha, revealed_at=1, status={"location": "monastery", "mood": "devout"})
    db.replace_state(s1, at=4, status={"location": "town", "mood": "troubled"})
    # state for the FUTURE entity, mis-stamped early
    db.add_state(murderer, revealed_at=3, status={"location": "the lair"})

    db.add_event("Dmitri and Katerina become engaged", revealed_at=2, order_idx=1,
                 participants=[(dmitri, "subject"), (katerina, "subject")])
    # an event visible early whose participants include BOTH a visible and a FUTURE entity
    shadowy = db.add_event("A shadowy meeting", revealed_at=3, order_idx=1,
                           participants=[(dmitri, "witness"), (murderer, "subject")])
    # a FUTURE event (its cast must not leak via participants_of)
    murder_event = db.add_event("THE MURDER IS SOLVED — spoiler", revealed_at=30, order_idx=1,
                                participants=[(murderer, "subject")])
    # an event later story-INVALIDATED (a rumour disproved) — its cast must not leak either
    rumored = db.add_event("A rumored betrayal", revealed_at=2, order_idx=2,
                           participants=[(dmitri, "subject")])
    db.end_event(rumored, at=4)

    db.add_theme("faith vs doubt", "the central moral tension", revealed_at=1)

    db.add_chunk(keys[1], 1, "Alyosha at the monastery with Father Zosima", [1.0, 0.0, 0.0])
    db.add_chunk(keys[2], 2, "the engagement is announced", [0.6, 0.3, 0.0])
    db.add_chunk(keys[4], 4, "a tense scene in town", [0.2, 0.8, 0.0])
    db.add_chunk(k30, 30, "it was X who committed the murder", [0.0, 0.0, 1.0])   # spoiler chunk

    # a fresh entity (with an alias = sub-graph) for the re-extraction test
    grushenka = db.add_entity("Grushenka", "character", revealed_at=5)
    db.add_alias(grushenka, "Grusha", revealed_at=5)

    # a FUTURE chapter whose raw text is mis-stamped early (tests the live-chapter semijoin)
    db.add_chapter("karamazov:c99.xhtml", revealed_at=40, href="c99.xhtml", content_hash="h99")
    db.add_raw("karamazov:c99.xhtml", revealed_at=2, text="SPOILER mis-stamped raw text", content_hash="h99")

    # defense-in-depth: a stray FOREIGN-book row inside THIS file must never surface
    db._ins("entities", book_id="OTHER_BOOK", canonical_name="Ghost from another book",
            type="character", revealed_at=1, schema_version=1, extractor_version="x",
            recorded_at=dal._now(), retracted_at=None)

    ids = dict(alyosha=alyosha, dmitri=dmitri, katerina=katerina, murderer=murderer,
               grushenka=grushenka, shadowy=shadowy, murder_event=murder_event,
               rumored=rumored, engaged=engaged, k30=k30, keys=keys)
    return db, ids


def build_book_b(path):
    db = dal.MemoryDB(path, "crime-and-punishment", title="Crime and Punishment",
                      author="Dostoevsky", source="gutenberg", source_id="2554")
    k = "crime-and-punishment:chap1.xhtml"
    db.add_chapter(k, revealed_at=1, href="chap1.xhtml", title="Chapter 1", content_hash="b1")
    db.add_entity("Raskolnikov", "character", revealed_at=1)
    db.add_chunk(k, 1, "Raskolnikov and the murder of the pawnbroker", [0.0, 0.0, 1.0])
    return db


def main():
    tmp = tempfile.mkdtemp(prefix="lit5_")
    path_a = os.path.join(tmp, "karamazov.db")
    path_b = os.path.join(tmp, "crime.db")
    db, ids = build_book_a(path_a)
    db_b = build_book_b(path_b)
    M = ids["murderer"]

    print("LIT-5 schema + DAL — executable proof (rev 2)\n" + "=" * 64)

    # --- 1. SPOILER BLOCK ---------------------------------------------------
    print("\n1. Spoiler block (revealed_at > bookmark is invisible)")
    v10 = db.view(bookmark=10)
    chars = [r["canonical_name"] for r in v10.characters()]
    check("future entity hidden", "the true murderer" not in chars, f"cast@10 = {chars}")
    check("future event hidden", all("MURDER IS SOLVED" not in r["summary"] for r in v10.timeline()))
    hits = v10.search([0.0, 0.0, 1.0], k=3)
    check("KNN excludes the spoiler chunk",
          all(h[2] <= 10 and "committed the murder" not in h[1] for h in hits),
          f"top hit @10 = {hits[0][1]!r} (rev {hits[0][2]})" if hits else "no hits")

    # --- 2. REFERENTIAL CLOSURE (the blocker found in review) ---------------
    print("\n2. Referential closure — a visible row may NOT surface a future entity")
    rel_dsts = [r["dst_entity"] for r in v10.relationships()]
    check("edge to a future entity is hidden", M not in rel_dsts,
          f"'distrusts' edge present? {'distrusts a mysterious figure' in [r['label'] for r in v10.relationships()]}")
    check("alias of a future entity is hidden", v10.aliases_of(M) == [])
    check("state of a future entity is hidden", v10.current_state(M) is None)
    parts10 = [r["canonical_name"] for r in v10.participants_of(ids["shadowy"])]
    check("future participant filtered from a visible event", "the true murderer" not in parts10,
          f"participants of shadowy meeting @10 = {parts10}")
    # participants_of must ALSO gate the parent event (the rev-2 residual leak)
    check("cast of a FUTURE event is hidden (event-visibility gate)",
          v10.participants_of(ids["murder_event"]) == [])
    check("cast of a story-INVALIDATED event is hidden",
          db.view(5).participants_of(ids["rumored"]) == [])
    # ...and it all correctly APPEARS once the entity is actually revealed (ch40)
    v40 = db.view(bookmark=40)
    check("the same references DO appear after the entity is revealed",
          M in [r["dst_entity"] for r in v40.relationships()]
          and v40.aliases_of(M) and v40.current_state(M) is not None
          and "the true murderer" in [r["canonical_name"] for r in v40.participants_of(ids["shadowy"])]
          and "the true murderer" in [r["canonical_name"] for r in v40.participants_of(ids["murder_event"])])

    # --- 3. SUPERSESSION (valid-time), atomic & gap-free -------------------
    print("\n3. Supersession — engaged -> estranged, never both, no gap")
    def rel_labels(bm):
        return [r["label"] for r in db.view(bm).relationships()]
    check("engaged visible @3", "engaged" in rel_labels(3) and "estranged" not in rel_labels(3),
          f"@3 = {rel_labels(3)}")
    check("estranged visible @5", "estranged" in rel_labels(5) and "engaged" not in rel_labels(5),
          f"@5 = {rel_labels(5)}")
    pair = {"engaged", "estranged"}
    check("never both at any bookmark", all(len(pair & set(rel_labels(bm))) <= 1 for bm in range(1, 7)))
    check("no validity GAP across the transition",
          all(len(pair & set(rel_labels(bm))) == 1 for bm in range(2, 7)),
          "the couple-relationship is live (one value) at every bookmark >= 2")

    # --- 4. TIME-TRAVEL -----------------------------------------------------
    print("\n4. Time-travel (same store, different as-of bookmark)")
    check("cast grows as you read", len(db.view(1).characters()) < len(db.view(3).characters()),
          f"chars@1={len(db.view(1).characters())}, chars@3={len(db.view(3).characters())}")
    e2 = db.view(2).bio(ids["alyosha"])["state"]["location"]
    e5 = db.view(5).bio(ids["alyosha"])["state"]["location"]
    check("Alyosha state time-travels", e2 == "monastery" and e5 == "town", f"@2={e2}, @5={e5}")

    # --- 5. MULTI-BOOK ISOLATION (LIT-18) ----------------------------------
    print("\n5. Multi-book isolation (per-file + book_id hook + per-connection guard)")
    a_hits = db.view(40).search([0.0, 0.0, 1.0], k=5)
    check("book A KNN never returns book B text", all("pawnbroker" not in h[1] for h in a_hits))
    check("book B KNN returns book B",
          any("pawnbroker" in h[1] for h in db_b.view(10).search([0.0, 0.0, 1.0], k=5)))
    check("foreign book_id row inside the file is filtered out",
          "Ghost from another book" not in [r["canonical_name"] for r in db.view(40).characters()])
    denied_cross = False
    with db_b._writer():                      # a write txn open on book B...
        try:
            db._conn.execute("SELECT canonical_name FROM entities").fetchall()   # ...raw read of A
        except sqlite3.DatabaseError:
            denied_cross = True
    check("a writer on book B does NOT unlock raw reads of book A (per-connection guard)", denied_cross)

    # --- 6. RE-EXTRACTION (transaction-time, LIT-19) -----------------------
    print("\n6. Re-extraction — supersede, no double-vision, history kept, idempotent, cascade")
    k3 = ids["keys"][3]
    db.reextract_summary(k3, revealed_at=3, new_summary="v2 BETTER summary of chapter 3.")
    summ = {r["chapter_key"]: r["summary"] for r in db.view(5).chapter_summaries()}
    check("current read shows v2 summary", summ[k3] == "v2 BETTER summary of chapter 3.")
    check("exactly one live summary for ch3",
          sum(1 for r in db.view(5).chapter_summaries() if r["chapter_key"] == k3) == 1)
    audit = [r["summary"] for r in db._audit_all("chapter_summaries")
             if r["chapter_key"] == k3 and r["kind"] == "chapter"]
    check("history auditable (v1 retained, retracted)", len(audit) == 2, f"{audit}")
    # re-extract a SPECIFIC entity in place: one row, new name, stable id, sub-graph intact
    db.reextract_entity(ids["grushenka"], "Agrafena Svetlova", extractor_version="x2")
    g_rows = [r for r in db.view(6).characters() if r["entity_id"] == ids["grushenka"]]
    check("re-extract updates in place (one row, new name, stable id — no double-vision)",
          len(g_rows) == 1 and g_rows[0]["canonical_name"] == "Agrafena Svetlova")
    check("re-extracted entity keeps its sub-graph (alias FK still resolves)",
          any(a["surface_form"] == "Grusha" for a in db.view(6).aliases_of(ids["grushenka"])))
    # raw text is reachable through the funnel for re-derivation; future/mis-stamped text hidden
    check("raw text available for re-extraction (LIT-19 loop)", db.view(5).raw_text(k3) is not None)
    check("future chapter raw text hidden even if mis-stamped early (live-chapter semijoin)",
          db.view(5).raw_text("karamazov:c99.xhtml") is None)
    # idempotent unchanged re-ingest
    n_before = len(db._audit_all("chapters"))
    db.add_chapter(k3, revealed_at=3, href="chap3.xhtml", title="Chapter 3", content_hash="h3")
    check("re-ingest of unchanged chapter is a no-op (idempotent)",
          len(db._audit_all("chapters")) == n_before)
    # retraction cascade: a retracted chapter's chunks vanish from search
    db.add_chapter("karamazov:tmp.xhtml", revealed_at=7, href="tmp.xhtml", content_hash="tmp")
    db.add_chunk("karamazov:tmp.xhtml", 7, "temporary orphan chunk about a river", [0.0, 1.0, 0.0])
    before_cascade = any("orphan chunk" in h[1] for h in db.view(40).search([0.0, 1.0, 0.0], k=8))
    db.retract_chapter("karamazov:tmp.xhtml")
    after_cascade = any("orphan chunk" in h[1] for h in db.view(40).search([0.0, 1.0, 0.0], k=8))
    check("retracting a chapter cascades: its chunk leaves the RAG path",
          before_cascade and not after_cascade)

    # --- 7. SCHEMA GUARDS ---------------------------------------------------
    print("\n7. Schema guards")
    rej = False
    try:
        db.add_edge(ids["dmitri"], ids["alyosha"], "test", "bad", revealed_at=5, invalid_at=2)
    except sqlite3.IntegrityError:
        rej = True
    check("CHECK rejects an inverted validity window (invalid_at <= revealed_at)", rej)
    mismatch = False
    try:
        dal.MemoryDB(path_a, "WRONG_ID", create=False)
    except ValueError:
        mismatch = True
    check("opening with the wrong book_id raises (no silent fail-open-to-empty)", mismatch)

    # --- 8. NO-BYPASS -------------------------------------------------------
    print("\n8. No-bypass (the structural guarantee)")
    denied = False
    try:
        db._conn.execute("SELECT canonical_name FROM entities").fetchall()
    except sqlite3.DatabaseError:
        denied = True
    check("raw 'SELECT FROM entities' on the DAL's connection is DENIED", denied)
    start = len(db.executed_sql)
    vv = db.view(5)
    vv.characters(); vv.relationships(); vv.timeline(); vv.themes(); vv.chapter_summaries()
    vv.bio(ids["alyosha"]); vv.catch_me_up(); vv.search([1, 0, 0]); vv.raw_text(ids["keys"][1])
    vv.participants_of(ids["shadowy"]); vv.aliases_of(ids["alyosha"]); vv.current_state(ids["alyosha"])
    # book_meta is per-book METADATA (no revealed_at, no spoiler content) — exclude it from the
    # spoiler-frontier assertion (it is still authorizer-protected; LIT-20's pinned_identity reads it).
    spoiler_tables = dal.FACT_TABLES - {"book_meta"}
    reads = [s for s in db.executed_sql[start:]
             if s.lstrip().upper().startswith("SELECT") and any(t in s for t in spoiler_tables)]
    check("every view-path SELECT applies 'revealed_at <=' (spoiler frontier)",
          all("revealed_at <=" in s or "revealed_at<=" in s for s in reads),
          f"{len(reads)} reads checked")
    check("every view-path SELECT applies 'book_id =' (multi-book isolation)",
          all("book_id =" in s or "book_id=" in s for s in reads))
    no_bm = False
    try:
        db.view()  # missing required arg
    except TypeError:
        no_bm = True
    check("view() requires a bookmark argument", no_bm)
    val = False
    try:
        db._select("entities", "*", bookmark=None)
    except ValueError:
        val = True
    check("the filter funnel rejects bookmark=None", val)
    # documented honest boundary: a SEPARATE raw connection has no authorizer
    raw2 = sqlite3.connect(path_a)
    leaked = raw2.execute("SELECT canonical_name FROM entities").fetchall()
    raw2.close()
    check("DOCUMENTED BOUNDARY: a 2nd connection (no authorizer) CAN read — the app owns the only connection",
          len(leaked) > 0, "this is the honest limit, not a product leak (see ADR threat model)")

    # --- summary ------------------------------------------------------------
    print("\n" + "=" * 64)
    if FAILS:
        print(f"RESULT: {len(FAILS)} CHECK(S) FAILED -> {FAILS}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
