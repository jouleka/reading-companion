# ADR 0035: Reader marks use portable anchors and remain frontier-bounded

## Status

Accepted — 2026-07-21

## Context

Highlights, annotations, and bookmarks must survive typography changes, synchronize between hosted sessions, remain useful in local mode, and export without a proprietary binary format. Persisted marks from a completed reading pass can themselves reveal future prose after the reader chooses “start over.”

## Decision

Every mark stores an EPUB CFI and a one-based atom ordinal. Text selections also carry a bounded text-quote selector (`exact`, `prefix`, and `suffix`) so another reader can recover when a CFI needs repair. PostgreSQL constraints require portable CFI-shaped anchors, integral bounded atoms, closed highlight colors, bounded selected text, annotation bodies, and bookmark labels. Each row carries a monotonic version and timestamps.

All hosted reads join a live owner/book/incarnation and return only anchors at or below `bookmark + 1`, the currently available chapter. Creation applies the same boundary. Every read and mutation uses explicit owner predicates, forced RLS, composite ownership-aware foreign keys, CSRF for writes, and the exact-grant runtime role. Book deletion soft-deletes all marks. A linked note can reference only a live highlight from the same owner, book, and incarnation.

The reader derives anchors from Foliate’s EPUB CFI for the actual selected range, renders highlights through Foliate’s overlayer, and navigates saved marks by exact CFI. The selection action surface uses native controls and escaped React text. The marks panel supports current-page bookmarks, deletion, and a versioned UTF-8 JSON export.

Hosted mode is authoritative and synchronizes through the owner-scoped API. A 404 from an older local backend selects a browser-local fallback with the same portable payload and frontier filter. Other marks-service failures degrade only the marks surface; they never prevent the EPUB from opening or turn a successfully saved reading position into a sync error.

## Consequences

- Marks survive pagination, measure, font, and theme changes because anchors are content locations, not pixels.
- Starting a new pass hides later marks until their chapters are available again.
- Export contains intentionally saved user text and no provider credentials or hidden server prose.
- Local fallback is device-local; cross-device convergence requires hosted mode.
- CFI is the primary locator and the text quote is a portable repair aid rather than a second source of truth.
