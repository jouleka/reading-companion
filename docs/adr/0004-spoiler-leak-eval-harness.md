# ADR 0004 — Spoiler-leak eval harness (structured reads · RAG · synthesis · cache)

**Status:** **Accepted** (2026-06-26) — *for the harness as a deliverable*: implemented, green across
all four vectors on the Karamazov set, **falsifiable** (planted leaks in every vector are caught), and
hardened through an adversarial Opus review (FIX_THEN_ACCEPT; all must-fixes applied — see below). The
*production spoiler-safety verdict* it produces remains **measured-risk / pending**: the deterministic
gate covers structured/RAG/cache + future-named-entities + future-tense, but a paraphrased future
*event* in past tense with no name rests on the soft LLM-judge (deterministic NLI gate routed to the
build), and synthesis was exercised with a book-aware model (cheap-tier run → LIT-6/LIT-20). The
harness is the runnable gate; closing those residuals is downstream work, not a gap in the harness.
**Date:** 2026-06-26
**Ticket:** LIT-8 (build-blocker). Also the gate that closes out LIT-6's paraphrased-fact spoiler-safety and gates the MVP views (LIT-14/15).
**Spike code:** [`spikes/lit-8-spoiler-eval/`](../../spikes/lit-8-spoiler-eval/) — `harness.py` (deterministic eval + synthesis scorer), `synth_measure.py` (synthesis over-reach), `synth_facts.json`, `synth_results.json`, `README.md`.
**Builds on:** [ADR 0002](0002-bitemporal-schema-and-dal.md) (the store + spoiler filter), [ADR 0003](0003-extraction-and-entity-resolution.md) (the extraction it scores).

**Post-acceptance amendment (2026-07-13):** LIT-25 added deterministic lexical sentence grounding;
LIT-27 added explicit entity/event-role binding. The routed residual described below is therefore
closed for name-bearing high-consequence claims. Implicit coreference and open-domain NLI remain under
the fail-closed LLM judge. See [ADR 0012](0012-deterministic-event-role-binding.md).

## Context

Spoiler-safety is the product's whole differentiator, and the sharpest leak vectors are NOT
extraction-time filtering (already covered by LIT-5) but the **RAG/quote path** (a bookmark-bounded
chunk can foreshadow) and the **synthesis model's generative over-reach** (paraphrasing beyond the
supplied facts using its own knowledge of the book). LIT-8 builds the harness that proves
spoiler-safety end-to-end and measures the residual leak/over-reach rate, so the MVP views can ship
behind a gate rather than a hope.

**Ground truth is automatic:** every fact in the LIT-5 store carries `revealed_at`, so at bookmark
`B` the forbidden set is exactly `{fact : revealed_at > B}`. A leak = any output surfacing a
forbidden fact. (No separate hand-labeling needed for the structured/RAG paths.)

## Decision

