# LIT-4 — EPUB chapter segmentation spike

Empirical spike behind [ADR 0001](../../docs/adr/0001-epub-chapter-segmentation.md) (**Accepted**). Stdlib only, no venv.

- `inspect_epubs.py` — downloads a structurally-varied EPUB2/NCX corpus (Project Gutenberg) into `../../books/` (gitignored) and dumps each book's **raw** spine / nav / front-matter structure.
- `fetch_se.py` — fetches two **EPUB3** Standard Ebooks titles (clean `epub:type` semantics).
- `peek_epubtype.py` — dumps the EPUB3 `epub:type` landmarks + per-doc tokens.
- `segment.py` — the **accepted** segmentation algorithm (`epub:type`-first, NCX-heuristic fallback) + validation against expected chapter counts.
- `report.json` — machine-readable EPUB2 structure dump.

```bash
python3 spikes/lit-4-segmentation/inspect_epubs.py   # EPUB2 raw structure dump
python3 spikes/lit-4-segmentation/fetch_se.py        # fetch EPUB3 Standard Ebooks
python3 spikes/lit-4-segmentation/segment.py         # accepted algorithm + validation (7 books)
```

**Corpus (7):** Karamazov (12 Books), Hamlet (Act→Scene), Pride & Prejudice (many chapters/file + NCX pollution), Sherlock Holmes (story collection), Frankenstein (license appended to last chapter) — all EPUB2/NCX; plus **EPUB3** Wilde *The Importance of Being Earnest* (has a `dramatis-personae` cast list) and Austen *Pride and Prejudice* (Standard Ebooks).

**Result:** 7/7 within tolerance (97 / 20 / 60 / 12 / 28 / 3 / 61). The EPUB3 Wilde play **excludes the `dramatis-personae` cast list as front-matter** via `epub:type` — the original spoiler vector, proven closed. See the ADR for the decision table, the adversarial-review findings, and routed follow-ups.
