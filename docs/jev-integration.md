# Jev integration

The local FastAPI service can use TypeSafe Jev for source selection and positive-evidence audits.
The configured generative provider still writes prose, extracts chapter memory, and independently
reviews every answer against its own citations. Hosted mode does not expose these local routes.

Enable in the ignored server configuration:

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

Supply `TYPESAFE_API_KEY` through the server environment. On the Windows account that already saved
the encrypted TypeSafe credential, `JEV_WINDOWS_CREDENTIAL=true` reads that existing current-user
DPAPI file directly into memory. It never exports the credential to the browser, a plaintext file,
or a shell command. Missing credentials fail startup when Jev is explicitly enabled. No new runtime
dependency is required.

## Answer and closeout policy

1. Existing deterministic retrieval fences the candidate pool to completed chapters and exact
   normalized source slices. Jev sees that pool only; source selection cannot authorize an answer.
2. Jev evaluates the whole original claim and every immutable contextual unit against that claim's
   own original citations. Both the selected probability (at least 0.95) and confidence (at least
   0.85) must pass for **every** question. The normal ordered evidence-coverage validator commits
   existing source-span IDs; Jev cannot rewrite claims or invent references.
3. Uncertain, negative, overlarge, timed-out, malformed, or unavailable Jev evaluations use the
   existing budgeted GPT selector/auditor. A rejection is never silently converted to approval.
4. The independent GPT own-citation reviewer and all deterministic spoiler/citation gates remain
   mandatory, including when Jev returns a falsely confident approval.

`JEV_PARTIAL_EVIDENCE_ENABLED` narrows the GPT evidence audit to unresolved **whole claims** when
Jev clears some claims but remains uncertain about others. A claim is eligible only when its whole
composition and every original contextual unit pass against its own original citations. There are
no individual clause approvals. GPT receives the original unresolved claim/unit IDs, all selected
completed sources and the full unchanged answer as context, without Jev's verdicts. Other claims
are context, never proof. The deterministic merge requires exact complementary coverage and valid
source spans before the full independent own-citation GPT judge. Missing, overwritten, renumbered
or forged reviews fail closed; unsupported claims remain rejections. Outages, overlarge batches
and zero confident claims retain the ordinary complete audit. The flag is off by default and on in
the tested local profile, including its background answer preparation. Cache identity includes it.

The fallback is bounded by existing call/repair limits and durable book spend reservations. Jev
uses a fixed HTTPS endpoint, a pinned model version, no redirects and no automatic retries. Failed
paid attempts retain conservative charges. The tested price is $0.042/M input tokens; output is
free, while output-token limits remain enforced. Probabilities are routing signals, not a promise
of correctness.

`READER_FAST_MODE` uses native OpenAI Fast processing for Ask and Catch Me Up only, with the same
GPT-6 Luna medium / GPT-6.1 Sol low reasoning settings and independent own-citation review. It disables
SDK retries. Extraction, embeddings and their version pins keep
their existing clients. Fast tokens cost twice Standard; reservations and displayed costs include
that premium. The flag requires native GPT-6 Luna or GPT-6.1 Sol for both generative roles.
It remains off in the local profile: the higher-priced tier did not meet the complete-answer target.

`READER_ANSWER_CACHE` reuses complete cleared answers in a bounded 128-entry cache, checkpointed
in one atomic file capped at 2 MiB. Shape, checksum and full identity must match after restart. Keys
include the question, effective frontier, reset epoch, incarnation, validity snapshot, model/endpoint
and review configuration. A hit has zero new provider cost. Refusals and rejected drafts are not
cached. Cold explanations still require sequential generation and independent review: **there is
no verified 1.5-second guarantee for complete fresh answers**. Jev selects/checks; it does not write
explanations. No quote-only response or first-token timing substitutes for a complete answer.

