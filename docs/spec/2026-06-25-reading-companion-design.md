# reading-companion — design spec

> **Implementation status (2026-07-13):** this is the original product design. The working MVP is on
> `main`; the original character-graph concept evolved into the full Codex and its DOM-native editorial
> relationship ledger. Current public status and setup instructions live in the repository README.

**Status:** historical approved direction; working MVP implemented (see the amendment above)
**Date:** 2026-06-25
**Historical project prefix:** `LIT` ("Litlet")

---

## 1. What we're building

A **spoiler-safe, growing reading companion**: a web app that is *also* an EPUB reader — "a reader with a brain." You read a book inside the app; as you read, it incrementally builds a structured **story memory** and offers views that re-orient you, **never revealing anything past your current bookmark.**

It is **not** a book summarizer. It is a **spoiler-safe memory of your own reading.**

### The user pain (the whole reason this exists)
You read ~200 pages of a long novel (the canonical test case: *The Brothers Karamazov*), put it down for a month, and come back lost — who's who, what the plot was, where you stopped. Re-skimming risks spoilers and is annoying. This tool makes coming back effortless.

### Users
- **v1:** the author/owner (personal use), reading classics and DRM-free books.
- **Later:** other people, via their own LLM key (BYO) or a paid subscription that uses the owner's hosted LLM.

---

## 2. Principles (non-negotiable)
1. **Spoiler-safe by construction** — not by the model "promising" to behave.
2. **Grows with you** — the map fills in as you read; you can rewind to any earlier point.
3. **As few steps as possible** — minimum friction; the app knows where you are because you read in it.
4. **Fast and not token-hungry** — efficiency comes from architecture, not brute force.
5. **Slick and precise UI** — perfection bar; this should feel like a reading tool, not a dashboard.

---

## 3. The engine

**The core move: don't store the book — store a structured *memory* of it.**

