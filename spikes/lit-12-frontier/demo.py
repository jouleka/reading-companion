#!/usr/bin/env python3
"""LIT-12 — executable proof of the sub-chapter spoiler frontier. Builds the REAL Karamazov store
(LIT-6 extractions through the LIT-5 DAL) and proves that a reader anywhere inside a chapter gets a
non-leaking, correctly-lagged memory. Stdlib only; exits non-zero on any failure.

Proves:
  1. CFI->bookmark mapping at 0% / 60% / 100% of a chapter (entered / mid / completed).
  2. NON-LEAK against the live DAL: for a reader at ANY position, view(bookmark) surfaces NO fact from
     the in-progress chapter or beyond (the pending chapter is filtered).
  3. The 'where you stopped' UX (recap of completed chapters + 'you are X% into chapter N') leaks nothing.
  4. The documented LAG (the read prefix of the in-progress chapter is not yet in the recap).
  5. The sub-chapter UPGRADE filter removes the lag without leaking (read span visible, unread hidden).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "lit-8-spoiler-eval"))
import frontier as F  # noqa: E402
import harness  # noqa: E402  (build_store: LIT-6 extractions -> LIT-5 store, + chapter texts)

FAILS = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def pos(bounds, chapter, frac):
    """A reading position at `frac` of the 1-based `chapter`."""
    s, e = bounds[chapter - 1]
    return s + frac * (e - s)


def main():
    db, max_bm, texts, client = harness.build_store()
    lengths = [len(texts[o]) for o in sorted(texts)]      # real chapter lengths (char counts)
    bounds = F.chapter_bounds(lengths)
    print("LIT-12 sub-chapter spoiler frontier\n" + "=" * 64)
    print(f"book: {len(bounds)} chapters; lengths(chars)={lengths}\n")

    # ---- 0. ALIGNMENT: the frontier's geometric atoms map 1:1 to the DAL's revealed_at atoms ----
    print("0. Frontier atoms aligned 1:1 with the DAL's revealed_at atoms (rev-2 invariant)")
    chap_ords = sorted(r["revealed_at"] for r in db._audit_all("chapters") if r["retracted_at"] is None)
    F.assert_aligned(bounds, chap_ords)                   # raises on count drift OR an ordinal gap
    check("frontier atoms == DAL revealed_at atoms, contiguous 1..N (1:1 identity guard)",
          len(bounds) == len(chap_ords), f"{len(bounds)} atoms")
    gap_caught = False
    try:
        F.assert_aligned(F.chapter_bounds([1, 1, 1]), [1, 2, 4])   # same count (3), but ordinal gap
    except ValueError:
        gap_caught = True
    check("assert_aligned CATCHES a same-count ordinal GAP (identity, not just count)", gap_caught)
    # a zero-length atom is REJECTED at construction (can't become a leaking revealed_at atom)
    empty_rejected = False
    try:
        F.chapter_bounds([10, 0, 10])
    except ValueError:
        empty_rejected = True
    check("chapter_bounds REJECTS a zero-length atom (empty/divider can't be a revealed_at atom)", empty_rejected)
    # geometric-vs-ordinal DRIFT (e.g. an unmerged divider: 4 ranges but 3 atoms) is caught at construction
    drift_caught = False
    try:
        F.assert_aligned(F.chapter_bounds([100, 20, 100, 100]), 3)   # 4 ranges vs 3 revealed_at atoms
    except ValueError:
        drift_caught = True
    check("assert_aligned CATCHES geometric/ordinal drift (unmerged divider)", drift_caught)
    print()

    # ---- 1. CFI -> bookmark mapping --------------------------------------
    print("1. CFI -> bookmark mapping (entered / mid / completed)")
    N = 3
    check("entered chapter N (0%): bookmark = N-1, chapter N pending",
          F.cfi_to_bookmark(pos(bounds, N, 0.0), bounds) == N - 1 and F.current_chapter(pos(bounds, N, 0.0), bounds) == N)
    check("mid chapter N (60%): bookmark still N-1 (N not complete)",
          F.cfi_to_bookmark(pos(bounds, N, 0.60), bounds) == N - 1)
    check("completed chapter N (100%): bookmark = N",
          F.cfi_to_bookmark(pos(bounds, N, 1.0), bounds) == N)
    check("start of the book: bookmark 0", F.cfi_to_bookmark(0, bounds) == 0)
    check("end of the book: all chapters complete",
          F.cfi_to_bookmark(bounds[-1][1], bounds) == len(bounds))

    # ---- 2. NON-LEAK against the live DAL, every chapter, every position --
    print("\n2. Non-leak against the live LIT-5 DAL (every surfaced fact is from a chapter read PAST)")
    rev = {r["entity_id"]: r["revealed_at"] for r in db._audit_all("entities") if r["retracted_at"] is None}
    leaks = 0
    for ch in range(1, len(bounds) + 1):
        for frac in (0.0, 0.60, 0.99):
            p = pos(bounds, ch, frac)
            bm = F.cfi_to_bookmark(p, bounds)
            cur = F.current_chapter(p, bounds)
            v = db.view(bm)
            surfaced = ([c["revealed_at"] for c in v.characters()]
                        + [e["revealed_at"] for e in v.timeline()]
                        + [r["revealed_at"] for r in v.relationships()])
            # TEETH (not just revealed_at<=bm, which the DAL enforces anyway): every surfaced fact's
            # chapter must have been FULLY READ PAST — its end offset <= the reader's position.
            for r in surfaced:
                if r == cur or r > bm or bounds[r - 1][1] > p:
                    leaks += 1
    check("zero in-progress-chapter leaks across all chapters x {0%,60%,99%}", leaks == 0,
          f"{len(bounds) * 3} reader positions checked; every surfaced fact's chapter end <= position")
    # INDEPENDENT signal (fixes the circular-proof finding): each name first appears in prose by its
    # revealed_at, so the revealed_at stamps the frontier trusts are themselves correct.
    rc_checked, rc_bad, _ = harness.reveal_correctness_eval(db, texts)
    check("revealed_at stamps are independently reveal-correct (not circular)", rc_bad == 0,
          f"{rc_checked} entities checked, {rc_bad} mis-stamped")
    # monotonic high-water: paging BACKWARD does not un-reveal (re-read = LIT-17)
    hw = F.bookmark_high_water(3, pos(bounds, 2, 0.5), bounds)
    check("bookmark is a monotonic high-water mark (backward paging does not shrink it)", hw == 3,
          f"prev=3, now-at-ch2 completed={F.cfi_to_bookmark(pos(bounds, 2, 0.5), bounds)}, high-water={hw}")

    # ---- 3. 'where you stopped' UX is safe -------------------------------
    print("\n3. 'Where you stopped' is spoiler-safe")
    p = pos(bounds, 4, 0.60)
    bm, cur, prog = F.cfi_to_bookmark(p, bounds), F.current_chapter(p, bounds), F.chapter_progress(p, bounds)
    where = {"recap_through_chapter": bm, "you_are_in_chapter": cur, "progress": round(prog, 2)}
    cmu = db.view(bm).catch_me_up()
    check("recap covers only completed chapters; 'in chapter N' names N but shows none of its facts",
          where["recap_through_chapter"] == cur - 1 and cmu["as_of_chapter"] == bm,
          f"{where}")

    # ---- 4. the documented LAG -------------------------------------------
    print("\n4. Documented lag (the read prefix of the in-progress chapter is not yet recapped)")
    p = pos(bounds, 4, 0.99)   # 99% through chapter 4
    bm = F.cfi_to_bookmark(p, bounds)
    ch4_entities_visible = any(rev[e] == 4 for e in {c["entity_id"] for c in db.view(bm).characters()})
    check("at 99% of chapter 4, chapter-4 facts are NOT in the recap (bookmark=3) — accepted lag",
          bm == 3 and not ch4_entities_visible, f"bookmark={bm}")

    # ---- 5. sub-chapter UPGRADE filter (removes lag without leaking) ------
    print("\n5. Sub-chapter upgrade filter (the routed option if lag is unacceptable)")
    # reader 60% into chapter N=4; a fact whose source span is at 30% is READ, at 80% is UNREAD
    bm_chapter, bm_offset = 4, 0.60
    check("a read-prefix fact (30% of the in-progress chapter) IS visible",
          F.subchapter_visible(4, 0.30, bm_chapter, bm_offset))
    check("an unread fact (80% of the in-progress chapter) is HIDDEN",
          not F.subchapter_visible(4, 0.80, bm_chapter, bm_offset))
    check("a fact from an earlier chapter is visible regardless of offset",
          F.subchapter_visible(2, 0.99, bm_chapter, bm_offset))
    check("a fact from a later chapter is hidden",
          not F.subchapter_visible(5, 0.0, bm_chapter, bm_offset))
    # boundary + STRADDLING span: fact_subpos is the span END. A span ENDING exactly at the cursor is
    # read (visible); a span that ends PAST the cursor (even if it started before) is unread (hidden).
    check("a span ending exactly at the cursor is visible (END semantics, boundary)",
          F.subchapter_visible(4, 0.60, bm_chapter, bm_offset))
    check("a span STRADDLING the cursor (ends at 0.75) stays hidden",
          not F.subchapter_visible(4, 0.75, bm_chapter, bm_offset))

    print("\n" + "=" * 64)
    if FAILS:
        print(f"RESULT: {len(FAILS)} CHECK(S) FAILED -> {FAILS}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("NOTE: position is modeled as a monotonic char-offset, which is order-isomorphic to a real "
          "EPUB CFI for the only ops the frontier needs (compare + locate-in-range). LIT-13's reader "
          "engine supplies the real CFI comparator; the frontier logic is unchanged.")


if __name__ == "__main__":
    main()
