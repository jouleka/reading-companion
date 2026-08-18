# ADR 0013 — Bookmark-effective entity split and re-merge corrections

**Status:** Accepted (2026-07-13)
**Ticket:** LIT-10

## Context

Entity resolution can fail in both directions: two people may be merged into one roster identity, or
two names for one person may remain fragmented. Correcting the database in place is not spoiler-safe.
If chapter 8 reveals that an earlier “Alex” referred to two people, rewriting chapter-1 foreign keys
would make that distinction visible to a reader scrubbing back to chapter 1. Conversely, retaining a
single timeless identity makes the correction impossible to express.

## Decision

An identity correction is valid-time story knowledge. At correction bookmark `N`:

1. every source identity must already be live and have `revealed_at < N`;
2. sources receive `invalid_at = N`;
3. replacement identity rows are created with `revealed_at = N`; and
4. aliases, active relationships, current state, and event participation are copied forward with the
   replacement identity and reveal stamp `N`.

Original rows are never rewritten or deleted. Referential closure therefore shows the old identity
model for bookmarks `< N` and the corrected identity model for bookmarks `>= N`. The entire operation
runs in one `MemoryDB.transaction()`, and an immutable `entity_corrections` row records source ids,
target ids, assignments, reason, schema version, and time.

Schema v3 adds `entities.invalid_at`, makes entities a valid-time table, and adds
`event_participants.revealed_at`. Existing participant links migrate to their parent event's reveal.
Database triggers reject inverted identity validity and unstamped new participant links. Recap cache
snapshots ignore a future correction at earlier bookmarks and change at the correction frontier.

### Split contract

A split creates two or more same-type replacements. The caller first obtains
`entity_correction_inventory(...)`, then explicitly maps every active alias, edge, and event
participation to zero or more target indexes. An empty list is an intentional drop; a missing or extra
record id rejects the operation before writes. Replacement state is explicit. Self-referential source
edges require manual re-extraction because a one-index assignment cannot express both endpoints.

### Merge contract

A merge creates one new same-type identity. Source canonical names and visible aliases become aliases
of the target; active edges are endpoint-rewritten and deduplicated; source-to-source self-edges drop;
event participation is unified. Conflicting participant roles require an explicit per-event override.
The target state is supplied explicitly rather than guessed from potentially contradictory sources.

### Recovery compatibility

Correction history is part of both SQLite and portable `.rcbackup` representations. A legacy schema-v2
archive remains verifiable: exact and portable restores migrate a temporary staging tree to v3 before
validation/publication. The archive itself and the source data remain unchanged.

## Consequences and limits

- The correction revelation cannot leak backward through structured views, event participation, recap
  cache keys, or character-card existence checks.
- Identity ids intentionally change at the correction frontier; an old deep link becomes a 404 at and
  after that frontier, while earlier bookmarked views retain it.
- Detection and assignment are manual trusted tooling. There is no end-user correction UI and no
  automatic split classifier in this ticket.
- Extractor-authored event/summary prose is not rewritten. It remains the text the reader already saw;
  generated recaps still pass the deterministic gates and fail-closed judge.
- Same-chapter correction at an identity's first reveal is rejected. Before publication, use the
  transaction-time re-extraction path; after publication, record the correction at a later bookmark.
