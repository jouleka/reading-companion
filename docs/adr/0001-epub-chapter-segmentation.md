# ADR 0001 — EPUB chapter segmentation & the "chapter as atom"

**Status:** **Accepted** (2026-06-25). The EPUB3 `epub:type` reader is implemented and validated on **7** structurally-varied real books across EPUB2/NCX and EPUB3 — including the original spoiler vector: Standard Ebooks' `dramatis-personae` (cast list) is excluded as front-matter. LIT-23 / ADR 0016 later added bounded Russian/CJK legacy cast and structural labels; arbitrary-language legacy front/back classification remains intentionally out of scope.
**Date:** 2026-06-25
**Ticket:** LIT-4 (build-blocker)
**Spike code:** [`spikes/lit-4-segmentation/`](../../spikes/lit-4-segmentation/) — `inspect_epubs.py` (raw structure dump), `segment.py` (this algorithm), `report.json`.

## Context

The **chapter is the atom** of the whole product: `revealed_at` is stamped per chapter, the spoiler filter is `revealed_at <= bookmark`, ingestion is append-once per chapter, and the reader's CFI position maps onto a chapter index (LIT-5, LIT-12). If segmentation is wrong, every downstream guarantee is wrong. We needed an algorithm grounded in **real EPUB structure**, not theory — especially the failure mode where front-matter (a translator intro or a play's *Dramatis Personae*) leaks the cast as "chapter 1."

We inspected 5 structurally-varied public-domain EPUBs (all Project Gutenberg, all **EPUB 2.0 + NCX**):

