# Research: long-document LLM memory (efficiency + no drift)

*Source: research pass, 2026-06-25. How to keep coherent, token-frugal LLM understanding over 1000+ page books.*

## Headline
**Hierarchical rolling-summary "story state" + spoiler-bounded RAG, with prompt caching.** Do **not** feed the read-so-far text to the model each turn. Maintain a persisted, structured story state updated chapter-by-chapter (rolling summary + entity/relationship records), and answer views by retrieving from that state plus raw chapters — all gated by the bookmark. This is the OpenAI book-summarization tree run *incrementally*, fused with GraphRAG-style entity tracking and SCORE-style explicit state.

## Technique survey
- **Recursive / hierarchical summarization + rolling state** — summarize chunks, then summaries (OpenAI, arXiv:2109.10862); maintain a per-chapter summary + one global rolling recap. Cheap, bounded, naturally incremental; lossy (keep raw chapters as ground truth). **Core.**
- **RAG vs long-context** — embed read chapters, retrieve top-k instead of stuffing the book. Far cheaper; scales to any length; avoids lost-in-the-middle. 2025 consensus = hybrid (arXiv:2407.16833, 2409.01666). Index is bookmark-bounded → spoiler-safe. **Core for detail/quote views.**
- **Agentic memory (MemGPT/Letta, Mem0, Zep/Graphiti)** — tiered "virtual context" (core in-window, recall/archival paged in). Graphiti is **bitemporal** (event vs ingestion time), strong on facts-that-change (MemGPT arXiv:2310.08560; Zep arXiv:2501.13956). **Borrow the pattern** (core recap in-window, chapter store on disk; bitemporal stamps), not necessarily the framework.
- **Prompt / KV caching** — provider caches a stable prefix; reads ~10% of input price. Min ~1024 tokens, 5-min/1-hr TTL, prefix must be byte-identical. **Free win:** cache the story-state recap as a shared prefix across views in a session.
- **Narrative-specific state tracking** — SCORE (arXiv:2503.23512): symbolic per-entity state + hierarchical episode summaries + hybrid retrieval; EvolvTrip (arXiv:2506.13641): temporal character graphs. LLMs are near-random at state tracking unless state is **explicit** (+12–15 pts). **Adopt explicit per-entity records.**

## Recommended hybrid
1. **On bookmark advance (per chapter, once):** extract (a) chapter summary, (b) entity/relationship deltas, (c) timeline events → persist (SQLite + vector index). Embed raw chunks. *No reprocessing.*
2. **Rolling global recap:** fold new chapter summary into a length-capped running recap.
3. **Views:** catch-me-up = rolling recap (cached); graph/timeline = structured records (near-zero LLM); deep/quote = RAG over bookmark-bounded index + records.
4. **Spoiler safety:** every store/index hard-filtered to `chapter <= bookmark`.

## Failure modes → mitigations
- Entity drift / alias confusion → explicit per-entity records + alias list (SCORE/Graphiti).
- Forgetting early plot → structured records + RAG are permanent & queryable; don't trust the lossy recap for facts.
- Reprocessing cost → append-only per-chapter ingestion.
- Lost-in-the-middle → retrieve targeted records+chunks, don't dump full text.
- Summary error compounding → keep raw chapters retrievable as ground truth.
- Conflicting facts as story evolves → bitemporal/validity-windowed records.

## Sources
- OpenAI, Recursively Summarizing Books — https://arxiv.org/abs/2109.10862 · https://openai.com/index/summarizing-books/
- MemGPT — https://arxiv.org/abs/2310.08560 · Letta — https://www.letta.com
- Zep/Graphiti — https://arxiv.org/abs/2501.13956
- RAG vs long-context — https://arxiv.org/pdf/2407.16833 · https://arxiv.org/pdf/2409.01666
- GraphRAG — https://arxiv.org/html/2404.16130v2 · https://microsoft.github.io/graphrag/
- SCORE — https://arxiv.org/html/2503.23512v1 · EvolvTrip — https://arxiv.org/pdf/2506.13641
- Anthropic prompt caching — https://platform.claude.com/docs/en/build-with-claude/prompt-caching
