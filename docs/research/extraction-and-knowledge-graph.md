# Research: incremental extraction & story-memory store

*Source: research pass, 2026-06-25. Incremental knowledge/entity extraction over long narrative text, local-first.*

## Headline
**Delta-only LLM extraction into a single SQLite file, with every node/edge/fact stamped `revealed_at_chapter`** so spoiler-safety is a `WHERE revealed_at <= :N` filter, not application logic. Use a **cheap model + Instructor/Pydantic JSON-schema extraction** per new chapter, **embedding-cosine entity resolution** (iText2KG pattern) for one canonical entity across aliases, and **sqlite-vec** for vectors in the same DB. Reserve a large model only for summary synthesis. Local-first, ~free at the margin, never reprocesses.

## KG extraction + incremental merge
- **Microsoft GraphRAG** — entities/relationships + Leiden communities + hierarchical community summaries; incremental append (v0.5+). Heavyweight, expensive (~$4/doc). *Borrow the community-summary idea, not the whole thing.* https://github.com/microsoft/graphrag
- **LightRAG** — GraphRAG-class quality, true incremental insert + dedup, ~$0.15/doc, dual-level retrieval. *Closest off-the-shelf framework if we want one.* https://github.com/HKUDS/LightRAG
- **iText2KG** — Distiller→Entity→Relation→Integrator; **incremental entity matching by embedding cosine** against the global set. *Adopt the resolution algorithm; skip Neo4j.* https://arxiv.org/html/2409.03284v1 · https://github.com/AuvaLab/itext2kg
- **Graphiti** — **bi-temporal** (`valid_at`/`created_at`/`invalid_at`); invalidates rather than deletes superseded facts. *Models our "revealed-at" + "later contradicted" needs.* https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/

## Entity resolution / coreference (Alyosha = Alexei = "youngest brother")
Per chapter: extract mentions (surface form + type), embed, cosine-match to canonical entities above threshold → merge, else create. Keep an **aliases/epithets** table per canonical id. Pass a **running roster of known canonical names** into the extraction prompt so the LLM links forward references (cheap; prevents fragmentation). Refs: https://arxiv.org/pdf/2510.26486 · https://arxiv.org/pdf/2504.05767

## Cost-efficient extraction
- **Instructor + Pydantic** — schema-enforced JSON with auto-retry; works with Ollama/local models. Small model for extraction; large model only for synthesis. **Delta-only:** hash each chapter, skip if seen. https://github.com/567-labs/instructor

## Local embeddings + vector store
- **sqlite-vec** — pure-C, in-process, same `.db`. Brute-force KNN (fine at one-book scale); **partition keys pre-filter before vector compare** → partition by chapter for spoiler-aware KNN. *Recommended.* https://github.com/asg017/sqlite-vec
- Alternatives: **LanceDB** (embedded ANN, scale-up path); **FAISS** (fast but no persistence/metadata/filtering); **Chroma** (easy, heavier). For one local DB, sqlite-vec wins on simplicity.

## Spoiler-safe schema (SQLite)
`entities(id, canonical_name, type, first_revealed_at)` · `aliases(entity_id, surface_form, revealed_at)` · `edges(src, dst, rel_type, revealed_at, invalid_at NULL)` · `events(id, summary, chapter, order_idx)` · `chapter_state(entity_id, chapter, status_json)` · `embeddings(vec0, partition key = chapter)`.
**Invariant:** every row carries `revealed_at`; all reads filter `revealed_at <= :N AND (invalid_at IS NULL OR invalid_at > :N)`. Recaps/notes are derived views over that filtered slice → spoiler-safe by construction; timeline = order by chapter.

## Licensing note
EbookLib is **AGPL-3.0** — isolate it or roll a `zipfile`+`lxml` EPUB parser. Keep a UTF-8 text fallback ingest path.
