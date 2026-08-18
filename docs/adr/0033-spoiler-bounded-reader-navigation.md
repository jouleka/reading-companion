# ADR 0033: Reader navigation preserves exact place and the spoiler frontier

## Status

Accepted — 2026-07-21

## Context

Long EPUBs need a hierarchical table of contents, full-text search, and exact back/forward navigation. Raw EPUB labels and unrestricted search results can reveal unread chapter titles or prose. The reader must work offline, while hosted search must preserve owner isolation and deletion semantics.

## Decision

The reader preserves the EPUB table-of-contents hierarchy, but it never renders raw EPUB labels. Labels come only from server-released atom metadata, with neutral chapter or section fallbacks. The active item follows the current atom.

Foliate remains the authority for exact CFI navigation history. Table-of-contents jumps use the existing far-jump confirmation, while back and forward restore Foliate's exact prior locations.

Offline search may scan the loaded EPUB, but it exposes results only for completed atoms. Each accepted result retains its exact CFI, displays escaped context, and clears all temporary raw-search annotations. Anchor-driven books fail closed when a match cannot be mapped to a released atom.

Hosted uploads atomically create owner- and incarnation-scoped search documents. PostgreSQL maintains a generated `tsvector` with a GIN index. Queries require a live owner/book match, apply forced RLS and explicit owner predicates, and return only documents below the durable reading frontier. Soft deletion physically removes the search documents. The same documents provide the hosted reader manifest.

Queries and result counts are bounded, and snippets use plain markers that the client renders as text with explicit highlighting.

## Consequences

- Contents, search, and history remain useful without exposing future labels or prose.
- Search results and history restore exact reading locations rather than approximate chapter positions.
- Hosted search duplicates bounded chapter text in PostgreSQL, so tenant isolation and deletion tests cover that storage explicitly.
- Ambiguous anchor mappings produce no result instead of risking a spoiler.
- Search is bounded to 200 query characters and 50 results.
