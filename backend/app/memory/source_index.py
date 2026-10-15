"""Trusted local derived-index operations; provider IO is deliberately absent.

Only the canonical chunks participate in search. Staged batches remain guarded and invisible until
one chapter transaction publishes exact source coverage together with the vec0 mirror.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from app.llm import versioning
from app.retrieval_passages import PASSAGE_INDEX_VERSION, source_digest, source_passages

from . import vectors


def _source(mem, chapter_key, ordinal, *, require_completion=True):
    with mem._writer():
        row = mem._conn.execute(
            "SELECT c.title,c.content_hash,c.revealed_at,r.text,r.content_hash AS raw_hash,"
            "r.revealed_at AS raw_ordinal FROM chapters c LEFT JOIN raw_chapters r "
            "ON r.chapter_key=c.chapter_key AND r.book_id=c.book_id AND r.retracted_at IS NULL "
            "WHERE c.book_id=? AND c.chapter_key=? AND c.retracted_at IS NULL",
            (mem._book_id, chapter_key),
        ).fetchone()
    if row is None or row["revealed_at"] != ordinal:
        raise ValueError("source index requires a live chapter at the exact ordinal")
    text = row["text"] or ""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    if (row["content_hash"] != digest or row["raw_hash"] not in (None, digest)
            or row["raw_ordinal"] not in (None, ordinal)):
        raise ValueError("source index requires verified unchanged raw chapter text")
    if require_completion and mem.chapter_completion(chapter_key, ordinal, digest) is None:
        raise ValueError("source index requires a durable completed chapter")
    title = row["title"] or ""
    return {"title": title, "text": text, "source_hash": source_digest(title, text),
            "passages": source_passages(text)}


def _identity(mem, identity):
    pinned = mem.pinned_identity()
    if not pinned or not pinned.get("embed_model"):
        raise versioning.ModelMigrationRequired([("embed_model", versioning.FORCE_RE_EMBED)])
    versioning.assert_compatible_identity(pinned, identity, check_extractor=False)
    return pinned


def _validated_passages(expected, passages, dimension, *, subset=False):
    if not isinstance(passages, list) or (not subset and len(passages) != len(expected)):
        raise ValueError("source index passage coverage is incomplete")
    by_start = {item["start"]: item for item in expected}
    seen = set()
    for index, item in enumerate(passages):
        target = by_start.get(item.get("start")) if subset else expected[index]
        if (target is None or any(item.get(key) != target[key] for key in ("start", "end", "text"))
                or item["start"] in seen):
            raise ValueError("source index passage is not an exact source window")
        seen.add(item["start"])
        value = item.get("vec")
        vectors.normalize(value)
        if len(value) != dimension:
            raise ValueError("source index vector dimension does not match the book pin")
    return passages


def snapshot(mem, chapter_key, ordinal):
    source = _source(mem, chapter_key, ordinal)
    pin = mem.pinned_identity() or {}
    with mem._writer():
        rows = [dict(row) for row in mem._conn.execute(
            "SELECT text,vec,embed_model,embed_dim,revealed_at,source_version,source_hash,source_start,source_end "
            "FROM chunks WHERE book_id=? AND chapter_key=? AND retracted_at IS NULL "
            "ORDER BY source_start,chunk_id", (mem._book_id, chapter_key),
        )]
        marker = mem._conn.execute(
            "SELECT * FROM retrieval_index_chapters WHERE book_id=? AND chapter_key=? "
            "AND retracted_at IS NULL", (mem._book_id, chapter_key),
        ).fetchone()
        staged = [dict(row) for row in mem._conn.execute(
            "SELECT batch_id,passages_json,embed_model,embed_dim FROM retrieval_index_batches "
            "WHERE book_id=? AND chapter_key=? AND index_version=? AND source_hash=? "
            "AND retracted_at IS NULL", (mem._book_id, chapter_key,
                                        PASSAGE_INDEX_VERSION, source["source_hash"]),
        )]
    # Portable exports deliberately omit local staging metadata. Canonical provenance is enough to
    # recognize an already-published nonempty index after portable restore, without paying again.
    complete = bool(rows or marker) and len(rows) == len(source["passages"])
    for row, passage in zip(rows, source["passages"]):
        complete = complete and (
            row["source_version"] == PASSAGE_INDEX_VERSION
            and row["revealed_at"] == ordinal
            and row["source_hash"] == source["source_hash"]
            and row["source_start"] == passage["start"] and row["source_end"] == passage["end"]
            and row["text"] == passage["text"] and row["embed_model"] == pin.get("embed_model")
            and row["embed_dim"] == pin.get("embed_dim")
        )
        try:
            complete = complete and len(vectors.normalize(json.loads(row["vec"]))) == pin.get("embed_dim")
        except (ValueError, TypeError):
            complete = False
    if not rows and marker:
        complete = (not source["passages"] and marker["source_hash"] == source["source_hash"]
                    and marker["index_version"] == PASSAGE_INDEX_VERSION
                    and marker["chunk_count"] == 0 and marker["embed_model"] == pin.get("embed_model")
                    and marker["embed_dim"] == pin.get("embed_dim"))
    source.update(complete=bool(complete), staged_batches=staged)
    return source


def stage_batch(mem, chapter_key, ordinal, source_hash, batch_id, passages, identity):
    source = _source(mem, chapter_key, ordinal)
    if source["source_hash"] != source_hash:
        raise ValueError("source changed while preparing the derived index")
    pin = _identity(mem, identity)
    _validated_passages(source["passages"], passages, pin["embed_dim"], subset=True)
    payload = json.dumps(passages, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    with mem._writer():
        existing = mem._conn.execute(
            "SELECT passages_json,embed_model,embed_dim FROM retrieval_index_batches "
            "WHERE chapter_key=? AND index_version=? AND source_hash=? AND batch_id=? AND book_id=?",
            (chapter_key, PASSAGE_INDEX_VERSION, source_hash, batch_id, mem._book_id),
        ).fetchone()
        if existing:
            if (existing["passages_json"] != payload or existing["embed_model"] != pin["embed_model"]
                    or existing["embed_dim"] != pin["embed_dim"]):
                raise ValueError("a staged source batch cannot be replaced with different vectors")
            return
        mem._conn.execute(
            "INSERT INTO retrieval_index_batches(chapter_key,book_id,revealed_at,index_version,"
            "source_hash,batch_id,embed_model,embed_dim,passages_json,recorded_at,retracted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
            (chapter_key, mem._book_id, ordinal, PASSAGE_INDEX_VERSION, source_hash, batch_id,
             pin["embed_model"], pin["embed_dim"], payload, datetime.now(timezone.utc).isoformat()),
        )


def publish(mem, chapter_key, ordinal, source_hash, passages, identity, *, require_completion=True):
    if not mem._transaction_active:
        with mem.transaction():
            return publish(mem, chapter_key, ordinal, source_hash, passages, identity,
                           require_completion=require_completion)
    source = _source(mem, chapter_key, ordinal, require_completion=require_completion)
    if source["source_hash"] != source_hash:
        raise ValueError("source changed while preparing the derived index")
    pin = _identity(mem, identity)
    _validated_passages(source["passages"], passages, pin["embed_dim"])
    with mem._writer():
        # Validate the full canonical/mirror parity once for this atomic chapter publication, rather
        # than re-reading and normalizing the whole book for every one of its source windows.
        if mem._vector_backend == "vec0":
            mem._ensure_vec0_index_locked(dimension_hint=pin["embed_dim"])
        previous_dimension = getattr(mem, "_source_index_append_dimension", None)
        mem._source_index_append_dimension = pin["embed_dim"]
        try:
            mem._retract("chunks", "chapter_key=?", (chapter_key,))
            for passage in passages:
                mem.add_chunk(chapter_key, ordinal, passage["text"], passage["vec"],
                              source_version=PASSAGE_INDEX_VERSION, source_hash=source_hash,
                              source_start=passage["start"], source_end=passage["end"])
        finally:
            mem._source_index_append_dimension = previous_dimension
        mem._conn.execute(
            "INSERT INTO retrieval_index_chapters(chapter_key,book_id,revealed_at,index_version,"
            "source_hash,embed_model,embed_dim,chunk_count,completed_at,retracted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(chapter_key) DO UPDATE SET "
            "index_version=excluded.index_version,source_hash=excluded.source_hash,"
            "embed_model=excluded.embed_model,embed_dim=excluded.embed_dim,"
            "chunk_count=excluded.chunk_count,completed_at=excluded.completed_at,retracted_at=NULL",
            (chapter_key, mem._book_id, ordinal, PASSAGE_INDEX_VERSION, source_hash,
             pin["embed_model"], pin["embed_dim"], len(passages), datetime.now(timezone.utc).isoformat()),
        )
        # Canonical vectors now constitute the durable receipt. Drop duplicate staging payloads only
        # in the same publication transaction; interrupted publication retains every paid vector.
        mem._conn.execute(
            "DELETE FROM retrieval_index_batches WHERE book_id=? AND chapter_key=?",
            (mem._book_id, chapter_key),
        )
