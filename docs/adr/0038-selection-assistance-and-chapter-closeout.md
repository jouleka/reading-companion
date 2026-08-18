# ADR 0038: Selection help is passage-only and chapter closeout is exact-chapter cited

## Status

Accepted — 2026-07-21

## Context

Explain, define, and translate actions are useful at the point of reading, including inside the
currently open but not yet completed chapter. Sending broader book context would let a small language
action become a spoiler retrieval channel. A chapter closeout has the opposite need: it should cover
the completed chapter, not silently summarize the whole memory or rely on a model's prior knowledge.
Both actions can incur owner-paid provider work and must preserve the LIT-57 citation and tenancy bar.

## Decision

`POST /api/books/{book_id}/selection-action` accepts one closed action (`explain`, `define`, or
`translate`), a bounded selected string, its one-based atom, and exact EPUB CFI. Translation is
deliberately limited to English in this release. The selection may belong to a completed chapter or
the one currently open atom (`bookmark + 1`); no later atom is accepted. Hosted mode resolves that
atom to a server-owned href under the session owner/book/incarnation. The selected string remains
reader-supplied evidence: the server does not retrieve or send any other chapter prose to the model.

The selected passage is marked untrusted and is the only book-specific source. General linguistic
knowledge is allowed for wording, definition, and translation, but book facts, motives, identities,
events, implications, and foreshadowing outside the selection are forbidden. Structured output is
either insufficient evidence or one bounded result citing source 1. A second provider setting judges
the result against only that selection; two failures return generic safe copy. The response carries
the server-owned chapter metadata and original CFI so the reader can return to the exact selection.

`POST /api/books/{book_id}/chapter-closeout` accepts exactly one chapter number. It must be at or
below the durable server reading frontier; local mode additionally requires that chapter's completed
ingestion and raw-text receipt. No request silently clamps to a different chapter. Evidence is sampled
across the beginning, middle, and end into at most six bounded excerpts. Hosted SQL owner-filters the
live incarnation and bounds both the number of documents and bytes returned before application
sampling. The closeout produces two to five claim-level takeaways, each grounded against its cited
excerpt, then passes the same citation-bounded judge. A missing or unsafe closeout becomes explicit
insufficient evidence or generic rejection.

Both paths use the owner's ready synthesis and judge settings, just-in-time credential resolution,
exact setting revalidation, atomic provider reservations, measured settlement, token ceilings, and
known/unknown pricing disclosure introduced by ADR 0037. Offline stub mode returns insufficient
evidence instead of pretending to explain or translate.

The reader exposes three native selected-text action buttons alongside highlight/note controls. Results
open in a focus-trapped modal with exact-anchor return. The companion offers an explicit closeout for
the latest completed chapter; it is not an automatic context change. Closeout citations navigate to
the chapter source. Both surfaces show provider/model, token use, payer, and price availability.

## Consequences

- Selection help can operate on text visibly under the reader's cursor without granting retrieval of
  the unread remainder of that chapter or any later chapter.
- A malicious client can ask about arbitrary text it supplies, but it receives no server book content
  beyond owned navigation metadata; existing owner rate, concurrency, token, and spend controls apply.
- Closeouts are exact-chapter and source-cited, not whole-book recaps relabelled as a chapter result.
- Bounded sampling can omit a detail in a very long chapter, so insufficient evidence is preferred to
  filling the gap from model memory.
- Closeouts are generated on explicit request and are not durably cached in this ticket; repeated use
  can incur another clearly disclosed provider charge.