`READER_REUSE_ENABLED` asks Jev whether a new question requests the same facts as an original
reviewed question **and** whether its unchanged complete answer covers every requested detail.
Both independent decisions require probability >=0.99 and confidence >=0.90. Up to four candidates
are shortlisted within the exact same source/frontier/model scope; word overlap never approves a
match. Ambiguous pronouns, new premises, extra questions and uncertain results fall back to normal
Ask. Aliases retain the original reviewed question, preventing chains of paraphrases from drifting.
The lookup has a 0.65-second provider timeout and makes no retries. Exact hits need no Jev call.

`READER_PREPARE_ENABLED` prepares up to eight questions by default (`READER_PREPARE_MAX_QUESTIONS`,
1–10) after the recap. A maximum twelve-question plan covers returning characters, established
relationships, events/objects, explicitly stated motives, one summary and up to two self-contained
follow-ups. Deterministic ordering covers available categories before second questions; unsupported
categories are never required. The context includes the last two completed chapters' summaries and
events, relevant visible cast/aliases, state snapshots and established ties. Whole fields interleave
under an 18,000-byte cap (or half the call budget); omitted/oversized fields cannot supply proof.
No future memory, expiry dates or whole-book oracle enter planning.

A separate independent structured question review checks every original question ID, its premises,
answerability and fact references. Invalid coverage/references fail closed; rejected parents drop
their follow-ups. Every answer still uses normal retrieval, Jev evidence checks where enabled, and
independent own-citation review. A follow-up runs only after its parent's complete answer clears.
Only explicitly premise-reviewed questions with cleared answers appear in Ask suggestions through
the read-only `/prepared-questions` endpoint. Cached foreground questions and semantic aliases do
not become suggestions automatically. Approval survives restart in the existing bounded checkpoint;
listing suggestions makes no model calls and rechecks the current source scope and frontier.

The preparation admission cap is $0.25 per book, including pending reservations and failed calls,
in addition to the ordinary book cap. Scope attempts are persisted to avoid repeated spending on
unchanged chapters after scrolling or restarting. Attempt identity includes the planner version
and configured question count, allowing one bounded upgrade attempt while preserving existing
reviewed answers. Preparation requires answer caching. Optional reader flags remain off by default;
the local Jev profile enables them with eight-question admission. Broader coverage is still bounded
by available evidence, successful reviews and spend; eight answers are not promised at every chapter.

## Chapter extraction screening

`JEV_EXTRACTION_ENABLED` adds a negative-only screen before the full extraction review. It can
request the existing single whole-chapter repair when both the whole field and at least one
contextual segment are confidently unsupported (probability >=0.99 and confidence >=0.90).
It cannot approve memory: every accepted extraction still needs the complete independent GPT
field/segment evidence audit and an exact source/payload clearance before atomic commit.

The screen prioritizes summaries, themes, relationship bindings, aliases and states. It packs whole
fields under the 24-question and conservative 30,000-byte admission limits, preserving the complete
current source and prior identity labels. No future chapter or prior actions are supplied. Fields
that do not fit remain for GPT; no truncation or omitted-field approval is allowed. Ambiguity,
outages, invalid replies and oversized input fall back to full review. Charges, including failures,
are recorded separately by chapter under `jev-extraction-screen`; extraction receipt costs and
the ordinary book ceiling remain intact. Shutdown and budget failures cannot publish partial data.

The flag is off by default and enabled by the local launcher. It applies to newly extracted chunks;
existing completed memory is not rewritten or silently re-extracted. This can avoid a slow review
of an obviously bad candidate, but adds a small screening call to eligible candidates. It is an
accuracy/early-repair feature, not a demonstrated general latency improvement.

## Character identities and aliases

`JEV_IDENTITY_ENABLED` handles unresolved names during background chapter ingestion. Exact known
names, aliases and narrow honorific matches remain deterministic. In this mode embeddings cannot
authorize an identity merge. A new name needs its own observed canonical surface and a distinct,
uniquely identifying earlier roster name observed in the current chapter. Shared aliases, pure
role epithets, different types and pending same-chapter identities cannot supply that anchor.

