# Jev integration

This local-only integration uses TypeSafe Jev for closed-set selection and evidence judgments.
The configured generative provider still writes prose, extracts memory, and independently reviews
answers. Hosted mode does not expose these routes. **The PR remains draft: live latency, answer
completeness and fresh-book availability have not met acceptance.**

## Local configuration

Use the ignored server environment; never put credentials in frontend configuration.

```dotenv
JEV_ENABLED=true
JEV_MODEL=jev-1.13.0
JEV_SELECTION_ENABLED=true
JEV_EVIDENCE_ENABLED=true
JEV_PARTIAL_EVIDENCE_ENABLED=true
JEV_EXTRACTION_ENABLED=true
JEV_IDENTITY_ENABLED=true
JEV_RECAP_ENABLED=false
RECAP_PREWARM=true
RECAP_DISK_CACHE=true
READER_ANSWER_CACHE=true
READER_PREPARE_ENABLED=true
READER_PREPARE_MAX_QUESTIONS=8
READER_REUSE_ENABLED=true
READER_FAST_MODE=false
```

Supply `TYPESAFE_API_KEY` in the server environment. On the Windows account with the existing
encrypted credential, `JEV_WINDOWS_CREDENTIAL=true` reads it directly into memory through DPAPI.
Explicitly enabling Jev without credentials fails startup. No credential is exported or downloaded.

All optional reader flags remain off by default. The tested profile uses the current configured
GPT models and Jev 1.13.0. Fast mode is optional native OpenAI processing for reader calls; it keeps
the existing reasoning/review requirements and reserves its price premium. It remains disabled
because it did not establish the complete-answer latency target.

## Evidence and spoiler boundaries

- Retrieval sees completed primary text only. Current-chapter candidates are reserved so older
  similar scenes cannot displace the current event. Jev selection grants no answer approval.
  Up to four bounded packets retain exact candidate text and IDs; overflow/outage falls back
  to the ordinary budgeted GPT selector.
- Jev positive-evidence checks require the entire original claim and every immutable contextual
  unit against that claim's own citations (probability >=0.95, confidence >=0.85).
  Partial mode sends unresolved **whole claims** to GPT. Exact complementary coverage and valid
  source-span IDs are required; no clause can disappear during the merge.
- Every fresh answer then receives a separate GPT review of its own citations, attribution,
  quantities, scene and completeness. Additional selected/current-scene context may expose an
  omission or mismatch, but cannot supply positive support for an uncited claim. Structured
  `missing_details` diagnoses omissions and enters the existing bounded repair path. This is
  a fallible semantic check; a passing verdict is not proof of completeness.
- Named future entities, prolepsis and unsupported bindings remain deterministic rejection gates.
  No failure, uncertain judgment or provider outage becomes an implicit approval. A refused
  future question and a failed HTTP call are different outcomes.

Jev uses the fixed HTTPS endpoint, pinned model, no redirects and no automatic retries. Local
generative and embedding clients also disable SDK retries: each reservation covers one wire
attempt, while explicit application repairs obtain separate reservations. Failed paid attempts
retain conservative charges; settled and pending costs share the ordinary token/USD ceilings.

## Cached answers and preparation

The answer bank holds at most 128 complete reviewed answers in one atomic file capped at 2 MiB.
Keys bind question, source snapshot, frontier, reset epoch, book incarnation, atom-set version,
models/endpoint and review policy. Invalid shapes/checksums/versions are misses. The completeness
policy invalidates older approvals; they are never relabeled as newly reviewed. Exact hits make
no provider calls. Simultaneous identical foreground questions share one result or failure;
at most eight distinct flights are admitted. Background cancellation/ceilings are kept separate.

Semantic reuse shortlists four candidates from that exact scope. Jev checks both the new
question's premises and complete coverage; neutral yes/no questions do not assert their premise.
The strict >=0.99 probability / >=0.90 confidence threshold remains mandatory for both decisions.
**Every production semantic reuse also needs one fresh independent GPT own-citation review of
the unchanged answer and new question**. A lower-confidence nomination cannot bypass the strict
gate through a GPT fallback. Jev's labels are not given to that reviewer. Missing details,
a new premise, ambiguous occasion or
invalid review causes a miss. Historical-only answers need an explicit matching chapter.
Aliases retain the original reviewed question to prevent chains of paraphrases. Exact cache
hits remain distinct from this paid semantic check.