| Book | Real structure | The hard part it exercises |
|---|---|---|
| The Brothers Karamazov | 12 "Books", 1 file per chapter | Part/Book dividers; intro + cast list front-matter |
| Hamlet | play, hierarchical Act → Scene | nested ToC (don't double-count Act + Scene) |
| Pride and Prejudice | 61 chapters across ~13 files | **many chapters per file** (ToC anchors w/ fragments) |
| Adventures of Sherlock Holmes | 12 standalone stories | collection (no continuous plot) |
| Frankenstein | 4 Letters + 24 Chapters | **license appended to the last chapter file** |

Three naive assumptions died on contact with real data:
1. **The `<title>` tag is boilerplate** ("… | Project Gutenberg") on *every* doc — useless as a heading, and "PROJECT GUTENBERG" cannot be a front/back signal.
2. **Position-based "near the end = back-matter" is wrong** — it ate Hamlet's Acts III–V and Frankenstein's last chapters.
3. **"File contains the license → drop it" deletes a real chapter** — Gutenberg appends the license to the *last chapter's* file.

## Decision

Derive chapters from the **OPF spine + ToC (EPUB3 `nav` with `epub:type`, else EPUB2 NCX `navMap`)**, classifying by **explicit signals only — never by position**.

### Algorithm
1. **Parse** OPF: linear spine order, manifest, `guide`, and the ToC **keeping fragments and hierarchy** (leaf vs parent navPoints).
2. **Classify each spine doc** — **EPUB3 `epub:type` first (authoritative, fail-closed); legacy heuristics only when it is absent:**
   - **EPUB3 `epub:type`** (the doc's `<body>`/`<section>` token): `frontmatter`/`titlepage`/`imprint`/`dedication`/`dramatis-personae`/… → **front**; `backmatter`/`colophon`/`endnotes`/… → **back**; `bodymatter`/`chapter`/`part`/`scene` → **body**. A cast list tagged `frontmatter` can never become a chapter.
   - **Legacy (no `epub:type`, e.g. EPUB2/NCX):**
     - **front** — the nav doc; a `guide` cover/title/toc/copyright reference; filename `cover|title|toc|contents|copyright|colophon|imprint`; first 4 KB contains `START OF THE PROJECT GUTENBERG`; or a ToC label/heading in the expanded front allowlist (Contents / Illustrations / Title page / Cover / Copyright / Introduction / Preface / Foreword / *Dramatis Personae* / Cast / Dedication / Translator's note).
     - **back** — a PG license marker (`END OF … PROJECT GUTENBERG` / `PROJECT GUTENBERG … LICENSE` / `Section 1. General Terms`) **only if it has no chapter-like label** (license appended to a chapter file → stays a chapter, trimmed at the text layer).
     - **body** — everything else.
3. **Body window** = first…last `body` doc. Front/back matter = the runs outside it. (No edge heuristics inside the window.)
4. **Granularity:** if leaf ToC navPoints landing in body files outnumber body files by >1.3×, the book packs **many chapters per file** → **anchor-driven** (one chapter per leaf ToC navPoint, split at fragments). Otherwise **file-driven** (one spine doc = one chapter).
5. **Title** = ToC label → else first body `<h1>/<h2>` → else `(untitled N)`.
6. **Chapter key** (stable, re-open-safe — keyed on content identity, NOT position):
   `file-driven → {book_id}:{href}` · `anchor-driven → {book_id}:{href}#{fragment}`.
   Keying off the spine doc href / manifest-id (not a positional index) means a later classification change — e.g. detecting one extra front doc — does **not** renumber every downstream key. `revealed_at` = the chapter's 1-based ordinal among included chapters: a separate derived integer the bitemporal filter uses; the ordinal may change on re-segmentation, the **key** does not. The chapter's CFI range runs from its anchor to the next chapter's anchor (consumed by LIT-12).

### Decision table (signal → action)
| Signal on a spine doc | Action |
|---|---|
| is the `nav`/NCX doc | front (exclude) |
| `guide` ref type ∈ {cover, title-page, toc, copyright-page} | front |
| filename ~ cover/title/toc/contents/copyright/colophon | front |
| body text starts with `START OF THE PROJECT GUTENBERG` | front (PG header page) |
| ToC label/heading ∈ {Contents, Illustrations, Title page, Cover, Copyright} | front |
| PG license marker **and** no chapter-like label | back |
| PG license marker **and** chapter-like label | body + flag "trim trailing license" |
| nested ToC (Act → Scene) | use **leaf** navPoints (scenes), never both levels |
| leaf ToC navPoints ≫ body files | anchor-driven split |
| else | body / file-driven chapter |

## Validation (run `python3 spikes/lit-4-segmentation/segment.py`)
| Book | Format | Mode | Chapters | Expected | Verdict |
|---|---|---|---|---|---|
| Karamazov | EPUB2/NCX | file-driven | **97** | ~95 | OK (Part dividers flagged) |
| Hamlet | EPUB2/NCX | anchor (leaf=scene) | **20** | 20 | OK (license ToC entry filtered) |
| Pride & Prejudice | EPUB2/NCX | anchor (leaf) | **60** | 61 | OK (NCX illustration-caption pollution; ch.1 in title file) |
| Sherlock Holmes | EPUB2/NCX | file-driven | **12** | 12 | OK |
| Frankenstein | EPUB2/NCX | file-driven | **28** | 28 | OK (appended license kept the chapter) |
| Importance of Being Earnest (SE) | EPUB3 | file-driven | **3 acts** | 3 | OK — **`dramatis-personae` excluded as front-matter** |
| Pride & Prejudice (SE) | EPUB3 | file-driven | **61** | 61 | OK — clean nav, no pollution |

**Spoiler-vector proof:** on the Standard Ebooks Wilde play, front-matter stripped = `titlepage, imprint, dramatis-personae, the-scenes-of-the-play, halftitlepage` and back = `colophon, uncopyright` — i.e. the **cast list (`dramatis-personae`) is excluded by the `epub:type` signal**, closing the original motivating failure. On legacy EPUB2/NCX the same vector is covered best-effort by the expanded front allowlist; a non-English *legacy* intro remains a known gap (routed to LIT-23).

## Consequences & known ambiguities (each handled or flagged, not hidden)
- **Part/Book dividers** (Karamazov "PART I", ~20 words) currently surface as tiny chapters → **flagged**; rule: merge a <200-word, label-only divider into the following chapter, keeping the Part as a grouping attribute (not its own `revealed_at` atom).
- **Illustration-caption pollution** in legacy NCX (P&P "Covering a screen. CHAPTER VIII.") → **flagged**; mitigation: prefer EPUB3 `epub:type` landmarks where present; for legacy NCX, dedupe by target anchor and prefer the `CHAPTER N` substring as the label.
- **License appended to the last chapter file** → chapter kept; license trimmed at the text layer (a text-extraction concern, tracked for the extraction pipeline LIT-6).
- **Hierarchical ToC** → leaf level chosen (scenes for a play). Whether the reader wants Act- or Scene-granularity is a per-book-type preference (coordinate with book-type detection LIT-9).
- **P&P 60 vs 61** — chapter I's anchor sits inside the title/front file that gets stripped. Acceptable within tolerance; the fix (don't strip a front file that also contains the first body anchor) is a follow-up.

## Adversarial review (2026-06-25) — findings & disposition
An independent Opus review flagged that the corpus is a single-publisher EPUB2/NCX monoculture and that the classifier is a *denylist* of front/back signals with a `body` default — an inverted safety posture for a spoiler gate. Findings and what was done:

| # | Sev | Finding | Disposition |
|---|---|---|---|
| 1 | BLOCKER | Unsignaled front-matter (Introduction, *Dramatis Personae*, Preface) defaults to `body` → could become "chapter 1" and leak; corpus never exercised it. | **FIXED & PROVEN:** `epub:type` (`frontmatter`) now authoritative; front-label allowlist expanded for legacy. Validated — SE Wilde play's `dramatis-personae` is excluded. |
| 2 | BLOCKER | `epub:type` landmarks/section semantics promised but never read; on EPUB3 the whole front/back strategy degrades. | **FIXED:** `epub:type` reader implemented as the primary classifier; validated on 2 Standard Ebooks (EPUB3) titles. |
| 3 | HIGH | `START OF THE PROJECT GUTENBERG` marks the *book*, not a throwaway page → dropping the whole doc could drop a real chapter. | **Documented:** treat START like the END-license — trim *above* the marker at the text layer (tracked with LIT-6), not whole-doc. |
| 4 | HIGH | No coverage check → a body file with no ToC anchor silently dropped. | **Fixed:** coverage assertion added (every body file must yield ≥1 chapter, else flag). |
| 5 | HIGH | `linear="no"` items dropped yet may be ToC targets. | **Deferred:** classify/flag explicitly (overlaps LIT-11). |
| 6 | MED | Chapter key used a **positional index** → shifts if classification changes, breaking append-once. | **Fixed:** keys are href/manifest-id based (content identity), never positional. |
| 7 | MED | No dedup of duplicate spine hrefs (file-driven). | **Fixed:** file-driven dedups by href. |
| 8 | MED/LOW | `label_for_file` first-wins collision; non-Latin labels ("Глава", "第一章") match no pattern; malformed OPF crashes. | **Deferred:** non-Latin enumerators + graceful OPF failure (overlaps LIT-11). |

## Remaining follow-ups (non-blocking; routed)
- **No-ToC single-file** book → heading-density split. The coverage assertion *flags* this today rather than failing silently.
- **Non-Latin / legacy-language** front detection: `epub:type` handles modern non-English EPUB3, but a *legacy* EPUB2 intro in a non-Latin script can still default to body → routed to **LIT-23** (i18n) and **LIT-11** (edge EPUBs).
- `linear="no"` interstitials, duplicate-href edge cases → **LIT-11**.
- START-marker text-layer trimming → **LIT-6** (extraction).
- A **Calibre-exported** EPUB as an additional corpus sample (different conventions) — nice-to-have.

## Outcome
A spine+ToC, **`epub:type`-first**, explicit-signal, leaf-aware segmentation algorithm that produces correct chapter atoms across **7** structurally-different real books spanning EPUB2/NCX and EPUB3 — with content-identity keys, a coverage assertion against silent loss, and a **proven** exclusion of a play's cast list (the original spoiler vector). It defines the `revealed_at` chapter unit and chapter-key scheme that LIT-5 (schema/DAL) and LIT-12 (CFI↔chapter frontier) build on. **Accepted** for EPUB2 + EPUB3; the routed follow-ups above harden the long tail.
