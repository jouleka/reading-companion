# ADR 0008 — Reader engine: foliate-js (LIT-13)

**Status:** Accepted (2026-07-03) — decided by an EXECUTED spike on the canonical book, not by
documentation claims (`frontend/src/spike/SpikePage.tsx`, run in a real browser against
`books/pg28054.epub` via the dev server).
**Ticket:** LIT-13 · builds on ADR 0006 (frontier),
ADR 0007 D-A10/D-A11 (position routes), D8/D10 (in-app reader, web-first).

## Decision

**foliate-js** (vendored ES modules at `frontend/src/vendor/foliate-js/`, MIT), rendered through its
`<foliate-view>` element, is the reading engine. epub.js is rejected. Readium was not exercised
(heavyweight integration; only needed if foliate fails, and it did not).

## Measured grounds (the spike, real Karamazov, Chrome)

| Probe | epub.js 0.3.93 | foliate-js @ main |
|---|---|---|
| Parse (spine / ToC) | 100 / 119 ✓ | 100 / 119 ✓ |
| First render | **`display()` TIMEOUT >15s** (book.ready fine — the render hangs; reproducible) | ~0.6s, pages immediately |
| CFI at position | unreachable (render hung) | `epubcfi(/6/14!/4/2[pgepubid00010],/2,/4/1:1142)` ✓ |
| Intra-section progress | unreachable | `relocate` detail: `fraction`, `section.current/total`, paginator `page/pages` (1/14) ✓ |
| Maintenance / license | stalled for years / FreeBSD | active (Foliate/GNOME ecosystem) / MIT |

One integration gotcha found by the spike and pinned for R3: `<foliate-view>` does NOT self-size —
without an explicit width/height its paginator lays out at 0px and `fraction`/`page` stay null. The
reader component must give it a sized block container.

## The position contract this enables (ADR 0006/0007 wiring)

The reader emits, debounced, on `relocate`: **(cfi, offset)** where `offset` = the monotonic char
offset the frontier consumes: `sum(char_len of atoms BEFORE the current section) + intra_section
_fraction × char_len(current atom)`, mapped via the import-time atom manifest (a new
`GET /api/books/{id}/manifest` route exposes `{ordinal, href, title, part_label, char_len}`; sections
map to atoms by href — exact for file-driven books; anchor-driven ambiguity is this ticket's known
routed residual, ADR 0001/Module C). `PUT /position` already derives the integer bookmark
monotonically server-side (`max` via SQL, D-A10) — the client never computes the spoiler frontier.

## Consequences

Vendoring (no npm package) means updates are manual copies — acceptable: the vendored tree is pure
MIT ES modules and the surface we use (view.js: `makeBook`, `<foliate-view>`, `relocate`) is small.
The stack stays D10 (React + TS + Vite; Vite 5 pinned to the box's node 18).
