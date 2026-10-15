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
JEV_RECAP_ENABLED=false
RECAP_PREWARM=true
RECAP_DISK_CACHE=true
READER_ANSWER_CACHE=true
READER_PREPARE_ENABLED=true
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

`READER_PREPARE_ENABLED` prepares up to three likely chapter questions after the recap, using only
the most recent completed chapter's facts. Each answer uses the normal retrieval, evidence and
independent GPT review route. Only cleared answers appear in the existing Ask suggestions through
the read-only `/prepared-questions` endpoint; no model call is triggered by listing suggestions.
The preparation admission cap is $0.25 per book, including pending reservations and failed calls,
in addition to the ordinary book cap. Scope attempts are persisted to avoid repeated spending on
unchanged chapters after scrolling or restarting. These three optional reader flags remain off
by default; the local Jev profile enables them together.

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
python scripts/jev_reuse_audit.py --run --output reuse.json
python scripts/jev_reuse_audit.py --run --heldout --output reuse-heldout.json
python scripts/reader_preparation_audit.py --run --env-file /path/to/ignored/.env --output prepared.json
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

Current preparation verification: two live reviewed synthetic answers returned in 5–40 ms,
the full reviewed recap in 10 ms, and five page-position saves in 8–16 ms. Preparation cost $0.0021;
restart incurred no new provider spend and reset rejected advanced answers. The direct reuse sample
had 0 false accepts across 45 development and 24 held-out multi-candidate checks, accepting only
4 of 24 eligible paraphrases. That limited recall is intentional; these small samples do not
establish production accuracy or a universal 1.5-second guarantee.

Selective-review verification: 1,368 backend tests passed (83 optional/platform skips). Mixed
development and held-out checks covered 180 claim judgments with 0 unsupported fast approvals;
Jev cleared 98 of 120 supported claims. All 24 paired API responses passed the expected answer,
citation-frontier and refusal checks. A mixed three-claim explanation required only one GPT claim
audit. Overall median complete-response latency remained about 3 seconds in both modes; this is
a reduction in duplicated audit work, not a measured general speedup or a 1.5-second guarantee.

References: [TypeSafe API](https://docs.typesafe.ai/api),
[models and pricing](https://docs.typesafe.ai/models),
[confidence](https://docs.typesafe.ai/confidence),
[OpenAI Fast mode](https://developers.openai.com/api/docs/guides/fast-mode).
