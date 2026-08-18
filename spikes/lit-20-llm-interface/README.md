# LIT-20 spike — pluggable LLM/embedding interface + versioning

Finalizes the **one provider-agnostic interface** (`../lit-6-extraction/llm.py`) as the contract and
adds the **version-pinning + safe-swap policy** that keeps it semantically safe. Closes the LIT-8
review gap (vectors carried no embedding-model stamp).

## Run

```bash
python3 spikes/lit-20-llm-interface/demo.py     # all checks pass
```

## The decision

The pluggable interface makes swapping a provider/model an **env change** — operationally trivial but
**semantically dangerous**: swap the embedding model and cosine silently compares two incompatible
spaces (no error); mix extractor models across a book and entity granularity drifts. So:

**PIN the extractor + embedding model per book at first ingestion** (stamped in `book_meta` + on every
vector). A later change is never silently mixed — it is detected and forces an explicit, costed
migration. The large/synthesis model is the one component free to change mid-book (synthesis is
stateless — it re-reads the bookmark-filtered facts each call).

## The interface contract

One tier-aware surface, backends auto-detected from the env (`../lit-6-extraction/llm.py`):

| | |
|---|---|
| `complete(system, user, tier, schema)` | tier ∈ {cheap, large}; `schema` forces structured output |
| `embed(texts)` | → `(vectors, usage)` |
| `version` / `current_identity()` | `{provider, extractor_model, synth_model, embed_model, embed_dim}` |
| backends | **anthropic**, **openai-compatible** (OpenAI/OpenRouter/Groq/Together/LM Studio/llama.cpp/Ollama), **stub** — raw HTTP, no SDK |

## The safe-swap matrix

| component | may change mid-book? | a change triggers |
|---|---|---|
| **synth (large) model** | YES — stateless, re-reads filtered facts | `OK` |
| **extractor (cheap) model** | NO — pinned (granularity drift) | `FORCE_RE_EXTRACT` (explicit, costed) |
| **embedding model** | NO — pinned (cosine across spaces is meaningless) | `FORCE_RE_EMBED` |
| **embedding dim** | NO — a dim change *is* an embed change | `FORCE_RE_EMBED` |
| **schema_version / prompt** | NO — append-only | `MIGRATE_SCHEMA` (LIT-19) |

## What the demo proves
1. One tier-aware interface resolves a cheap + large model for ≥2 real backends + stub (swap = env change).
2. The model identity is **pinned** in `book_meta` at first ingestion.
3. Every vector carries its `embed_model`; a KNN under a **different** embedding model returns **nothing**
   (no cross-space garbage), and a **re-embed migration** restores it.
4. The safe-swap policy returns the right decision per component (synth OK; extractor → re-extract;
   embed/dim → re-embed; schema → migrate).

## Schema delta (applied to LIT-5)
- `chunks`: `embed_model`, `embed_dim` (stamped by `add_chunk`); `search(..., embed_model=)` only
  compares same-model vectors.
- `book_meta`: `extractor_model`, `synth_model`, `embed_model`, `embed_dim` (set by `pin_models()`).

Full record: `../../docs/adr/0005-llm-embedding-interface-versioning.md`.
