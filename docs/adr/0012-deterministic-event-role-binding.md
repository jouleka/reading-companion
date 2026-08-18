# ADR 0012 — Deterministic high-consequence event-role binding

**Status:** Accepted (2026-07-13)
**Ticket:** LIT-27

## Context

LIT-25 requires recap sentences and clauses to be lexically traceable to bookmark-bounded supplied
facts. Its measured residual appears once a high-consequence word legitimately enters an unrelated
fact: at Karamazov bookmark 40, a visitor confesses to an old murder, after which `Fyodor was
murdered` and `Smerdyakov killed him` are lexically grounded even though neither binding is known.
The LIT-14 LLM judge catches this semantic substitution, but the judge is probabilistic and paid.

## Decision

The deterministic gate now extracts a narrow signature from explicit name-bearing claims:

`(visible entity identity, high-consequence event family, agent | patient)`

Every recap signature must be entailed by a signature extracted from the same bookmark-bounded
chapter summaries and events supplied to synthesis. The grammar covers active/passive voice,
possessive and `of` nominalizations, `by` agents, reflexive hanging, and these event families: death,
homicide, conviction, arrest, imprisonment, exile, execution, suicide, betrayal, shooting, stabbing,
poisoning, strangling, drowning, and hanging. Homicide/execution/suicide/hanging can entail the less
specific patient state `death`; a known death does not entail homicide.

Suspicion, allegation, accusation, possibility, and reported/supposed language is non-factive: it can
be repeated as an allegation but cannot license a concrete event claim. Exact multi-token name/alias
overlap coalesces historical duplicate entity rows. Relational aliases such as `Fyodor's first wife`
cannot steal another entity's name token, while a genuinely ambiguous shared first name fails closed
unless the facts use the same ambiguous surface.

The identity map and fact surface are read through `BookmarkView`; no future audit data enters the
binder. The result is wired into both `assert_recap_safe` and the structured merge-gate evaluation.
Rejections remain server-only diagnostics and use the existing generic API failure path.

## Consequences and limits

The known real-store attacks are rejected at bookmark 48; `Fyodor was murdered` becomes supported at
the first explicit fact at bookmark 55; `Smerdyakov killed him` remains conservatively unsupported in
the earlier suspicion window. The same real-store probe accepted all 366 chapter-summary/event records
when scored at their own reveal point. At the full 96-chapter fact surface, the pure binding pass is
approximately 17 ms on the acceptance machine.

This is deliberately not general-purpose NLI. Pronoun-only outcomes, long-distance coreference,
metaphor, euphemism, and event families outside the grammar remain with the fail-closed LLM judge.
False uncertainty regenerates or withholds a recap; it never widens the spoiler frontier.
