# ADR 0006 — Sub-chapter spoiler frontier (continuous CFI ↔ chapter-stamped memory)

**Status:** **Accepted** (2026-06-26) — frontier mapping + the pending-chapter rule defined and proven
non-leaking against the live LIT-5 DAL + LIT-6 store; survived **two** adversarial Opus passes (both
FIX_THEN_ACCEPT). Pass 1 caught two HIGH leaks (a zero-length atom / unmerged-divider breaking the
geometric↔`revealed_at` 1:1 alignment) + a circular-proof gap; pass 2 (a 99-position probe grid)
confirmed all fixes hold with **0 leaks** and tightened the alignment guard to a true 1:1 identity
check — all fixed and re-proven.
**Date:** 2026-06-26
**Ticket:** LIT-12 (build-blocker — the last pure one).
**Spike code:** [`spikes/lit-12-frontier/`](../../spikes/lit-12-frontier/) — `frontier.py` (the mapping + the sub-chapter upgrade), `demo.py` (proof against the real store).
**Builds on:** ADR 0001 (the chapter atom + per-chapter CFI range), ADR 0002 (the DAL's integer `bookmark`). Feeds LIT-13 (reader engine: it supplies the real CFI).

## Context

The most subtle spoiler-leak vector: the reader engine yields a **continuous CFI** (usually
mid-chapter), but the memory is stamped per **whole chapter** (`revealed_at` = chapter ordinal). The
seam fails two ways: **lag** — a read chapter not yet reflected makes the recap useless; **leak** —
treating "entered chapter N" as "chapter N is in the recap" surfaces the *end* of N, which the reader
hasn't reached. We must define exactly when a chapter joins the recap and how the CFI becomes the
integer `bookmark` the DAL already filters on.

## Decision

**Frontier = the number of chapters the reader has FULLY completed; the chapter under the CFI is held
PENDING.** A chapter joins the recap only once the reader's position is **at or past its end**, never
on entry. The pending chapter's facts (stamped `revealed_at` = its ordinal) are excluded because
`bookmark < that ordinal` — no DAL change required.

```
bookmark = count(chapters whose end-position <= reader_position)   # the integer the LIT-5 DAL consumes
current  = the chapter whose [start, end) contains reader_position # held pending
```

- **Eligibility:** whole-chapter-on-completion (the safe option), not on-entry. Non-leaking **given
  the preconditions below**: the unread remainder lives in the pending chapter, whose ordinal > bookmark.
- **Preconditions (rev-2, from adversarial review) — enforced, not assumed:**
  1. **1:1 atom↔ordinal alignment** — the frontier's geometric ranges are the DAL's *included,
     post-merge* chapter atoms, one per `revealed_at` ordinal (`assert_aligned` raises on drift). An
     unmerged divider (a geometric range with no `revealed_at`) would otherwise shift the count and
     leak the in-progress chapter.
  2. **No zero-length atom** — `chapter_bounds` rejects `len == 0`; a degenerate atom would be counted
     "complete" at its boundary (`end == start`) yet never be pending, leaking its stamped facts with
     nothing read. (The ADR-0001 `<200w`/empty divider-merge must run first — currently *flagged* by
     segmentation, must be *implemented* before the build.)
  3. **Monotonic-forward** — the persisted bookmark is a high-water mark (`bookmark_high_water`);
     paging backward never un-reveals. Non-monotonic re-read/rewind is **LIT-17**.
- **CFI → bookmark:** a CFI is a total order over positions; the frontier needs only *compare* and
  *locate-in-[start,end)*. The spike models position as a monotonic char-offset (cumulative chapter
  length), **order-isomorphic** to CFI for those ops; LIT-13's engine supplies the real CFI comparator.
- **"Where you stopped":** recap of completed chapters + "you are X% into chapter N: «title»". The
  title is front-matter the reader has already seen; **none of chapter N's extracted facts are shown.**
- **Lag policy (accepted):** the just-read prefix of the in-progress chapter is not recapped until the
  chapter completes. This is acceptable for a catch-me-up tool — the in-progress chapter is the one the
  reader just read / would re-read on return; the recap's job is the *earlier* chapters they've
  forgotten. The cost is bounded (≤ one chapter).

*Rejected:* on-entry eligibility (leaks the chapter's end). *Deferred:* sub-chapter extraction of the
read prefix (precise but needs sub-chapter-stamped facts — see upgrade path) — adopt only if the
one-chapter lag proves unacceptable in the MVP.

## Sub-chapter upgrade path (routed, not the default)
`frontier.subchapter_visible(fact_chapter, fact_subpos, bm_chapter, bm_offset)` extends the frontier to
a `(chapter, offset)` pair: a fact in the in-progress chapter is visible iff its source span sits
at/behind the reader's offset — removing the lag **without leaking** (unread-span facts stay hidden).
It requires every extracted fact to carry a sub-chapter source position (an extraction change owned by
**LIT-6**) and an extended DAL predicate. Prototyped + proven here so the option is ready; not wired
into the default integer frontier.

## Validation (`demo.py`, all 20 checks pass, against the real Karamazov store)
- Mapping at 0% / 60% / 100% of a chapter (entered → N-1; mid → N-1; completed → N; book start → 0; book end → all).
- **Non-leak**: 15 reader positions (every chapter × {0%, 60%, 99%}) — `view(bookmark)` surfaces no
  character/event/relationship from the in-progress chapter or beyond.
- "Where you stopped" names the current chapter but shows none of its facts.
- The documented lag (at 99% of ch4, chapter-4 facts are not in the recap; bookmark = 3).
- The sub-chapter upgrade filter (read-prefix visible, unread hidden, earlier visible, later hidden).

## Adversarial review

### Pass 1 (2026-06-26) — verdict **FIX_THEN_ACCEPT**. All fixed + re-proven.

| Sev | Finding (probe-verified) | Disposition |
|---|---|---|
| HIGH | **Zero-length atom leaks** — a `len==0` chapter is counted *complete* at its degenerate boundary (`end==start`) yet can never be the in-progress chapter, so its `revealed_at` facts surface with nothing read. Reachable: the ADR-0001 divider-merge is only *flagged*, not implemented. | **FIXED.** `chapter_bounds` rejects `len<=0` (precondition: no zero-length atom); demo asserts the rejection. (The real fix upstream is to implement the divider-merge so no empty atom ever gets a `revealed_at`.) |
| HIGH | **Geometric↔ordinal drift** — `cfi_to_bookmark` counted raw geometric ranges, but the DAL filters `revealed_at` ordinals over the *post-merge* atom set; an unmerged divider makes the two diverge and leaks the in-progress chapter. | **FIXED.** `assert_aligned(bounds, n_revealed_atoms)` ties the frontier's atoms 1:1 to the DAL's; the demo builds bounds from the store's real atoms and asserts alignment + catches a synthetic drift. |
| MED | **Circular non-leak proof** — the filter and the oracle were both derived from the same ordering; the `revealed_at > bm` half could never fire (the DAL enforces it). | **FIXED.** The demo now (a) calls the independent `reveal_correctness_eval` (name-first-appears-in-prose-by-`revealed_at`) and (b) asserts every surfaced fact's chapter `end <= position` (read past), not merely `revealed_at <= bm`. |
| MED (PARTIAL) | **Sub-chapter upgrade under-specified** — `subpos` START-vs-END unpinned; `<=` admitted a straddling span; needs a subtractable metric a bare CFI doesn't give. | **SPECIFIED.** `fact_subpos` = span **END** (keep `<=`); the upgrade uses a **normalized fraction-through-chapter** (LIT-13 from CFI+char-range, LIT-6 stamps in the same space), file-driven-scoped. Demo adds boundary + straddling-span checks. (Path remains routed/unbuilt → LIT-6.) |
| — | **Monotonic-forward** was an unstated assumption. | **STATED + enforced** via `bookmark_high_water`; re-read/rewind routed to LIT-17. |
| — | `chapter_progress` div-by-zero | **FALSE POSITIVE** (the reviewer retracted it — guarded by `if e > s`). |

Post-fix, `demo.py` PASSes all 20 checks.

### Pass 2 (2026-06-26) — re-attack of the fixed code. Verdict **FIX_THEN_ACCEPT** → fixed.
A 99-position probe grid (every chapter × many fractions + exact boundaries s, s+1, e-1, e, e±0.5,
book start/end/past/negative) + targeted attacks against the live store: **0 leaks, 0 in-progress
facts surfaced**; every rev-1 fix confirmed by probe (zero-length rejection not bypassable; the
non-leak teeth falsifiable — `view(bm+1)` fires; `reveal_correctness_eval` non-vacuous — a planted
mis-stamp is flagged; high-water monotonic; sub-chapter END/straddle boundary exact). Two items, fixed:

| Sev | Finding | Disposition |
|---|---|---|
| MED | **`assert_aligned` was count-only** — blind to a same-count ordinal gap (`{1,2,4}`), so it over-claimed the 1:1 identity LIT-13 is told it has (not a leak — a gap under-reveals; the only leak direction, an extra geometric range, *was* caught). | **FIXED.** `assert_aligned` now takes the `revealed_at` ordinal list and asserts contiguity 1..N (true identity), so the reusable guard — not just the demo — enforces it. Demo asserts a gap is caught. |
| LOW | **Stale check-count in the ADR** ("16"/"20"; the review's own count was also off). | **FIXED.** Corrected to the actual **20** checks. |

Post-fix, `demo.py` PASSes all 20 checks; no BLOCKER/HIGH leak remains.

## Consequences & routed follow-ups
- **LIT-13** (reader engine) implements the CFI capture and calls `cfi_to_bookmark` with the engine's
  CFI comparator (replacing the char-offset stand-in); it must define a chapter's start/end CFI from
  the LIT-4 segmentation anchors.
- **LIT-8** (spoiler eval) already flagged sub-chapter as its RAG residual; this ADR is that residual's
  resolution (whole-chapter frontier now; the upgrade if needed). The eval should add a reader-position
  axis once the engine emits CFIs.
- **Re-read / rewind** (the reader going *backwards* then forward, partial re-reads) is **LIT-17**; the
  frontier here is monotonic-forward — LIT-17 owns non-monotonic position semantics.
- The sub-chapter upgrade's fact-position stamping → **LIT-6**; the extended DAL predicate → a small
  LIT-5 follow-up if adopted.

## Outcome
A precise, provably non-leaking mapping from the reader's continuous CFI to the integer bookmark the
DAL consumes: the in-progress chapter is pending until completed, so the unread remainder can never
surface, at the cost of a bounded one-chapter lag — with a ready sub-chapter upgrade if that lag bites.
**Accepted** for LIT-12; with it, the eight build-blockers are closed/provisional and the engine
(LIT-13) + MVP views (LIT-14/15) can build on a defined frontier.