Jev nominates at most one of four eligible candidates, requiring probability >=0.99 and confidence
>=0.90. Every nomination then needs a separate GPT `IdentityVerdict` citing valid current-source
passages containing both name anchors. The reviewer sees no Jev labels or confidence. Only an
explicit narrative identity binding is allowed; resemblance, impersonation, quoted allegations,
denial, pronouns and familiar plot knowledge cannot establish it. An accepted name is reused
within that preparation without another paid check. All accepted extractions also retain their
full independent field/segment grounding review.

Missing evidence, uncertain or multiple nominations, outages, invalid citations and oversized
input leave identities separate. Complete source/roster input must fit the conservative byte cap;
it is never truncated. At most eight paid nominations are admitted per chapter attempt, including
stale-roster retries. Separate chapter ledger phases `jev-identity` and `identity-review` enforce
the normal book ceiling. Budget/shutdown failure cannot publish partial memory.

Provider calls run outside store/lifecycle locks. Link clearances bind exact chapter text, extracted
identity and working roster; the final resolution clearance binds every prepared decision. Commit
also rechecks the earlier visible roster, forcing fresh preparation after a correction. Aliases
retain the chapter reveal ordinal, so a reset cannot expose a later alias. The flag is off by
default and enabled in the local launcher. Completed books are preserved; this does not repair
older mistaken merges. This is an accuracy/background-preparation feature, not a fresh-Ask speedup.

## Recaps

`RECAP_PREWARM` prepares the normal guarded recap after successful durable ingestion. One background
consumer and an eight-book queue bound work. Startup also schedules up to eight existing books.
Page reports notify this same coalesced scheduler; repeated reports within a completed chapter
do not launch more model calls. The scheduler takes a short snapshot lease and releases it before
provider IO, allowing position saves, resets and deletion to proceed while preparing. It validates
incarnation, reset epoch and frontier atomically before paid reservations, and validates the full
source snapshot before publishing. A source correction schedules a fresh snapshot, including when
it arrives during an active job. It never trusts a queued bookmark, reads ahead, changes the reading position, or bypasses
review. Existing snapshot/model/prompt/atom-set cache keys, single-flight and negative-cache TTLs
still apply. Queued work is discarded at shutdown; active work finishes before stores close.

The hybrid profile strengthens recap generation/review against unsupported location, time and
causal attachments and withholds unsupported details. Its separate cache identity prevents reuse
of earlier approvals. This does not certify that old extracted memory is factually correct.

The experimental `JEV_RECAP_ENABLED` flag is **off by default**. In live calibration it escalated
most supported summaries to GPT and added cold-request overhead. Background preparation supplies
the useful recap speedup while retaining generative review. A cold request can still take seconds
if preparation has not finished. `RECAP_DISK_CACHE` checkpoints only cleared positive recaps in one
atomic file capped at 2 MiB. Shape, checksum and full cache identity must match after restart;
corruption is a miss. Cached and prior-recap frontiers are checked again before use. Failures and
active calls are never persisted. A reset cannot reuse an advanced recap.

## Reproducing validation

Run the usual Ruff and pytest suites, then opt into paid synthetic checks:

```powershell
python scripts/jev_quality_audit.py --run --output quality.json
python scripts/jev_quality_audit.py --run --heldout --output heldout.json
python scripts/jev_quality_audit.py --run --partial --output partial.json
python scripts/jev_quality_audit.py --run --partial --heldout --output partial-heldout.json
python scripts/jev_quality_audit.py --run --extraction --output extraction.json
python scripts/jev_quality_audit.py --run --extraction --heldout --output extraction-heldout.json
python scripts/jev_identity_audit.py --run --env-file /path/to/ignored/.env --output identities.json
python scripts/jev_identity_audit.py --run --heldout --env-file /path/to/ignored/.env --output identities-heldout.json
python scripts/jev_identity_audit.py --run --pipeline-only --env-file /path/to/ignored/.env --output identities-pipeline.json
python scripts/jev_reuse_audit.py --run --output reuse.json
python scripts/jev_reuse_audit.py --run --heldout --output reuse-heldout.json
python scripts/reader_preparation_audit.py --run --env-file /path/to/ignored/.env --output prepared.json
python scripts/reader_preparation_audit.py --run --extraction --env-file /path/to/ignored/.env --output extraction-prepared.json
python scripts/reader_preparation_audit.py --run --broader --extraction --env-file /path/to/ignored/.env --output broader-prepared.json
python scripts/reader_preparation_audit.py --run --broader --reviewed-fixture --env-file /path/to/ignored/.env --output broader-controlled.json
python scripts/reader_preparation_audit.py --run --premises --env-file /path/to/ignored/.env --output premises.json
python scripts/reader_preparation_audit.py --run --premises --heldout --env-file /path/to/ignored/.env --output premises-heldout.json
python scripts/jev_integration_audit.py --run --env-file /path/to/ignored/.env --output paired.json
python scripts/jev_integration_audit.py --run --partial --repeat 2 --env-file /path/to/ignored/.env --output partial-paired.json
python scripts/jev_integration_audit.py --run --reader-fast-mode --repeat 3 --env-file /path/to/ignored/.env --output fast.json
```

