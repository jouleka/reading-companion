# ADR 0005 — Pluggable LLM/embedding interface + provider/model versioning

**Status:** **Accepted** (2026-06-26) — interface contract finalized; per-book model pinning + the
embed-version stamp + the safe-swap policy implemented and validated by an executable proof; survived
**two** adversarial Opus passes (both FIX_THEN_ACCEPT). Pass 1 caught two BLOCKERs (embed backend not
separable from completion; `safe_swap` keyed on a bare model name) + enforcement gaps. Pass 2 confirmed
all six fixes hold and caught the **canary-determinism** trap (exact-hash would false-trigger on a real
embedder's float noise) + a late-pin recall hole — **all fixed and re-proven**; LIT-5/6/8 suites green.
**Date:** 2026-06-26
**Ticket:** LIT-20 (build-blocker).
**Spike code:** [`spikes/lit-20-llm-interface/`](../../spikes/lit-20-llm-interface/) — `versioning.py` (identity + safe-swap policy), `demo.py` (proof); the interface itself is [`spikes/lit-6-extraction/llm.py`](../../spikes/lit-6-extraction/llm.py).
**Builds on:** LIT-6's `llm.py` (the one-function interface) + ADR 0002 (the store; this ADR adds the embed-stamp/pinned-model columns).

## Context

The owner's directive: "any key, any provider, any subscription — like Hermes — or just use ours." The
pluggable interface (built in LIT-6) delivers that, but it makes swapping a model an **env change** —
operationally trivial and **semantically dangerous**. Swapping the **embedding** model silently breaks
entity-resolution + RAG (cosine across two embedding spaces is meaningless, with *no error*). Mixing
**extractor** models across a book yields inconsistent entity granularity. LIT-20 specifies the
interface contract and the versioning/safe-swap policy that closes those holes (and the LIT-8 review's
finding that vectors carried no embedding-model stamp).

## Decision

### 1. The interface contract (one tier-aware surface; embedding backend INDEPENDENT)
`llm.LLMClient`: `complete(system, user, tier, schema)` (tier ∈ {cheap, large}; `schema` forces
structured output) · `embed(texts) → (vectors, usage)` · `version` / `current_identity()` /
`embed_identity()` / `embed_canary()`. Completion backends auto-detected from the env: **anthropic**,
**openai-compatible** (OpenAI, OpenRouter, Groq, Together, LM Studio, llama.cpp, Ollama `/v1`), **stub**.
**The embedding backend is configured INDEPENDENTLY** of completion (`EMBED_PROVIDER` /`EMBED_MODEL`
/`EMBED_BASE_URL`/`EMBED_API_KEY`) — Anthropic has no embeddings API, so a real Anthropic deployment
pairs Anthropic completion with an openai-compatible embeddings endpoint, which this expresses. Raw
HTTP, no vendor SDK; a swap is an env change. **`embed()` never lies about which embedder ran**: with
no reachable embeddings endpoint it falls back to a lexical stub, WARNS loudly, and stamps the *actual*
identity `stub:lexical-stub-256` — never a configured-but-unhonored name. *(Rev-1 review BLOCKER #1.)*

### 2. Pin the model identity per book at first ingestion
`book_meta` stores `extractor_model`, `synth_model`, `embed_model`, `embed_dim` (+ existing
`schema_version`), set once by `pin_models()`. This is the per-book source of truth for what produced
its memory. *Rejected:* allow free mid-book swaps (silent granularity drift / broken cosine);
version-stamp-everything-and-lazily-migrate (more complex, defers the cost unpredictably). Pinning is
the simplest safe default.

### 3. Stamp the embedding identity on every vector + ENFORCE it
`chunks` carries `embed_model` (the **full** identity `provider@base_url:model`, not a bare name) +
`embed_dim`; `book_meta` carries the pinned identity + an `embed_canary` (a fingerprint of the
embedder's actual output). **Enforced, not advisory:** when a book is pinned, `add_chunk` **rejects**
a vector whose model/dim ≠ the pin (a wrong/truncated-dim or wrong-model vector is silent corruption);
`search` is **same-space by default** — with no `embed_model` it resolves the book's pinned model, and
filters `embed_dim` in SQL — so a cross-space query returns **nothing**, not garbage. *(Rev-1 review
HIGH #3/#4/#5 — pinning was advisory and search defaulted to scanning all models.)*

### 4. The safe-swap matrix
| component | may change mid-book? | a change triggers |
|---|---|---|
| **synth (large) model** | **YES** for spoiler-safety (stateless; re-reads filtered facts) — but the **recap cache must key on `synth_model`** (else an upgrade silently no-ops; LIT-8 `cache_key` does) | `OK` (+ recap-cache miss) |
| **extractor (cheap) model** | NO — pinned (granularity drift) | `FORCE_RE_EXTRACT` (explicit, costed) |
| **embedding model / endpoint** | NO — pinned (cosine across spaces is meaningless) | `FORCE_RE_EMBED` |
| **embedding dim** | NO — a dim change *is* an embed-model change | `FORCE_RE_EMBED` |
| **embedding *weights* (same NAME)** | NO — a stub-vs-real / base_url-repoint / silent re-train | `FORCE_RE_EMBED` via the **canary** |
| **schema_version / prompt** | NO — append-only | `MIGRATE_SCHEMA` (LIT-19) |

`versioning.safe_swap(pinned, current)` returns the decision(s). The embed identity is the **full**
`provider@base_url:model` + dim + an **`embed_canary`** (a fingerprint of the embedder's output), so a
**same-name space change** (stub-vs-real, base_url repoint, silent re-train) is still caught —
`safe_swap` keying on a bare name was rev-1 review BLOCKER #2. A re-embed migration (retract old-space
vectors → re-embed → **`repin_embedding()`** overwrite) restores KNN and leaves `safe_swap == OK` —
proven in the demo.

## Validation (`demo.py`, all checks pass)
1. One tier-aware interface resolves cheap + large for anthropic + openai-compatible + stub (swap = env change).
2. Model identity pinned in `book_meta` at first ingestion.
3. Vector carries `embed_model`; a KNN under a different embedding model returns nothing; re-embed restores it.
4. Safe-swap returns the right decision per component (synth OK; extractor → re-extract; embed/dim → re-embed; schema → migrate).
Downstream regression: LIT-5/6/8 suites all still PASS after the schema delta (columns are nullable + back-compatible).

## Adversarial review

### Pass 1 (2026-06-26) — verdict **FIX_THEN_ACCEPT**. 27 raw → consolidated; all fixed + re-proven.

| Sev | Finding (probe-verified) | Disposition |
|---|---|---|
| **BLOCKER** | **Embedding backend hard-coupled to completion** — an Anthropic book silently pinned + used the lexical stub as its embedder; configuring an embed model name stamped that name onto a stub vector (the stamp lied). | **FIXED.** `EMBED_PROVIDER`/`EMBED_MODEL`/`EMBED_BASE_URL`/`EMBED_API_KEY` configure embedding independently; `embed()` warns on stub fallback and `embed_identity()` always reports the *actual* embedder (`stub:lexical-stub-256`). |
| **BLOCKER** | **`safe_swap` called a space-corrupting swap SAFE** — embed identity was a bare model name, so stub-vs-real / base_url-repoint / silent re-train with the same name+dim returned `OK` → cross-space cosine. | **FIXED.** Embed identity is full `provider@base_url:model` + dim + an **`embed_canary`** fingerprint; any change forces `FORCE_RE_EMBED`. |
| HIGH | **Pin was advisory** — nothing enforced it; `add_chunk` never checked `book_meta`; a book could ingest with NULL pins; `safe_swap` was called only by the demo. | **FIXED.** `add_chunk` rejects a chunk whose model/dim ≠ the pin (when pinned); `search` resolves the pinned model by default. (Honest scope: the ingestion layer must call `safe_swap`/pin before writing — documented.) |
| HIGH | **`search(embed_model=None)` scanned ALL models** (unsafe default; safe behaviour opt-in). | **FIXED.** Default resolves the book's pinned model; `embed_dim` filtered in SQL. |
| HIGH | **`add_chunk` inferred `embed_dim=len(vec)`** with no validation → a truncated/garbled vector stored silently. | **FIXED.** When pinned, the dim/model must match the pin or `add_chunk` raises. |
| MED | **Recap cache key omitted `synth_model`** — "synth may change freely = OK" served the old model's cached recap. | **FIXED.** LIT-8 `cache_key` now includes `synth_model` (+ recap prompt version); matrix note updated. |
| MED | **`pin_models` COALESCE froze the first pin** — no re-pin; the demo migration didn't re-pin so `safe_swap` would report `FORCE_RE_EMBED` forever. | **FIXED.** `repin_embedding()` overwrites within the migration; the demo re-pins and asserts `safe_swap == OK` post-migration. |
| MED | **NULL `embed_model`** chunk invisible to a model-guarded search. | **FIXED.** When pinned, `add_chunk` stamps/validates the model; unpinned legacy (LIT-5/6/8 stub stores) keeps back-compat. |

Post-fix, `demo.py` PASSes all checks and LIT-5/6/8 suites remain green.

### Pass 2 (2026-06-26) — re-attack of the fixed code. Verdict **FIX_THEN_ACCEPT** → fixed.
All six pass-1 fixes **confirmed holding by probe** (embed separation + honest stub identity; canary
catches a same-name swap; `add_chunk` enforces the pin incl. a lying dim arg; same-space default search;
`repin_embedding`; LIT-8 `cache_key` keys on `synth_model`); no regression in LIT-5/6/8; no-bypass not
reintroduced (a raw `book_meta`/`chunks` read is still DENIED). Three MED items, all fixed:

| Sev | Finding (probe-verified) | Disposition |
|---|---|---|
| MED | **Canary determinism** — the exact-hash canary sat on a rounding boundary; ≥1e-8 per-dim jitter flipped it 200/200 trials. A real (GPU/batch) embedder's run-to-run noise (~1e-4) would false-trigger a costed full re-embed on every on-open check. | **FIXED.** The canary is now a **vector compared by COSINE** with tolerance (`< 0.999 → FORCE_RE_EMBED`): same embedder + noise = 0.99996 (OK), scaled space = 0.84, random = 0.02 (re-embed). Demo asserts both the catch and the noise-tolerance. |
| MED | **Late pinning hid pre-pin NULL-model chunks** — ingest unpinned, then pin → default same-space search silently returned 0 hits for the unstamped chunks (recall loss). | **FIXED.** `pin_models` **rejects** pinning an embed model when the book already holds unstamped chunks (pin before embedding, or re-embed). |
| MED | **ADR Outcome said "Accepted" while the header was Provisional / pass-2 pending.** | **FIXED.** Reconciled — Status + Outcome both Accepted now that both passes are recorded. |

## Consequences & routed follow-ups
- The embed-stamp / dim-guard prototype uses a JSON vector + Python cosine; the **real `vec0` build**
  must carry `embed_model`/`embed_dim` as partition/metadata and enforce the same single-space rule —
  coordinate with the vector spike (ADR 0002 open follow-up).
- `current_identity()` probes the embed dim via one `embed()` call; the real build should record the
  dim from the provider's model card where available to avoid a probe call.
- Cost/quota of the chosen models and huge-chapter handling are resolved by **LIT-21 / ADR 0010**.
  Re-extract/re-embed *execution*
  (the migration mechanics) → **LIT-7** (transactional) + LIT-19 (policy).
- This unblocks LIT-6's close-out: pin a real **cheap** extractor + a real **embedding** model, then
  re-run the LIT-6 + LIT-8 harnesses against them for the cheap-tier quality + real-embedding numbers.

## Outcome
A single tier-aware, provider-agnostic interface with **per-book model pinning**, an **embedding-model
stamp on every vector** (cross-space KNN returns nothing, not garbage), and a **safe-swap policy**
(synth free; extractor/embed/schema pinned with explicit migration) — validated by an executable proof
and with the LIT-5 schema delta applied + downstream suites green. **Accepted** for LIT-20; it is the
versioning spine the cheap-tier/real-embedding runs (LIT-6 close-out) plug into.
