# LIT-12 spike — sub-chapter spoiler frontier

Resolves the seam where "spoiler-safe by construction" can silently break: the reader engine
(LIT-13) yields a fine-grained **CFI** (usually mid-chapter), but memory is stamped per **whole
chapter** (`revealed_at` = chapter ordinal). Get it wrong and you either **lag** (read content not
recapped — useless) or **leak** (the in-progress chapter shown past where the reader actually is).

## Run
```bash
python3 spikes/lit-12-frontier/demo.py     # all checks pass; proves non-leak against the live LIT-5 DAL
```

## Decision (the safe, simple frontier)
**The spoiler-filter bookmark = the number of chapters the reader has FULLY completed.** The chapter
currently under the CFI is held **pending** — its facts (stamped `revealed_at` = its ordinal) are
filtered out because `bookmark < that ordinal`. Provably non-leaking; accepts a bounded **lag**.

```
bookmark = count(chapters whose end-position <= reader position)   # frontier.cfi_to_bookmark
current  = the chapter whose [start,end) contains the reader       # held pending
```

- **Eligibility:** a chapter enters the recap only once **fully read** (reader position ≥ its end),
  never on entry — so the unread remainder of the in-progress chapter can't surface.
- **Mapping to the DAL:** the continuous CFI collapses to the integer `bookmark` the LIT-5 DAL already
  consumes; no DAL change needed for the default.
- **"Where you stopped":** recap of completed chapters + "you are X% into chapter N: «title»" — the
  title is front-matter the reader has seen; none of chapter N's extracted facts are shown.
- **Lag policy:** the just-read prefix of the in-progress chapter isn't in the recap until the chapter
  completes. Acceptable: you just read it; it's the chapter you'd re-read on return anyway.

## Position model (faithful to CFI for what the frontier needs)
A CFI imposes a **total order** on positions in spine order. The frontier needs only two operations:
compare two positions, and locate a position within a chapter's `[start,end)` range. The spike models
position as a monotonic char-offset (cumulative chapter length), which is **order-isomorphic** to a CFI
for exactly those operations. The real reader (LIT-13) supplies the CFI comparator; the frontier logic
is identical.

## Sub-chapter upgrade path (routed; only if lag is unacceptable)
`frontier.subchapter_visible(fact_chapter, fact_subpos, bm_chapter, bm_offset)` extends the frontier to
`(chapter, offset)`: a fact in the in-progress chapter is visible iff its source span sits at/behind the
reader's offset. This removes the lag **without leaking** — but requires facts stamped with a
sub-chapter position (an extraction change owned by **LIT-6**), so it is the upgrade, not the default.

## What the demo proves (against the real Karamazov store)
1. Mapping at 0% / 60% / 100% of a chapter (entered → bookmark N-1; mid → N-1; completed → N).
2. **Non-leak**: 15 reader positions (every chapter × {0%, 60%, 99%}) — `view(bookmark)` surfaces no
   fact from the in-progress chapter or beyond.
3. "Where you stopped" names the current chapter but shows none of its facts.
4. The documented lag (at 99% of ch4, chapter-4 facts are not yet recapped).
5. The sub-chapter upgrade filter: read-prefix fact visible, unread fact hidden.

Full record: `../../docs/adr/0006-subchapter-spoiler-frontier.md`.
