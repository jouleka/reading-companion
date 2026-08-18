"""LIT-6 — the per-chapter extraction pipeline that wires the LLM client to the spoiler-safe store.

Productionized from ``spikes/lit-6-extraction/`` per ADR 0003 + ADR 0007 (D-A3/D-A5/D-A11) and
DECISIONS D17. Group (a) pure cores (``resolve.py``) are lifted near-verbatim; ``schema.py`` /
``prompts.py`` / ``chapter_text.py`` / ``pipeline.py`` are re-ported with the named, behaviour-relevant
changes ADR 0007 calls out (Pydantic schema source-of-truth, the productionized segmenter parse path,
the append-once early-return re-sourced off ``chapter_is_ingested``).
"""
from app.ingest.extraction.chapter_text import (
    chapter_texts, content_hash_of, segment_for_ingest,
)
from app.ingest.extraction.pipeline import all_entities, ingest_chapter, prepare_chapter
from app.ingest.extraction.prompts import (
    EXTRACT_SYSTEM, extract_user_prompt, roster_for_prompt,
)
from app.ingest.extraction.resolve import resolve_chapter, resolve_one
from app.ingest.extraction.schema import (
    Entity, EntityType, Event, Extraction, RelType, Relationship, Theme,
)

__all__ = [
    "Entity", "EntityType", "Event", "Extraction", "RelType", "Relationship", "Theme",
    "EXTRACT_SYSTEM", "extract_user_prompt", "roster_for_prompt",
    "resolve_one", "resolve_chapter",
    "chapter_texts", "segment_for_ingest", "content_hash_of",
    "prepare_chapter", "ingest_chapter", "all_entities",
]
