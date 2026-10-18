# Jev integration and reader performance

PR #12 merged on 2026-10-16 at the owner's explicit request. The performance follow-up replaces
serial semantic nomination and answer review with one independently reviewed selection of prepared
claims. Complete fresh answers still depend on preparation coverage; the universal 1.5-second
acceptance criterion has not passed.

## Local configuration

Use the ignored server environment; never expose credentials in frontend configuration.

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
READER_REUSE_FAST_REVIEW=true
READER_FAST_MODE=false
```

Supply `TYPESAFE_API_KEY` in the server environment, or use `JEV_WINDOWS_CREDENTIAL=true` with
the existing Windows DPAPI credential. Explicit Jev activation without credentials fails startup.
Hosted mode does not expose these local reader routes. Cache, preparation and reuse remain opt-in.

When reuse is enabled, `READER_REUSE_FAST_REVIEW` defaults to true for native OpenAI GPT-6 Luna
in local mode. An isolated client copy requests Fast processing, reasoning none and temperature
zero for the short selection/review call only. Generation, extraction, preparation and their
independent audits retain their configured models and reasoning. Other providers are unchanged.
The cost ledger reserves the Fast premium and settles the actual reported tier, including an
explicit Standard downgrade. Missing usage and failed calls remain conservatively charged.

## Evidence and spoiler boundaries

Retrieval sees completed primary text only. Current-scene windows prevent older similar scenes
from displacing the current occasion. Jev selects sources and screens evidence; it cannot grant
a final answer approval. Its whole-claim evidence checks require probability >=0.95 and confidence
>=0.85. Partial mode sends unresolved whole claims to GPT with exact complementary coverage.

Fresh generation retains positive citation audits, deterministic spoiler checks, independent GPT
own-citation/completeness review and a bounded repair. Additional context may reveal omissions;
it cannot positively support an uncited assertion. Any failed or uncertain gate withholds output.

Prepared reuse ranks at most twelve answers from the exact current scope. One GPT call first
checks the top four; a miss may check the remaining eight before normal generation. The reviewer
selects one response and a complete subset of its unchanged claims, copies their exact bytes,
then checks question premises, attribution, quantities, timing, identity, completeness and each
claim's own primary citations. Rewritten text, invalid indices, omitted details, unsupported
assertions, future references and ambiguous kinship labels fail closed. Bare subject pronouns
without conversational context cannot use this route.

A question using another name cannot silently borrow an identity from scene context. A bounded
candidate identity passage may be attached to the claim's own citations before independent
review; the reviewer must prove the link from those exact bodies. Named question subjects must
also occur in the returned own-proof bodies, excluding title prefixes. This necessary lexical
check is not identity approval. The semantic reviewer remains fallible.

## Caches, preparation and budgets

One atomic answer file holds at most 128 rows and 2 MiB. Keys bind the question, source snapshot,
frontier, reset epoch, book incarnation, atom-set version, provider/model and review policy.
Policy v8 invalidates earlier experimental approvals. Exact hits require no provider calls.
Aliases retain the original reviewed question; cache pressure evicts narrow aliases before their
complete source answer. Identical foreground requests share one bounded flight. Background work
has separate cancellation and ceilings. Cache fingerprint reads project metadata only; they
preserve the existing membership and content-change algorithm without loading source bodies/vectors.

Preparation plans varied questions from visible memory and whole current-primary windows.
Independent question review checks every premise before the ordinary complete-answer gates.
Answers retain short, separately cited related facts so narrower questions can reuse them.
A failed parent answer no longer suppresses an independently premise-reviewed follow-up.
Budget exhaustion stops further paid attempts. Cleared answers become available before recap
prewarming. Only cleared planned questions appear in `/prepared-questions`.

Preparation reserves one maximum-call token allowance and $0.05 for foreground use, each bounded
to a quarter of the book ceiling. Its separate default cap remains $0.25 per book. The default
lifetime input ceiling is now 8M tokens because long-book ingestion exhausted the former 2M
before preparation; explicit token overrides still win. Per-call, output, dollar and background
caps remain enforced atomically against settled and pending costs. No ledger is reset.

One consumer and an eight-book queue perform provider IO outside reading mutation locks.
Short snapshot checks prevent publication after reset, deletion, correction or supersession.
Plans persist in the same bounded answer file. Durable attempt markers prevent restart/scroll
spend loops; a changed allowance permits one new bounded attempt, resuming only matching stages.
Successful attempts remove the plan. Original data, credentials and reading position are preserved.

## Extraction and recaps

New identities need a source-observed name/alias or an earlier visible identity. An object
description may join exact source tokens in order within a short span; people, places and factions
retain exact name matching. This only admits a candidate to full independent review.
The extraction reviewer audits every exact field segment before the overall verdict. Every
source chunk must clear before atomic publication. Source chunk sizes account for the structured
output ceiling as well as input capacity; no text is dropped. One bounded grounding repair
remains available. Malformed or incomplete reviews do not become approvals.

Identity merging needs an observed narrative link and independent GPT review; embeddings cannot
approve a merge in Jev mode. Alias reveal ordinals preserve reading boundaries. Existing memory
is not silently rewritten.

Recaps use whole primary windows and bounded earlier checkpoints, never legacy summaries as
generation facts. Every sentence needs a positive unit/span audit and a separate own-citation
review. Any rejected batch withholds the whole recap. A bounded regeneration may use corrections.
The positive recap cache has its own 2 MiB limit and full source/model/policy identity.

## Verification and reproduction

Run repository CI, Ruff, pytest, dependency audit and high-severity Bandit. Live commands from
`backend/` use configured providers and isolated capped data:

```powershell
python scripts/real_book_audit.py --run --source-data-dir PATH --book-id BOOK_ID --env-file ENV --checkpoints 2 5 --preserve-token-limits --max-new-usd .20 --output real-book.json
python scripts/classic_books_audit.py --run --env-file ENV --output classics.json
python scripts/prepared_reuse_audit.py --run --env-file ENV --output reuse.json
```

The original-book audit uses SQLite backups, preserves historical spend, fingerprints original
files/reading state/costs, and removes its disposable copy. Each independent probe restores the
prepared bank so earlier probes cannot create later hits. The classic audit naturally ingests
complete public-domain chapters from Alice, Pride and Prejudice and A Scandal in Bohemia, capped
at $0.25 per book. Stored unread chapters contain provider-input canaries. Failed ingestion is
reported as failure, not replaced with hand-authored memory. Remove detailed traces/downloads
after diagnosis; retain this compact verification record.

Current measurements (2026-10-17):

- Previous six-frontier baseline: 19/24 answers, median 14.45 s, worst successful answer 32.44 s.
- Latest production API replay against seven independently prepared answers: all eight original
  questions/paraphrases reused complete claims in 1.17–1.46 s. Each required one fresh independent
  review. Own citations were manually checked against the primary passages. The unread-scene
  question refused in 5.05 s. New spend $0.01194; zero pending reservations; original fingerprint
  unchanged. This replays a fixed prepared bank, not a new ingestion or universal coverage claim.
- The 82-case synthetic review audit rejected all 55 negative cases and reused 26/27 positives;
  median 1.12 s, spend $0.03614, zero pending. These are development fixtures, not a measured
  real-book accuracy rate.
- Fresh Karamazov preparation at chapters 2 and 5 answered 8/8 independent questions, with 4/8
  prepared reuses (1.26–1.33 s). Overall median 3.96 s; worst 16.61 s. Both recaps returned and
  both future probes refused. New spend $0.11260; zero pending; original files and position
  unchanged. This run started before the final identity-binding and focused-planner revisions.
- Natural ingestion of three additional public-domain books completed Pride and Prejudice
  chapter 1: 4/4 questions answered, 3/4 prepared reuses (1.26–1.62 s), one cold answer at
  18.71 s, recap returned and future probe refused. Alice chapter 1, Pride chapter 2 and
  Sherlock chapter 1 failed ingestion; none are counted as successful coverage. Spend $0.09079,
  zero pending, no unread canary reached a provider. This run also preceded the final identity
  and planner revisions. Malformed segment audits and all-reasoning output exhaustion remain
  blockers. A separate low-reasoning extraction experiment still failed Alice's segment audit
  ($0.00809); production extraction/review settings were therefore retained.
- All seven CI checks passed on implementation commit `ea46e38`: 1,621 backend tests passed,
  85 skipped; 319 frontend tests passed, with build, PostgreSQL integration, dependency,
  security and secret checks green. Universal latency, fresh coverage and multi-book acceptance
  remain unpassed; the performance follow-up stays draft.

References: [TypeSafe API](https://docs.typesafe.ai/api),
[TypeSafe models](https://docs.typesafe.ai/models),
[GPT model parameters](https://developers.openai.com/api/docs/guides/latest-model),
[Fast processing](https://developers.openai.com/api/docs/guides/deployment-checklist).
