# ADR 0037: Book answers are passage-bounded, claim-cited, and cost-visible

## Status

Accepted — 2026-07-21

## Context

An open-ended question is a stronger spoiler channel than a recap. A model may know the rest of a
well-known book, follow instructions embedded in the question or EPUB prose, attach a citation that
does not support its whole claim, or hide owner-paid provider work behind an apparently free action.
Hosted execution also has to preserve tenant isolation and atomically reserve spend before any secret
leaves the credential vault for provider I/O.

## Decision

`POST /api/books/{book_id}/ask` accepts a bounded question and optional bookmark. The server computes
the effective completed frontier; client input can only narrow it. Retrieval is restricted before
ranking to passages at or before that frontier. Local mode uses the existing bookmark view, completed
receipt frontier, pinned embedding space, and manifest. Hosted mode uses the session-derived owner,
live book incarnation, durable reading frontier, explicit owner predicates, forced RLS, and the
owner-scoped search index. A foreign book remains indistinguishable from a missing one.

The synthesis prompt treats both question and passages as untrusted content and permits only the
retrieved passages as evidence. Structured output is either an explicit insufficient-evidence result
or at most six short claims, each citing one to three supplied passage identifiers. The server rejects
unknown citations and checks every claim against only its cited passages. Local answers additionally
pass the deterministic spoiler gates; both local and hosted answers pass an independent model judge
using only the citations actually returned. Two rejected attempts end in a generic response that does
not echo unsafe prose. Citations expose a bounded excerpt plus manifest title/href so the reader can
jump to the source without widening the frontier.

Hosted synthesis and judge settings must both be enabled, validated, and backed by a live owned
credential. The credential is resolved just in time into a short-lived client. Under the owner's
database advisory lock, an exact setting snapshot, book ownership, provider concurrency, and spend
limit are rechecked and a durable reservation is inserted before provider I/O. Measured usage settles
to the owner/book ledger; failures and missing usage settle conservatively. A setting change aborts
before the call. Runtime table privileges are limited to the reads/inserts/updates required for this
flow.

Every answer reports input/output tokens, call provider/model, payer, and measured advisory USD.
Known prices are shown as such. An unknown model is explicitly labelled “price unavailable” rather
than being presented as free; token and concurrency ceilings still apply. When no evidence exists,
no provider call is made and the zero cost is explicit.

## Consequences

- The model never receives passages beyond the completed server frontier, and every public claim is
  traceable to a navigable returned citation.
- “The completed pages do not establish this yet” is a normal safe outcome, including local offline
  stub mode; the system does not guess to maximize answer rate.
- Hosted questions are owner-scoped through HTTP, repository predicates, RLS, runtime locks, settings,
  credentials, reservations, and ledger rows.
- Retrieval quality limits answer recall. A relevant passage absent from the bounded top-six set can
  produce an insufficient answer even when the reader has encountered it.
- A process crash can leave a reservation conservative until operator reconciliation, matching the
  existing cost-ceiling policy. Advisory USD for an unknown model cannot enforce a precise spend cap;
  the UI directs the owner to the provider account instead of claiming a zero charge.
