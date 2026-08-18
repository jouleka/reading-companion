#!/usr/bin/env python3
"""LIT-12 — the sub-chapter spoiler frontier: map the reader's continuous position to the INTEGER
bookmark the LIT-5 DAL consumes, so the in-progress chapter can never leak its unread remainder.

THE SEAM: the reader engine (LIT-13) yields a fine-grained CFI, usually MID-chapter; memory is stamped
per WHOLE chapter (revealed_at = chapter ordinal). Naively treating "entered chapter N" as "chapter N
is in the recap" leaks the END of N (which the reader hasn't reached). Naively never advancing lags.

DECISION (the safe, simple frontier): the spoiler-filter bookmark = the number of chapters the reader
has FULLY completed; the chapter currently under the CFI is held PENDING (its facts, stamped
revealed_at = its ordinal, are filtered out because bookmark < that ordinal). Provably non-leaking;
accepts a bounded LAG (the just-read prefix of the current chapter isn't in the recap yet). A
sub-chapter upgrade (below) removes the lag if it ever proves unacceptable.

POSITION MODEL: a CFI imposes a TOTAL ORDER on positions in spine order. The frontier needs only two
operations on it — compare two positions, and locate a position within a chapter's [start,end) range.
We model position as a monotonic offset (e.g. cumulative char count), which is ORDER-ISOMORPHIC to CFI
for these operations; the real reader (LIT-13) supplies the CFI comparator, the logic here is identical.
Stdlib only.
"""


def chapter_bounds(chapter_lengths):
    """Cumulative [start, end) offset per chapter ATOM, in revealed_at order.

    PRECONDITION (rev-2, from adversarial review): `chapter_lengths` must be the DAL's INCLUDED,
    POST-MERGE chapter atoms — one entry per `revealed_at` ordinal (1..N), so geometric index i ↔
    revealed_at i+1 exactly. A zero-length atom is REJECTED: it would be counted 'complete' at its
    degenerate boundary (end==start) yet never be the in-progress chapter, leaking its stamped facts
    with nothing read. The ADR-0001 divider-merge must run BEFORE this (a <200w/empty label-only
    divider folds into the next chapter as a grouping attribute, not its own revealed_at). Use
    assert_aligned() to tie len(bounds) to max(revealed_at)."""
    bounds = []
    acc = 0
    for i, ln in enumerate(chapter_lengths, start=1):
        if ln <= 0:
            raise ValueError(f"chapter atom {i} has length {ln}: a zero-length atom cannot be a "
                             f"revealed_at atom (merge dividers per ADR 0001 before building bounds)")
        bounds.append((acc, acc + ln))
        acc += ln
    return bounds


def assert_aligned(bounds, revealed_ats):
    """Guard the rev-2 invariant: the frontier's geometric atoms map 1:1 onto the DAL's revealed_at
    ordinals — a TRUE identity check, not just a count. `revealed_ats` is the list of `revealed_at`
    values of the included chapter atoms; this asserts both `len(bounds)==len(revealed_ats)` AND that
    they are exactly the contiguous ordinals 1..N. A count-only guard would pass a same-count ordinal
    GAP (e.g. {1,2,4}) — which under-reveals (lag) rather than leaks, but LIT-13 is told this is a 1:1
    guarantee, so the contiguity check belongs in the reusable guard, not only the demo. (An int is
    still accepted for the count-only legacy form.)"""
    n = revealed_ats if isinstance(revealed_ats, int) else len(revealed_ats)
    if len(bounds) != n:
        raise ValueError(f"frontier/DAL atom mismatch: {len(bounds)} geometric chapter ranges vs "
                         f"{n} revealed_at atoms — divider merge / segmentation must align them 1:1")
    if not isinstance(revealed_ats, int) and sorted(revealed_ats) != list(range(1, n + 1)):
        raise ValueError(f"revealed_at ordinals are not the contiguous 1..{n}: {sorted(revealed_ats)} "
                         f"— a gap/permutation breaks the geometric↔ordinal identity the frontier needs")


def cfi_to_bookmark(position, bounds):
    """THE mapping. Returns the spoiler-frontier bookmark = the count of chapters FULLY completed
    (end <= position). The chapter containing `position` is in-progress -> NOT counted -> pending.
    position at the very start of the book -> 0 (nothing complete); at/after the last chapter's end
    -> all chapters complete."""
    return sum(1 for (_s, e) in bounds if e <= position)


def bookmark_high_water(prev_bookmark, position, bounds):
    """The persisted bookmark is MONOTONIC NON-DECREASING — a high-water mark of the furthest chapter
    completed. Paging BACKWARD must not un-reveal facts the reader has already read (that would be
    needless lag, and re-read/rewind semantics are owned by LIT-17). So the stored frontier =
    max(prev, completed-now). The instantaneous `cfi_to_bookmark` is forward-safe on its own; this is
    the contract the reader (LIT-13) persists."""
    return max(prev_bookmark, cfi_to_bookmark(position, bounds))


def current_chapter(position, bounds):
    """1-based ordinal of the in-progress (pending) chapter — the one whose [start,end) contains
    `position`. Returns len(bounds)+1 once the reader is at/after the book's end (nothing pending)."""
    for i, (s, e) in enumerate(bounds, start=1):
        if s <= position < e:
            return i
    return len(bounds) + 1


def chapter_progress(position, bounds):
    """Fraction [0,1] read of the in-progress chapter (for the 'where you stopped' UX — never used to
    reveal content, only to say how far in you are)."""
    ch = current_chapter(position, bounds)
    if ch > len(bounds):
        return 1.0
    s, e = bounds[ch - 1]
    return (position - s) / (e - s) if e > s else 1.0


# --- sub-chapter UPGRADE PATH (routed; not the default) --------------------
# If whole-chapter lag is ever unacceptable, extend the frontier to a (chapter, offset) pair and stamp
# each extracted fact with the sub-chapter position of its source span. The DAL predicate becomes:
def subchapter_visible(fact_chapter, fact_subpos, bm_chapter, bm_offset):
    """A fact is visible iff its chapter is fully before the bookmark chapter, OR it is IN the bookmark
    (in-progress) chapter AND its source span has been fully read. Removes the lag WITHOUT leaking.

    CONTRACT (rev-2, from adversarial review):
      - `fact_subpos` is the **END** offset of the fact's source span (the last char the reader must
        pass), so `<=` is correct: a span straddling the cursor (ends after `bm_offset`) stays hidden.
      - `bm_offset` and `fact_subpos` are a **normalized fraction-through-chapter** [0,1] in the SAME
        space: LIT-13 computes it from CFI + the chapter's char range; LIT-6 stamps each fact's span
        END in that space. A raw CFI gives order + locate-in-range but NOT a subtractable global
        metric, so this normalized offset is the required primitive — not the bare CFI comparator.
      - Scoped to file-driven books until anchor-driven CFI→normalized-offset is defined.
    Requires sub-chapter-stamped facts (LIT-6) — the UPGRADE, not the default integer frontier."""
    return (fact_chapter < bm_chapter
            or (fact_chapter == bm_chapter and fact_subpos <= bm_offset))