Ingestion is **incremental and bookmark-bounded**:
- Each chapter is processed **exactly once** (append-only / delta — never re-processed).
- Only chapters **up to the bookmark** are ever read. The model never sees ahead, so:
  - **Spoilers are impossible at the source** (it hasn't read the future), and
  - **Book length stops mattering** (the whole book is never in context at once — solves token/context limits).
- Per chapter, a model extracts **entities, events, places, relationships, themes** plus a **chapter summary**, written into a local store.
- **Views render from the store, not from raw prose.**

### Spoiler-safety by construction
Every fact/edge/event is stamped with `revealed_at` (the chapter that first revealed it). A bitemporal `invalid_at` marks facts later contradicted/superseded (instead of overwriting them). **Every read filters:**

```
revealed_at <= :bookmark AND (invalid_at IS NULL OR invalid_at > :bookmark)
```

That single filter *is* the spoiler rule. It also enables **time-travel**: render the story as of any earlier chapter.

### Entity resolution (kills long-book drift)
The #1 way long-book tools break is losing that *Alyosha = Alexei = Alexei Fyodorovich = "the youngest brother."* Mitigation:
- One **canonical record** per entity with an **alias/epithet list**.
- New mentions are embedded and **cosine-matched** to existing canonical entities (the iText2KG pattern); above threshold → merge, else create.
- A **running cast roster** is fed into each extraction prompt so the model links references forward instead of inventing duplicates.

### Efficiency
- **Two model tiers:** a cheap/small (or local) model does per-chapter extraction; a large model only does **synthesis** (recaps, notes), lazily.
- **Prompt caching** on the stable recap prefix (~90% off on repeats).
- **Append-only** ingestion → reopening after a month costs ~nothing; advancing the bookmark only processes new pages.

### Storage
- A single **local SQLite** file.
- **sqlite-vec** for embeddings in the *same* DB, **partitioned by chapter** so the spoiler filter pre-applies to vector search.
- **RAG** over bookmark-bounded chapter chunks for detail/quote-level questions.
- **LanceDB** is the scale-up path if we outgrow brute-force KNN.

### Provisional schema (to be finalized by a spike)
```
entities(id, canonical_name, type, first_revealed_at)
aliases(entity_id, surface_form, revealed_at)
edges(src, dst, rel_type, revealed_at, invalid_at NULL)
events(id, summary, chapter, order_idx)
chapter_state(entity_id, chapter, status_json)
chapters(idx, title, summary, processed_hash)
embeddings(vec0, partition_key = chapter)   -- sqlite-vec
```
Invariant: **every row carries `revealed_at`; all reads filter `<= bookmark`.**

---

## 4. The four views

All four are lenses on the one memory, all bounded by the bookmark.

1. **Catch me up (HERO).** The re-orientation moment. Rolling recap (cached) + "who you're following" (character records) + **"where things stand"** (open tensions = what was about to happen, *minus* the resolution) + the last beat. This is what earns the app.
2. **Character graph.** Nodes = characters (toggle places/themes); edges = labeled relationships (family / love / rivalry / allegiance). Click a node → spoiler-safe bio + key moments. **Bookmark scrubber:** drag backward to see the graph as it stood at an earlier chapter (uses `revealed_at`). Mostly free structured reads.
3. **Timeline.** Events as beats in **swimlanes** (per character / plotline); follow one thread or watch threads converge. Grows rightward; sealed past the bookmark. Later: split **story-time vs reading-order** for flashbacks.
4. **Chapter notes.** Per-chapter "what happened / who appeared / what it pays off / what it sets up" — where "sets up" only points at **already-read** chapters. The granular layer the others are built from.

**MVP build pair:** catch me up + character graph. Timeline + chapter notes follow (cheap once the memory exists).

---

## 5. Form factor & import

**A reader with a brain.** You read EPUBs in-app (split pane: book | companion). Reading position is automatic. We **stand on a mature EPUB reader engine** (epub.js / Readium / foliate-js) — we do not reinvent rendering. **Web first** (desktop/browser, matching the dev environment); backend/API split so a **mobile** client can reuse the backend later.

### Import policy (legal, settled)
- ✅ **Allowed:** DRM-free EPUBs the user owns (file/drag) + **one-tap public-domain classics** (Project Gutenberg via Gutendex + Standard Ebooks).
- ⚠️ **Partial / later:** Kindle **highlights** sync (~10%, via the read.amazon.com/notebook route, like Readwise) — never full text or live position.
- ❌ **Forbidden:** DRM-locked purchases (Kindle, Apple Books store, Adobe-DRM Kobo/Google/library). Importing them requires breaking DRM — **DMCA §1201, no personal-use exception** — so we do **not** build it.
- **Rule of thumb:** an actual `.epub` file you can point to imports; content sealed inside Kindle/Apple cannot.

---

## 6. Stack

- **Backend:** Python + FastAPI · SQLite (+ sqlite-vec) · structured extraction (Instructor/Pydantic).
- **LLM:** behind a **one-function pluggable interface** (BYO-key / hosted-sub / local Ollama) so v1 uses the owner key and BYO/subscription is a later config flip.
- **Frontend:** TypeScript + React · graph via **Cytoscape** (sigma/d3 alternatives to evaluate).
- **Why Python not Rust:** the bottleneck is LLM network latency, not CPU; Rust buys ~nothing on an I/O-bound workload and loses the Python AI ecosystem. Optimize real hotspots (embeddings, vector search) with native-backed libs only if profiling demands it.

---

## 7. How we build it (process)

**Research / decision spikes first, then build.** Each substantial spike ends in a recorded **ADR**
(`docs/adr/`) and updates `docs/DECISIONS.md`. The historical `LIT-*` identifiers remain as stable
cross-references in those records.

---

## 8. Precedents / prior art
OpenAI recursive book summarization (arXiv:2109.10862) · Microsoft GraphRAG · LightRAG · iText2KG entity resolution (arXiv:2409.03284) · Graphiti/Zep bitemporal memory (arXiv:2501.13956) · SCORE narrative state tracking (arXiv:2503.23512) · MemGPT/Letta tiered memory (arXiv:2310.08560) · Anthropic prompt caching. See `docs/research/`.

---

## 9. Glossary
- **Bookmark:** the reader's current position; the spoiler frontier.
- **Story memory:** the structured, chapter-stamped store the views render from.
- **revealed_at / invalid_at:** the bitemporal stamps that make reads spoiler-safe and time-travelable.
- **Catch me up:** the hero view — re-orientation on return.