**A hybrid harness** (matching the ticket's leaning): deterministic forbidden-fact assertions for the
structured + RAG + cache paths, plus a deterministic future-entity post-gen check AND an LLM-judge for
the synthesis paraphrase path.

### Vector 1 — structured reads (deterministic)
For every bookmark and **every** `BookmarkView` read method — `characters`, `relationships`,
`timeline`, `themes`, `chapter_summaries`, **`aliases_of`, `current_state`, `events_for`, `bio`,
`raw_text`**, plus `catch_me_up`'s recap — assert no returned row **or referenced entity** has
`revealed_at > B`. Re-validates LIT-5 referential closure as a scored eval. **Result: 1877 reads, 0
leaks.** Falsifiable, not a no-op: a late entity ("the elder Zossima", rev 4) is hidden@2 / appears@4,
**and** a monkeypatch that drops the spoiler clause for the `aliases` table makes the harness report
**6343 leaks** — proving the extended paths are genuinely leak-checked (the alias path is
auto-falsified in-CI; the others re-assert `revealed_at <= B` per returned row through the same funnel).

### Vector 1b — reveal-correctness (independent of the DAL filter)
The structured/RAG vectors validate filter *consistency* (`revealed_at <= B`) against the store's own
`revealed_at` — which is **circular** if the extractor *mis-stamped* `revealed_at`. So an independent
check asserts each named entity's name first appears in the **prose** of a chapter ≤ its `revealed_at`
(lexical first-mention against the retained raw text). **Result: 27 named entities, 0 mis-stamps** —
an independent confirmation the reveal boundaries are correct, not just self-consistent.

### Vector 2 — RAG / quote path (deterministic + analysis)
`search()` must never return a chunk with `revealed_at > B`. **Result: 60 retrievals, 0 leaks.**
In-text foreshadow inside an *already-read* chunk ("…his tragic death, which I shall describe later")
is **reader-parity-safe** — the reader read that sentence too, so returning the chunk reveals nothing
new; 36 such chunks are flagged (informational), not failed. The genuine residuals are (a) **sub-chapter
position** — a chunk read only partway → **LIT-12**, and (b) **synthesis elaboration** of a foreshadow
→ covered by vector 3.

### Vector 3 — synthesis over-reach (hybrid)
A **grounded-only prompt** (recap may use ONLY the bookmark-bounded supplied facts; never refer to
later events) + three checks on the generated recap:
- **Deterministic — future entity name (hard):** a recap word matching a token that names an entity
  revealed *only later*. Matched **case-insensitively** (so a lowercased diminutive "sofya"/"zossima"
  is caught) but keyed on proper-noun tokens of the canonical, **minus** tokens shared with a visible
  entity (surname "Karamazov") **and minus tokens the reader has already read** (reader-parity: e.g.
  "monastery", capitalized in the future place "Optin Monastery" but read earlier — not a spoiler).
- **Deterministic — prolepsis (hard):** a future-tense modal ("will", "would later/eventually", "is
  destined to", "was to become") — a structural tell of a paraphrased future *event* that the
  entity-name check can't see. Narrowed to modals so it does NOT fire on past narration that merely
  uses "eventually".
- **LLM-judge (soft + hard):** `references_future` is a **hard** blocker; paraphrase over-reach
  (`unsupported_claims`) is a soft, reported signal. Fail-closed: a recap with no judge verdict counts
  as unsafe.

**Honest limit (review HIGH #1):** a future event stated in **past tense with no name** (e.g. "the
family is torn apart by a killing") is caught by *none* of the deterministic checks — only the soft
judge. The deterministic gate covers future *named entities* + future-tense prose, **not arbitrary
future events**; a deterministic NLI/span event-grounding gate is **routed to the build**.

**Result on 3 real grounded-only recaps (bookmarks 1/3/5, via the agent harness):** **0 hard
future-entity leaks, 0 prolepsis hits, 0 judge-flagged future references.** The LLM-judge surfaced
**11 soft over-reach claims** — mild characterization restatement (the bm-3 recap called Ivan "proud",
only in a later chapter's facts; bm-5 "universally loved", which the judge itself noted is supported by
cumulative events). Hard checks block; soft over-reach is surfaced, and would tighten the prompt /
add retrieval-grounding in the build.

### Vector 4 — recap-cache coherence
Cache key = `(book_id, bookmark, validity_snapshot)`, where the snapshot **hashes the live
`(id, invalid_at, retracted_at)` set visible at B**. So a retroactive `invalid_at` (story-time
supersession backdated to ≤ B) **or** a re-extraction (transaction-time retraction) that changes what
is valid at B flips the key → cache miss → regenerate. **Result: the snapshot changes on both a
retraction and a retroactive `invalid_at`** (verified); a stale cached recap can never be served.

## Honest scope / NOT proven
- **Synthesis used a strong book-aware model** (no cheap backend in the env). The hard future-entity
  check is model-tier-independent, but cheap-tier over-reach behaviour is unproven → **LIT-20**.
- **The LLM-judge is itself an LLM** (judge error is possible); it is paired with the deterministic
  check precisely so spoiler-blocking never rests on the judge alone (the judge is a soft signal).
- **Sub-chapter spoiler frontier** (partially-read chunk) → **LIT-12**.
- **Sample** = 5 Karamazov chapters, one translation; broader corpus / book types → **LIT-9**. RAG was
  tested with the lexical-embedding stand-in (real `vec0` recall → LIT-20/vector spike).

## Adversarial review (2026-06-26) — verdict **FIX_THEN_ACCEPT** → all must-fixes applied

3 lenses (leak-miss · coverage · falsifiability) → verifier; 17 raw findings, each reproduced with a
probe. It confirmed the structured/RAG/cache guarantees but found the first draft **over-stated** the
synthesis "hard" guarantee and had coverage/cache/metric gaps. Findings & dispositions:

| Sev | Finding (reproduced) | Disposition |
|---|---|---|
| HIGH | The deterministic synthesis gate was **blind to future EVENTS** paraphrased without a future proper noun ("Dmitri will eventually be murdered… sent to Siberia" passed clean) — so spoiler-blocking *did* rest on the soft judge for the most important class. | **FIXED + honestly scoped.** Added a **prolepsis** future-tense tripwire (hard) + promoted the judge's `references_future` to **hard**; corrected the ADR/README to state the deterministic gate covers future *named entities + future-tense*, not arbitrary events; routed a deterministic NLI/span event-grounding gate to the build. |
| HIGH | A future entity named by a **lowercased** form bypassed the capitalization-keyed detector ("sofya" missed, "Sofya" caught). | **FIXED.** Future-entity matching is now **case-insensitive** (whole-word) against canonical proper-noun tokens. |
| MED | `structured_eval` **omitted read paths** (`aliases_of`, `current_state`, `events_for`, `raw_text`, `bio`, `catch_me_up`) — a DAL regression there was invisible; "1002 reads/every method" overstated coverage. | **FIXED.** All paths now scored (1877 reads) + a monkeypatch **falsifiability** proof per the extended paths; wording corrected. |
| MED | `validity_snapshot` **omitted tables** (aliases, chunks, raw_chapters, event_participants) and ignored in-place `reextract_entity` → a stale cached recap could be served. | **FIXED.** Snapshot now spans all fact tables with a **content fingerprint** (`recorded_at`/`content_hash`), so an in-place re-extraction, new alias, re-chunk, or raw-text edit flips the key. |
| MED | **Circular ground truth** — the forbidden-set and the DAL filter both read `revealed_at`, so an extractor mis-stamp leaks through undetected. | **FIXED (independent signal added).** Vector 1b reveal-correctness checks each name's first prose appearance ≤ its `revealed_at` (0 mis-stamps); the consistency-vs-correctness distinction is documented. |
| LOW | Vacuous-pass risk (empty store / missing judge counted as clean); single capitalized canary. | **FIXED.** Non-vacuity asserts (reads/retrievals > 0), fail-closed on a missing judge, and 3 canaries (capitalized / lowercased / prolepsis) each asserted-caught. |
| LOW | Harness's own canary comment mislabeled Zossima's reveal chapter. | **FIXED** (canaries rewritten). |

Post-fix, `harness.py` and `synth_measure.py` both PASS every check.

### Rev 2 (2026-06-26) — re-attack of the FIXED harness. Verdict **FIX_THEN_ACCEPT** → fixed.
A second pass (the same two-pass bar LIT-5/LIT-6 got) confirmed every rev-1 fix holds by probe, and
caught **two real regressions my rev-1 fixes introduced** — both latent on the shipped inputs (so the
suite stayed green), both now fixed and re-proven:

| Sev | Regression (reproduced) | Disposition |
|---|---|---|
| MED | **`superior` false-flag** — case-insensitive matching + `_proper_nouns` not applying the role-noun stop-list made the future entity "the Superior" forbid the token `superior`, so a grounded recap ("…his claim was *superior*…") failed. Fail-*safe* (over-block) but breaks clean recaps. | **FIXED.** Future tokens now subtract `ROLE_NOUNS`; asserted a "superior" recap no longer flags. |
| MED | **Reader-parity was fail-*open*** — it dropped any future token present in earlier prose regardless of entity, so a future name colliding with a common earlier word ("Town" ← lowercase "town" in ch1) was silently un-flagged (*can leak*). | **FIXED (now fail-safe).** A future token is dropped only if (a) the reader saw it as a **capitalized proper noun** ("Russia") or (b) it is a common word **and the entity keeps another distinctive token** ("Optin Monastery" keeps "optin"). The **sole** distinctive token of a future entity seen only lowercase is never dropped. Asserted: a future "Town" IS caught. |

LOW nits (accepted, fail-safe): the prolepsis tripwire over-blocks "his will"/"about to" (over-block,
never leak); only the alias path is *auto*-falsified in-CI (the other extended paths route through the
same `_select` funnel and are re-asserted per-row, but aren't each monkeypatch-proven). The
**name-collision residual** (a future entity whose *sole* name is a never-before-seen common word not
in `ROLE_NOUNS`) now fails *safe* (over-block) rather than open; defense-in-depth: synthesis also has
the hard LLM-judge `references_future`. Post-rev-2, both scripts PASS every check.

## Real-model close-out (2026-06-26 — `gpt-4o` via LIT-20)
The synthesis vector was re-run with a **real** model (`spikes/lit-8-spoiler-eval/synth_live.py` →
`synth_measure.py`), replacing the agent-harness stand-in. Result: the deterministic checks were clean
(0 future-entity, 0 prolepsis), but the **LLM-judge hard gate fired at bookmark 5** — the recap added
*"sets the stage for an impending family gathering at the monastery, fraught with tension"*, a
forward-looking framing with **no future-entity name and no future-tense modal** (so it slipped past
both deterministic checks). This is the paraphrased-future over-reach class made concrete: real, and
caught only by the judge backstop. Mitigation, then re-proven clean: an **anti-foreshadow synthesis
prompt** ("describe only what has happened; do not foreshadow / set the stage") + the judge hard-gate →
all 3 recaps PASS (0 hard leaks / 0 prolepsis / 0 judge-flagged future refs). `SYNTH_SYSTEM` updated to
that wording. This empirically validates the hybrid guardrail (deterministic + judge-as-hard-gate) on a
real model — and confirms the residual is mitigated by prompt + gate, not left to chance.

## Consequences & routed follow-ups
- This harness is the **gate** for the MVP views (LIT-14 catch-me-up, LIT-15 graph) and for closing
  out LIT-6's paraphrased-fact spoiler-safety — run it in CI on every extraction/synthesis change.
- **LIT-12** owns the sub-chapter frontier the RAG vector flags. **LIT-20** plugs the cheap/embedding
  backend the synthesis + RAG checks should be re-run against. **LIT-9** broadens the corpus.
- Build-time hardening implied: a retrieval-grounded synthesis (feed the recap writer only retrieved
  bookmark-bounded sentences) would shrink the soft over-reach further.

## Outcome
A hybrid spoiler-leak eval harness proving **0 hard leaks** across structured reads, RAG, and
synthesis, with a **validity-snapshot cache-invalidation rule**, a **falsifiable** metric (planted
leaks are caught), and a **measured** synthesis over-reach rate (0 hard / 11 soft paraphrase claims
surfaced by the LLM-judge). It is the runnable gate that lets the MVP views ship spoiler-safe. The
cheap-tier / sub-chapter / broader-corpus / past-tense-event gaps are honestly out-of-harness-scope
and routed to LIT-20/12/9 + a build-time NLI gate. **Accepted** for LIT-8 (the harness); the
production spoiler verdict it computes is measured-risk pending the cheap-tier run.
