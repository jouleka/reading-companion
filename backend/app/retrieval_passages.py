"""Deterministic source windows shared by lexical retrieval and the local semantic index.

Offsets address whitespace-normalized chapter prose. Titles enrich the embedding input only;
they never become part of a quoted source passage. Changing this contract requires a new version.
"""
from __future__ import annotations

import hashlib
import json

from app.cost.limits import CostCeilingExceeded

PASSAGE_CHARS = 1500
PASSAGE_OVERLAP = 250
TITLE_CONTEXT_CHARS = 256
PASSAGE_INDEX_VERSION = "full-source-v1"


def normalize_source(text: str) -> str:
    return " ".join((text or "").split())


def passage_windows(text: str):
    """Yield start/end/text covering every character of normalized prose, with overlap."""
    text = normalize_source(text)
    start = 0
    while start < len(text):
        end = min(start + PASSAGE_CHARS, len(text))
        if end < len(text):
            boundary = text.rfind(" ", start + PASSAGE_CHARS // 2, end)
            if boundary > start:
                end = boundary
        yield start, end, text[start:end]
        if end == len(text):
            break
        next_start = max(start + 1, end - PASSAGE_OVERLAP)
        boundary = text.find(" ", next_start, end)
        start = boundary + 1 if boundary >= 0 else next_start


def embedding_text(title: str, passage: str) -> str:
    # Title enrichment must not turn one bounded prose window into an oversized embedding input.
    # Source evidence is always the full exact prose window, independent of this context label.
    title = normalize_source(title)[:TITLE_CONTEXT_CHARS]
    return f"Chapter: {title}\n\n{passage}" if title else passage


def source_digest(title: str, text: str) -> str:
    payload = [PASSAGE_INDEX_VERSION, normalize_source(title), normalize_source(text)]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()


def source_passages(text: str) -> list[dict]:
    return [{"start": start, "end": end, "text": passage}
            for start, end, passage in passage_windows(text)]


def embedding_batches(title: str, passages: list[dict], input_limit: int):
    """Plan all batches before IO using the exact conservative budgeted_embedding estimate."""
    if isinstance(input_limit, bool) or not isinstance(input_limit, int) or input_limit < 33:
        raise CostCeilingExceeded("embedding input ceiling cannot fit a source passage")
    batches, batch, size = [], [], 32
    for passage in passages:
        length = len(embedding_text(title, passage["text"]).encode("utf-8"))
        if length + 32 > input_limit:
            raise CostCeilingExceeded("embedding input ceiling cannot fit a source passage")
        if batch and size + length > input_limit:
            batches.append(batch)
            batch, size = [], 32
        batch.append(passage)
        size += length
    if batch:
        batches.append(batch)
    return batches


def prepare_passages(title: str, text: str, embed_fn, *, input_limit: int = 60_000):
    passages = source_passages(text)
    for batch in embedding_batches(title, passages, input_limit):
        values = embed_fn([embedding_text(title, passage["text"]) for passage in batch])
        if len(values) != len(batch):
            raise ValueError("source embedding count does not match passage count")
        for passage, vector in zip(batch, values, strict=True):
            passage["vec"] = vector
    return passages