The direct challenge script requires the existing Windows credential; its combined admission cap
is $0.02 per run. The paired API sample uses a fresh disposable synthetic book, the current configured
GPT models, actual embeddings and a $1 durable per-book ceiling. It alternates provider order, records
complete claims/citations and call timings, and verifies that a reading reset cannot serve an advanced
cached recap. It includes a complete multi-fact explanation and separately measures identical cached
answers. Inspect semantic results manually: term matches and a small sample are not accuracy
certification. Integration scripts delete temporary databases and imported EPUBs when finished.

Broader preparation verification: forty frozen question-premise judgments had zero unsupported
approvals, accepting 19/20 supported questions. The natural live fixture prepared five complete
reviewed answers in 2–4 ms, kept page-position saves below 7 ms, incurred no new provider spend
after restart, and rejected advanced answers after reset. Preparation cost $0.0056. A separate
controlled extraction mode isolates planning while retaining actual independent extraction
grounding, question review and full answer review; it is not an extraction-quality evaluation.
The direct reuse sample
had 0 false accepts across 45 development and 24 held-out multi-candidate checks, accepting only
4 of 24 eligible paraphrases. That limited recall is intentional; these small samples do not
establish production accuracy or a universal 1.5-second guarantee.

Selective-review verification: 1,368 backend tests passed (83 optional/platform skips). Mixed
development and held-out checks covered 180 claim judgments with 0 unsupported fast approvals;
Jev cleared 98 of 120 supported claims. All 24 paired API responses passed the expected answer,
citation-frontier and refusal checks. A mixed three-claim explanation required only one GPT claim
audit. Overall median complete-response latency remained about 3 seconds in both modes; this is
a reduction in duplicated audit work, not a measured general speedup or a 1.5-second guarantee.

Extraction-screen calibration: 68 frozen development/held-out cases caught 17/34 bad candidates
with 0/34 false rejections of supported candidates. Missed errors still receive the full review.
The live synthetic ingestion/preparation trial verifies that screen calls run, complete extraction
reviews remain mandatory, no unread facts reach either provider, and restart/reset safeguards hold.
These small samples do not certify factual correctness or real-world false-rejection rates.

Identity verification: 44 frozen development/held-out cases produced 0 false merges, accepting
7/12 explicit valid links. The live pipeline used hand-authored extractions to isolate resolution,
with actual budgeted Jev nomination, independent GPT extraction/identity reviews and embeddings.
It committed one character with a newly revealed alias, while the earlier view had no alias.
Fifty-one offline tests cover thresholds, ambiguity, malformed/outage responses, citations, mutation,
pending identities, spend limits, shutdown and stale-roster preparation. Small synthetic samples
do not certify real-world identity accuracy or improve the unmet universal 1.5-second target.

References: [TypeSafe API](https://docs.typesafe.ai/api),
[models and pricing](https://docs.typesafe.ai/models),
[confidence](https://docs.typesafe.ai/confidence),
[OpenAI Fast mode](https://developers.openai.com/api/docs/guides/fast-mode).
