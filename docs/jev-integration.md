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
JEV_RECAP_ENABLED=false
RECAP_PREWARM=true
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

The fallback is bounded by existing call/repair limits and durable book spend reservations. Jev
uses a fixed HTTPS endpoint, a pinned model version, no redirects and no automatic retries. Failed
paid attempts retain conservative charges. The tested price is $0.042/M input tokens; output is
free, while output-token limits remain enforced. Probabilities are routing signals, not a promise
of correctness.

## Recaps

`RECAP_PREWARM` prepares the normal guarded recap after successful durable ingestion. One background
consumer and an eight-book queue bound work. Preparation acquires the same request gate as reset,
position updates and deletion, rechecks the catalog incarnation, and recomputes the authoritative
frontier. It never trusts a queued bookmark, reads ahead, changes the reading position, or bypasses
review. Existing snapshot/model/prompt/atom-set cache keys, single-flight and negative-cache TTLs
still apply. Queued work is discarded at shutdown; active work finishes before stores close.

The hybrid profile strengthens recap generation/review against unsupported location, time and
causal attachments and withholds unsupported details. Its separate cache identity prevents reuse
of earlier approvals. This does not certify that old extracted memory is factually correct.

The experimental `JEV_RECAP_ENABLED` flag is **off by default**. In live calibration it escalated
most supported summaries to GPT and added cold-request overhead. Background preparation supplies
the useful recap speedup while retaining generative review. A cold request can still take seconds
if preparation has not finished. Caches remain process-local and bounded.

## Reproducing validation

Run the usual Ruff and pytest suites, then opt into paid synthetic checks:

```powershell
python scripts/jev_quality_audit.py --run --output quality.json
python scripts/jev_quality_audit.py --run --heldout --output heldout.json
python scripts/jev_integration_audit.py --run --env-file /path/to/ignored/.env --output paired.json
```

The direct challenge script requires the existing Windows credential; its combined admission cap
is $0.02 per run. The paired API sample uses a fresh disposable synthetic book, the current configured
GPT models, actual embeddings and a $1 durable per-book ceiling. It alternates provider order, records
complete claims/citations and call timings, and verifies that a reading reset cannot serve an advanced
cached recap. Inspect semantic results manually: term matches and a small sample are not accuracy
certification. Both scripts delete temporary databases and imported EPUBs when finished.

References: [TypeSafe API](https://docs.typesafe.ai/api),
[models and pricing](https://docs.typesafe.ai/models),
[confidence](https://docs.typesafe.ai/confidence).