Preparation makes up to the configured 1–10 questions from a bounded visible fact pack, including
whole primary windows from the latest chapter. Character, relationship, event, motive, summary and
follow-up coverage are balanced when supported. A separate question review checks every premise
and reference before the normal complete answer route. Only cleared questions/answers appear in
the read-only `/prepared-questions` endpoint; foreground answers do not automatically become suggestions.

The planner requests only the configured count and reserves at most 4,096 output tokens; question
review uses at most 3,072. Both are clipped to the configured per-call ceiling, including reasoning.
The background allowance defaults to $0.25 per book in addition to ordinary lifetime ceilings.
It reserves foreground headroom of up to four call token limits and $0.05, each capped at a quarter
of the corresponding book limit. It cannot reset historical usage or raise a production allowance.

One consumer, an eight-book queue and short snapshot leases keep provider IO outside reading
mutation locks. Reader focus can pause preparation; resets, deletion, corrections and shutdown
invalidate stale publication. Every paid reservation checks scope atomically. In-flight calls
settle normally before shutdown. Plans persist in the same 2 MiB file, at most sixteen total and
one per incarnation. Failed stages retain their completed plan/review work. A durable attempt
marker prevents restart/scroll spending loops; an explicit allowance change admits one new bounded
attempt and may resume stages only when the exact fact/source/review scope still matches.
Successful attempts remove the plan. Source or policy changes cannot reuse stale stage approvals.

## Extraction, identities and recaps

New identities require a source-observed name/alias or earlier visible identity. An unnamed spouse
does not authorize an inferred surname. Bounded private repair feedback identifies rejected paths
and forms; reader-facing errors omit those names. Existing completed memory is preserved.

The optional Jev extraction screen is negative-only: a confident unsupported whole field and
segment can request the existing single whole-chapter repair. Every acceptance still needs the
full independent GPT field/segment review and exact source/payload clearance before atomic commit.
Dense reviews split only the requested whole-field list into bounded groups (at most sixteen),
retaining the entire source, roster and extraction context in each separately reserved call.
Missing coverage, a rejected final group or a budget failure cannot publish partial memory.
Oversize, outage and ambiguous screening fall back to full review. No partial memory is published.

For unresolved identities, embeddings cannot approve merges in Jev mode. At most four candidates
and eight paid nominations per chapter attempt are allowed. A nomination needs a distinct observed
new name and unambiguous prior name, followed by a separate GPT review citing the explicit current
narrative identity link. Similarity, roles, allegations and remembered plot are insufficient.
Clearances bind source, identity and roster; commit rechecks the earlier visible roster. Alias
reveal ordinals preserve the reading boundary. Existing old merges are not silently rewritten.

Recap generation uses whole primary windows from recent chapters and bounded earlier checkpoints.
Legacy summaries can rank local windows but cannot supply generation facts. This bounded sample
is not an exhaustive digest. Every original sentence receives a positive GPT unit/span audit;
adjacent claims share a call only within six sources, three claims and the estimated output budget.
A separate own-citation GPT review remains mandatory. Invalid coverage or any rejected batch
withholds the entire recap. One regeneration may use exact source corrections. The current-chapter
line reuses an unchanged cleared sentence. Persistent positive recaps use a separate bounded
2 MiB file and full source/model/policy identity; failures and flights are never persisted.

## Reproducing live validation

Run Ruff, pytest, dependency audit and Bandit, plus repository CI. From `backend/`:

```powershell
python scripts/real_book_audit.py --run --source-data-dir PATH --book-id BOOK_ID --env-file ENV --max-new-usd .25 --output real-book.json
python scripts/real_book_audit.py --run --source-data-dir PATH --book-id BOOK_ID --env-file ENV --checkpoints 2 --preserve-token-limits --max-new-usd .05 --output headroom.json
python scripts/classic_books_audit.py --run --env-file ENV --output classics.json
python scripts/jev_reuse_audit.py --run --heldout --output reuse.json
```

