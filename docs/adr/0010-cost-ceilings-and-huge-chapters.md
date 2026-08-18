# ADR 0010 — Cost ceilings, huge chapters, and runaway-spend guardrails

- **Status:** Accepted
- **Date:** 2026-07-13
- **Ticket:** LIT-21

## Context

The catalog recorded successful extraction and recap usage only after provider calls. That made the
ledger useful for display but not as a ceiling: concurrent calls could observe the same remaining
budget, a crash between provider I/O and the ledger write could lose spend, generic structured-output
re-asks were under-counted, output size was not capped, and an unusually large chapter was sent as one
request. USD pricing was also known for only a small model set.

## Decision

Before every completion or embedding batch, insert a durable `cost_reservations` row inside `BEGIN IMMEDIATE` after
atomically checking the book's ledger plus all active reservations. Reserve a conservative input
estimate, the provider-enforced maximum output, and known-model worst-case USD. Replace the estimate
with returned usage, then settle it into `cost_ledger`. Successful extraction chunks remain reserved
until the chapter's memory transaction produces its LIT-7 receipt; `finalize_ingest` publishes the one
aggregated extraction row and deletes all of that chapter's reservations in the same catalog
transaction. Rejected recap/judge calls still settle because they were billed.

Use UTF-8 byte length plus message/schema headroom as a tokenizer-independent input upper bound.
Generic OpenAI-compatible structured output reserves both the initial attempt and its one corrective
re-ask, and the client aggregates both usage reports. Native OpenAI and Anthropic calls receive explicit
output-token caps. Unknown model prices reserve zero advisory USD but remain protected by hard token
ceilings; never guess a current price.

If a chapter exceeds the input ceiling, split only provider I/O at the latest safe paragraph, newline,
or word boundary. Chunks are non-overlapping and concatenate exactly to the stored chapter. Validate
each structured result, concatenate summaries, and preserve extraction order for entities,
relationships, events, and themes. The memory model still commits one chapter ordinal, raw chapter,
chunk vector, completion receipt, and aggregate cost. This avoids redefining the LIT-4/LIT-12 spoiler
atom. Boundary-local context can be weaker than a whole-chapter call; the deterministic split prefers
large paragraph boundaries and records this as a quality limit, not a spoiler-safety exception.

On process death, retain reservations across restart. Unknown in-flight spend fails closed until an
operator runs the explicit status/reconcile command, which converts the reserved estimate into a
clearly labelled ledger row. Backup refuses outstanding reservations rather than silently exporting
ambiguous spend state.

## Rejected alternatives

- Post-call ledger checks: detect overspend only after it happens and race under concurrency.
- Process-local counters: lose reservations on crash and do not protect another process.
- Truncating huge chapters: silently drops facts and damages recall.
- Creating sub-chapter memory atoms: changes the spoiler frontier and requires a different LIT-12 data
  model.
- Treating a stale reservation as free: favors availability over an honest cost bound.

## Consequences

Defaults cap a book at 2,000,000 input tokens, 500,000 output tokens, and USD 5.00 where pricing is
known, with 60,000 estimated input and 4,096 output per provider request. Operators can override all
five positive settings through environment variables. Ceiling exhaustion refuses provider I/O; recap
returns HTTP 429, ingestion surfaces an error, and the optional “right now” line degrades to absent.
