# ADR 0039: Reader corrections are future-effective, provenance-visible memory facts

**Status:** Accepted (2026-07-21)
**Ticket:** LIT-59 / QUALITY-1

## Context

Generated structured memory can contain a mistaken or incomplete character name. Editing the original
entity in place would rewrite what the Codex showed at earlier scrub points, destroy provenance, and
make a later correction visible before the reader reached it. The trusted split/merge machinery from
ADR 0013 already established the required bitemporal model but deliberately had no reader surface.

## Decision

The reader may publish a one-for-one identity-name correction only at the exact current completed
bookmark. The source identity must have been revealed strictly earlier. In one transaction the source
ends at that bookmark, a replacement starts there, the prior canonical name becomes an alias, and all
currently visible aliases, state, relationships, and event participation are copied forward. An
immutable `replace` correction records source and target identities, the reader's bounded reason,
schema version, effective bookmark, and database timestamp.

Correction history is itself spoiler-bounded: history reads return only rows whose effective bookmark
is at or before the requested server-clamped frontier. The Codex shows old-to-new names, reason, and an
effective-chapter navigation control. Scrubbing before the correction therefore restores both the old
identity model and an empty history for that future change.

Hosted mutation requires session-derived ownership, CSRF, a per-book runtime lock, forced RLS, and
explicit owner/book/incarnation predicates. It locks the reading row, rejects stale bookmark input,
requires a live ingestion receipt for the correction chapter, and writes the replacement and audit
event atomically. No model or provider is involved, so the action has no provider cost.

## Consequences

- Published history is never rewritten, and correction provenance cannot leak backward.
- Correction is deliberately limited to a displayed identity name; ambiguous split/merge assignments
  remain trusted tooling until a reader UX can make every dependency decision explicit.
- A character first appearing in the current completed chapter becomes correctable after the next
  completed chapter, preserving the strict valid-time boundary.