The Karamazov audit reads the original through SQLite backups, fingerprints files/reading state/
costs, and destroys its disposable copy. It preserves historical spend while adding a bounded
test-only token allowance unless `--preserve-token-limits` is selected. Frozen independent questions
cover six frontiers; unread memory canaries test provider inputs. Each probe restores the prepared
bank, excluding hits created by earlier probes. Generated suggestion clicks are separate controls.

The classic audit imports complete public-domain chapters from Project Gutenberg: Alice in
Wonderland (#11), Pride and Prejudice (#1342), and A Scandal in Bohemia (#1661). It uses natural
extraction with the normal gates, at most $0.25 per book. Unread chapters remain stored with
canaries. Frozen question/reference pairs precede planning; reset/restart checks follow each book.
Ingestion failures are reported as failures, not silently replaced by hand-authored memory.
Temporary databases and EPUBs are removed. Delete disposable downloaded texts and detailed traces
after inspecting them; retain only a compact current verification record.

Compare complete responses and citations with primary text. HTTP 200, keyword overlap, synthetic
fixtures and exact suggestion timing do not establish semantic accuracy, useful preparation
coverage or the <=1.5-second complete fresh-answer target.

## Current verification

Takeover checks on 2026-10-16 (current configured providers, no production allowance increase):

- The six-checkpoint diagnostic replay returned 19/24 independent answers: median 14.45 s,
  worst successful answer 32.44 s, and a failed request took 85.37 s. No returned answer met
  1.5 s. One independent question reused preparation in 10.54 s. Exact suggestion controls
  took 157–235 ms with zero provider calls; they are not independent-query coverage.
- Five of six future-scene probes cleanly refused. The sixth hit a token ceiling and is
  **not** a successful spoiler test. No future citations or unread canary inputs were observed.
  Two recaps cleared, three were withheld, and one hit the budget ceiling. New spend was
  $0.180, including $0.102 of preparation. Original fingerprints were unchanged; pending
  reservations were zero, reset hid advanced answers, and restart added no spend.
- That broad replay began before the final planner and instruction-wording repairs. Its chapter
  2/5 plans exhausted the old 3,072-token cap. The final focused checks included both previously
  omitted details: service in the world (49.22 s) and the legal-age condition (11.08 s).
  These two passes do not establish general completeness or latency acceptance.
- The original book still has only 159,794 input tokens below its two-million lifetime ceiling,
  less than the 240,000-token foreground reserve. Background preparation correctly remains
  blocked there. No original ledger, setting, credential or reading position was changed.

The final fresh-book run stopped on chapter one of all three books: Alice's extraction was
rejected, Pride and Prejudice exhausted the review output cap, and Sherlock's repair retained
an unobserved name. No independent answer probes ran on those books; this is failed ingestion
coverage, not successful multi-book spoiler/accuracy validation. The run cost $0.0304, left no
pending reservations, and passed each disposable book's reset/restart check.

Local verification: 1,581 backend tests passed / 83 optional or platform skips; the subsequently
added audit-accounting regression passed in a nine-test focused run. Ruff, high-severity Bandit,
dependency audit and diff checks passed. The final safety adjustment passed 83 focused tests.

The corrected planner produced eight premise-reviewed questions and three cleared suggestions
at chapter 2. Four independent questions returned, with two reuse hits around five seconds and
a clean future refusal. Manual review found one hit incorrectly replacing a mother's cousin
with the child's 'maternal cousin', despite model approval. That is an accuracy failure, not
successful reuse coverage. The lower-confidence fallback was removed, the original strict
threshold restored, and the review rubric now explicitly rejects changed kinship generations.
Cache policy v5 invalidates the experimental approvals. A fresh provider check rejected this
exact counterexample in 7.40 s, with zero pending reservations and unchanged original fingerprints.
That focused negative check does not establish general reuse accuracy. Final-commit CI results
are recorded on the PR. The release gate remains closed.

References: [TypeSafe API](https://docs.typesafe.ai/api),
[models and pricing](https://docs.typesafe.ai/models),
[confidence](https://docs.typesafe.ai/confidence).
