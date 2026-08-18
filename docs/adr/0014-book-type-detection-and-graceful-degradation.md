# ADR 0014 — Advisory book-type detection and graceful degradation

**Status:** Accepted (2026-07-13)

**Ticket:** LIT-9 (implemented from the local research record; live tracker reconciliation was login-blocked)

## Context

The memory schema and reader were designed around a conventional novel: extraction asked for a cast,
relationships, plot events, and chapter summaries; recap prose assumed one continuing story; and the
Companion/Codex displayed cast and thread furniture even when extraction had no grounded data. Those
assumptions are misleading for plays, collections, poetry, nonfiction, manuals, and unusual EPUBs.

Book type cannot be a spoiler authority. A detector may inspect structure the reader has not reached,
may be uncertain, and will misclassify some works. It therefore cannot change the EPUB atom set, the
bookmark frontier, fact visibility, completion receipts, generated-prose gates, or recovery rules.

## Decision

Run a deterministic, provider-free classifier once, after the existing bounded EPUB segmentation and
before publication. It scores only coarse structural/content signals and requires both a minimum score
and a margin over the runner-up. Supported results are `novel`, `anthology`, `drama`, `poetry`,
`nonfiction`, `reference`, and `unknown`; weak or conflicting evidence produces `unknown`.

The profile is advisory metadata:

- `book_type`
- bounded confidence in `[0, 1]`
- detector version
- at most twelve evidence codes from a closed, content-free vocabulary

The detector receives already-segmented chapters but never edits, drops, combines, or reorders them.
No classification result skips extraction or creates facts. The existing chapter/frontier alignment,
LIT-7 completion receipts, bookmark funnel, referential closure, runtime recap gate, and fail-closed
judge remain unchanged.

### Adaptive extraction and recap

The `novel` extraction and recap prompt paths remain byte-for-byte unchanged, and novel recap cache
identity remains `recap-v3`. Other profiles, including `unknown`, use neutral section language. They
allow grounded people, relationships, concrete developments, topics, and summaries, but explicitly
leave irrelevant arrays empty and do not invent narrative continuity or a stable cast. Collections get
an additional warning not to carry identities across independent works without textual evidence.

Generated non-novel recaps still receive only bookmark-bounded facts and pass the same deterministic
gate and fail-closed judge. Their cache key includes the profile-specific prompt version. Classification
does not authorize a fact, relax a gate, or expose detector input; only the generic profile is returned.

### Honest presentation

The reader maps profiles to presentation vocabulary. Novels retain the established chapter/story/cast
language. Drama uses action-oriented labels but calls atoms “sections” because the detector cannot
prove that every atom is a scene. Other and unknown profiles use section/reading-notes language.
People/thread statistics and the Codex people leaf are conditional outside profiles where people are
intrinsically useful. Chapter/section notes remain available even when no people were extracted, and a
short profile note explains the omitted panel rather than fabricating content.

### Persistence and recovery

Schema v4 adds the four profile fields to `book_meta`. Existing schema-v3 stores migrate to an explicit
legacy `novel` profile with zero confidence and a `legacy-novel-v1` marker because their existing facts
were produced with the novel prompt; silently reclassifying them without re-extraction would create a
mixed contract. Fresh imports replace that default with the deterministic result.

Exact and portable backups include the profile. Schema-v2 and schema-v3 archives are accepted only in
verification/restore staging, rebuilt at their declared shape, imported, and forward-migrated to v4.
The archive and source store are never rewritten. Migration v4 is shape-aware for repair/replay. When
the running service successfully opens and migrates a per-book database, it also advances that book's
catalog schema marker so a later backup cannot see mismatched recovery metadata.

## Acceptance and degradation cases

- conventional long chaptered narrative → `novel`, with unchanged prompts, labels, and recap cache;
- repeated act/scene plus speaker/stage cues → `drama`;
- sonnet/ode/canto structure with short sections → `poetry`;
- repeated story/tale work headings → `anthology`;
- lesson/exercise/glossary structure → `reference`;
- weak, absent, or competing signals → `unknown` and neutral behavior;
- no extracted people or relationships in a non-novel → notes remain useful and empty novel-oriented
  panels are omitted/explained;
- any classification, including a wrong one → identical atoms, frontier, receipts, data constraints,
  and spoiler gates.

## Consequences and limits

- Detection adds no provider call or provider cost and persists no title/prose excerpt as evidence.
- Coarse heuristics cannot reliably distinguish all narrative nonfiction, essay collections, hybrid
  works, or misleading headings. These may become `unknown` or another neutral-capable profile.
- A migrated store intentionally remains legacy `novel` until an explicit future reclassification and
  re-extraction workflow exists. LIT-9 does not retrofit already-published memory.
- Type-specific schemas are deferred. All profiles retain the existing storage schema, using empty
  arrays where concepts do not apply; usefulness may be lower, but data is not discarded.
- Segmentation quality remains governed by ADR 0001/0011. LIT-9 does not repair a missing or weak ToC;
  it degrades the interpretation and UI of whatever non-empty atom set the bounded segmenter produced.
