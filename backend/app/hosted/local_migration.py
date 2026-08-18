"""Safe planning primitives for local-library to hosted PostgreSQL migration (LIT-50)."""

from __future__ import annotations

import argparse
import base64
from datetime import UTC, datetime
import hashlib
import json
import math
import os
import sqlite3
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import psycopg

from app.config import Settings
from app.hosted.audit import record_event
from app.hosted.storage import (
    EPUB_MEDIA_TYPE,
    ObjectConflictError,
    SourceObjectRef,
    build_object_storage,
)
from app.hosted.tenant.models import OwnerId
from app.lifecycle.archive import PortableArchive, backup_book, read_portable_archive

_NAMESPACE = uuid.UUID("aaac9a16-5f55-4eb7-b32d-62536efb9b4f")


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    owner_id: OwnerId
    book_id: uuid.UUID
    incarnation: uuid.UUID
    source_object_id: uuid.UUID
    source_book_id: str
    archive_sha256: str
    source_checksum: str
    source_sha256: str
    atom_count: int
    atom_set_version: str
    row_counts: dict[str, int]
    bundle: PortableArchive


def _stable_uuid(owner: OwnerId, kind: str, value: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"{owner.value}:{kind}:{value}")


def _decode(value: object) -> object:
    if isinstance(value, dict) and set(value) == {"$bytes"}:
        return base64.b64decode(value["$bytes"], validate=True)
    return value


def _rows(export: dict[str, object], section: str, table: str) -> list[dict[str, object]]:
    raw_section = export.get(section)
    if not isinstance(raw_section, dict):
        raise ValueError(f"portable export {section} section is malformed")
    raw_table = raw_section.get(table)
    if not isinstance(raw_table, dict):
        raise ValueError(f"portable export table {table} is malformed")
    columns, rows = raw_table.get("columns"), raw_table.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise ValueError(f"portable export table {table} is malformed")
    return [dict(zip(columns, (_decode(value) for value in row), strict=True)) for row in rows]


def _table_counts(export: dict[str, object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for section_name in ("catalog", "memory"):
        section = export.get(section_name)
        if not isinstance(section, dict):
            raise ValueError(f"portable export {section_name} section is missing")
        for table, value in section.items():
            if not isinstance(table, str) or not isinstance(value, dict):
                raise ValueError("portable export table metadata is malformed")
            columns, rows = value.get("columns"), value.get("rows")
            if not isinstance(columns, list) or not isinstance(rows, list):
                raise ValueError(f"portable export table {table} is malformed")
            if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
                raise ValueError(f"portable export table {table} has malformed rows")
            counts[f"{section_name}.{table}"] = len(rows)
    return counts


def plan_archive(archive: str | Path, owner_id: OwnerId) -> MigrationPlan:
    """Build a deterministic, write-free plan from a verified backup archive."""
    if not isinstance(owner_id, OwnerId):
        raise TypeError("owner_id must be an OwnerId")
    bundle = read_portable_archive(archive)
    supported_phases = {"extraction", "synthesis", "embedding", "search-embedding", "judge"}
    phases = {str(row["phase"]) for row in _rows(bundle.export, "catalog", "cost_ledger")}
    if unsupported := phases - supported_phases:
        raise ValueError(f"portable export contains unsupported cost phases: {sorted(unsupported)}")
    source_sha256 = hashlib.sha256(bundle.source_epub).hexdigest()
    atoms = bundle.atoms.get("atoms")
    if not isinstance(atoms, list):
        raise ValueError("atom manifest is malformed")
    _preflight_bundle(bundle)
    canonical = json.dumps(
        {"export": bundle.export, "atoms": bundle.atoms, "source_sha256": source_sha256},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    source_checksum = hashlib.sha256(canonical).hexdigest()
    identity = f"{bundle.book_id}:{source_checksum}"
    return MigrationPlan(
        owner_id=owner_id,
        book_id=_stable_uuid(owner_id, "book", bundle.book_id),
        incarnation=_stable_uuid(owner_id, "incarnation", identity),
        source_object_id=_stable_uuid(owner_id, "source", f"{bundle.book_id}:{source_sha256}"),
        source_book_id=bundle.book_id,
        archive_sha256=bundle.archive_sha256,
        source_checksum=source_checksum,
        source_sha256=source_sha256,
        atom_count=len(atoms),
        atom_set_version=str(bundle.atoms.get("atom_set_version")),
        row_counts=_table_counts(bundle.export),
        bundle=bundle,
    )


def plan_checksum(plan: MigrationPlan) -> str:
    payload = {
        "owner_id": str(plan.owner_id.value),
        "book_id": str(plan.book_id),
        "incarnation": str(plan.incarnation),
        "source_object_id": str(plan.source_object_id),
        "source_checksum": plan.source_checksum,
        "source_sha256": plan.source_sha256,
        "atom_count": plan.atom_count,
        "atom_set_version": plan.atom_set_version,
        "row_counts": plan.row_counts,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class MigrationStorage(Protocol):
    provider: str
    encryption: str

    def put(self, ref: SourceObjectRef, data: bytes, *, media_type: str, expected_sha256: str): ...
    def get(self, ref: SourceObjectRef): ...
    def exists(self, ref: SourceObjectRef) -> bool: ...
    def delete(self, ref: SourceObjectRef) -> None: ...


@dataclass(frozen=True, slots=True)
class MigrationResult:
    status: str
    plan_checksum: str
    row_counts: dict[str, int]


def _report(plan: MigrationPlan) -> dict[str, object]:
    return {
        "atom_count": plan.atom_count,
        "atom_set_version": plan.atom_set_version,
        "row_counts": plan.row_counts,
    }


def _id(plan: MigrationPlan, kind: str, value: object) -> uuid.UUID:
    return _stable_uuid(plan.owner_id, kind, f"{plan.source_book_id}:{value}")


def _json(value: object, fallback: object) -> object:
    if value is None or value == "":
        return fallback
    return json.loads(value) if isinstance(value, str) else value


def _vector(value: object, dimension: object) -> tuple[str, int] | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        if not isinstance(dimension, int) or len(value) != dimension * 4:
            raise ValueError("legacy chunk vector payload does not match its dimension")
        numbers = struct.unpack(f"<{dimension}f", value)
    else:
        decoded = json.loads(value) if isinstance(value, str) else value
        if not isinstance(decoded, list) or not decoded:
            raise ValueError("legacy chunk vector payload does not match its dimension")
        numbers = tuple(float(number) for number in decoded)
        if dimension is None:
            dimension = len(numbers)
        if not isinstance(dimension, int) or len(numbers) != dimension:
            raise ValueError("legacy chunk vector payload does not match its dimension")
    if dimension < 1 or any(not math.isfinite(number) for number in numbers):
        raise ValueError("legacy chunk vector payload contains an invalid value")
    return "[" + ",".join(format(number, ".9g") for number in numbers) + "]", dimension


def _preflight_bundle(bundle: PortableArchive) -> None:
    """Reject local shapes that cannot be represented by the hosted constraints before upload."""
    export = bundle.export
    chapters = _rows(export, "memory", "chapters")
    chapter_keys = {str(row["chapter_key"]) for row in chapters}
    ordinals = {int(row["revealed_at"]) for row in chapters}
    raw_keys = {str(row["chapter_key"]) for row in _rows(export, "memory", "raw_chapters")}
    if chapter_keys - raw_keys:
        raise ValueError(
            "portable export has a chapter without verified raw bytes for checksum upgrade"
        )
    if any(ordinal < 1 for ordinal in ordinals):
        raise ValueError("hosted migration requires positive chapter reveal ordinals")
    for table in (
        "chapter_summaries",
        "entities",
        "aliases",
        "edges",
        "events",
        "event_participants",
        "themes",
        "entity_state",
        "entity_corrections",
        "chunks",
    ):
        for row in _rows(export, "memory", table):
            reveal = int(row["revealed_at"])
            if reveal < 1 or reveal not in ordinals:
                raise ValueError(f"portable export {table} row has no hosted source chapter")
    allowed_entity_types = {"character", "place", "faction", "object"}
    entities = _rows(export, "memory", "entities")
    if unsupported := {str(row["type"]) for row in entities} - allowed_entity_types:
        raise ValueError(
            f"portable export contains unsupported entity types: {sorted(unsupported)}"
        )
    for row in _rows(export, "memory", "chunks"):
        if str(row["chapter_key"]) not in chapter_keys:
            raise ValueError("portable export chunk references a missing chapter")
        _vector(row["vec"], row["embed_dim"])


def _insert_all(
    conn: psycopg.Connection[Any], plan: MigrationPlan, provider: str, encryption: str
) -> None:
    export = plan.bundle.export
    book = _rows(export, "catalog", "books")[0]
    meta = _rows(export, "memory", "book_meta")[0]
    owner, bid, inc = plan.owner_id.value, plan.book_id, plan.incarnation
    conn.execute(
        """INSERT INTO books
        (owner_id,id,incarnation,title,author,source_kind,source_id,file_hash,schema_version,
         content_language,book_type,extractor_model,synthesis_model,embedding_model,
         embedding_dimension,embedding_space,created_at,updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            owner,
            bid,
            inc,
            book["title"],
            book["author"],
            book["source"],
            book["source_id"],
            book["file_hash"],
            book["schema_version"],
            meta["content_language"] or "und",
            meta["book_type"] or "unknown",
            meta["extractor_model"],
            meta["synth_model"],
            meta["embed_model"],
            meta["embed_dim"],
            "legacy-local" if meta["embed_model"] else None,
            book["added_at"],
            book["last_opened_at"] or book["added_at"],
        ),
    )
    conn.execute(
        """INSERT INTO source_objects
        (owner_id,id,book_id,book_incarnation,storage_provider,storage_key,media_type,byte_size,sha256,encryption_key_id,verified_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())""",
        (
            owner,
            plan.source_object_id,
            bid,
            inc,
            provider,
            plan.source_object_id.hex,
            EPUB_MEDIA_TYPE,
            len(plan.bundle.source_epub),
            plan.source_sha256,
            encryption,
        ),
    )
    state = _rows(export, "catalog", "reading_state")[0]
    conn.execute(
        """INSERT INTO reading_state
        (owner_id,book_id,book_incarnation,bookmark,high_water_cfi,current_cfi,atom_set_version,
         position_epoch,last_opened_at,updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s,%s)""",
        (
            owner,
            bid,
            inc,
            state["bookmark"],
            state["cfi"],
            state["cfi"],
            state["position_epoch"],
            book["last_opened_at"] or book["added_at"],
            state["updated_at"],
        ),
    )

    raw_hashes = {
        str(row["chapter_key"]): hashlib.sha256(str(row["text"]).encode()).hexdigest()
        for row in _rows(export, "memory", "raw_chapters")
    }
    chapters = _rows(export, "memory", "chapters")
    chapter_ids: dict[str, uuid.UUID] = {}
    ordinal_ids: dict[int, uuid.UUID] = {}
    for row in chapters:
        cid = _id(plan, "chapter", row["chapter_key"])
        chapter_ids[str(row["chapter_key"])] = cid
        ordinal_ids[int(row["revealed_at"])] = cid
        conn.execute(
            """INSERT INTO chapters
            (owner_id,book_id,book_incarnation,id,chapter_key,revealed_at,href,fragment,title,part_label,kind,
             content_hash,schema_version,extractor_version,recorded_at,retracted_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                owner,
                bid,
                inc,
                cid,
                row["chapter_key"],
                row["revealed_at"],
                row["href"],
                row["fragment"],
                row["title"],
                row["part_label"],
                row["kind"],
                raw_hashes[str(row["chapter_key"])],
                row["schema_version"],
                row["extractor_version"],
                row["recorded_at"],
                row["retracted_at"],
            ),
        )
    receipts = {
        str(row["chapter_key"]): row for row in _rows(export, "memory", "ingested_chapters")
    }
    for key, row in receipts.items():
        cid = chapter_ids[key]
        conn.execute(
            """INSERT INTO ingested_chapters
            (owner_id,book_id,book_incarnation,chapter_id,content_hash,extractor_model,input_tokens,output_tokens,usd,completed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                owner,
                bid,
                inc,
                cid,
                raw_hashes[key],
                row["extractor_model"],
                row["input_tokens"],
                row["output_tokens"],
                row["usd"],
                row["completed_at"],
            ),
        )

    def source_id(row: dict[str, object]) -> uuid.UUID:
        ordinal = int(row["revealed_at"])
        if ordinal not in ordinal_ids:
            raise ValueError(f"memory row references missing chapter ordinal {ordinal}")
        return ordinal_ids[ordinal]

    entity_ids: dict[str, uuid.UUID] = {}
    for row in _rows(export, "memory", "entities"):
        eid = _id(plan, "entity", row["entity_id"])
        entity_ids[str(row["entity_id"])] = eid
        conn.execute(
            """INSERT INTO entities
          (owner_id,book_id,book_incarnation,id,source_chapter_id,canonical_name,entity_type,revealed_at,invalid_at,
           schema_version,extractor_version,recorded_at,retracted_at)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                owner,
                bid,
                inc,
                eid,
                source_id(row),
                row["canonical_name"],
                row["type"],
                row["revealed_at"],
                row["invalid_at"],
                row["schema_version"],
                row["extractor_version"],
                row["recorded_at"],
                row["retracted_at"],
            ),
        )
    for row in _rows(export, "memory", "chapter_summaries"):
        conn.execute(
            """INSERT INTO chapter_summaries
          (owner_id,book_id,book_incarnation,id,source_chapter_id,kind,summary,revealed_at,schema_version,extractor_version,recorded_at,retracted_at)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                owner,
                bid,
                inc,
                _id(plan, "summary", row["summary_id"]),
                source_id(row),
                row["kind"],
                row["summary"],
                row["revealed_at"],
                row["schema_version"],
                row["extractor_version"],
                row["recorded_at"],
                row["retracted_at"],
            ),
        )
    alias_ids: dict[str, uuid.UUID] = {}
    for row in _rows(export, "memory", "aliases"):
        alias_id = _id(plan, "alias", row["alias_id"])
        alias_ids[str(row["alias_id"])] = alias_id
        conn.execute(
            """INSERT INTO aliases
          (owner_id,book_id,book_incarnation,id,entity_id,source_chapter_id,surface_form,revealed_at,recorded_at,retracted_at)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                owner,
                bid,
                inc,
                alias_id,
                entity_ids[str(row["entity_id"])],
                source_id(row),
                row["surface_form"],
                row["revealed_at"],
                row["recorded_at"],
                row["retracted_at"],
            ),
        )
    edge_ids: dict[str, uuid.UUID] = {}
    for row in _rows(export, "memory", "edges"):
        edge_id = _id(plan, "edge", row["edge_id"])
        edge_ids[str(row["edge_id"])] = edge_id
        conn.execute(
            """INSERT INTO edges
          (owner_id,book_id,book_incarnation,id,source_chapter_id,src_entity_id,dst_entity_id,relationship_type,label,
           revealed_at,invalid_at,schema_version,extractor_version,recorded_at,retracted_at)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                owner,
                bid,
                inc,
                edge_id,
                source_id(row),
                entity_ids[str(row["src_entity"])],
                entity_ids[str(row["dst_entity"])],
                row["rel_type"],
                row["label"],
                row["revealed_at"],
                row["invalid_at"],
                row["schema_version"],
                row["extractor_version"],
                row["recorded_at"],
                row["retracted_at"],
            ),
        )
    event_ids: dict[str, uuid.UUID] = {}
    for row in _rows(export, "memory", "events"):
        eid = _id(plan, "event", row["event_id"])
        event_ids[str(row["event_id"])] = eid
        conn.execute(
            """INSERT INTO events
          (owner_id,book_id,book_incarnation,id,source_chapter_id,order_idx,summary,kind,revealed_at,invalid_at,
           schema_version,extractor_version,recorded_at,retracted_at)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                owner,
                bid,
                inc,
                eid,
                source_id(row),
                row["order_idx"],
                row["summary"],
                row["kind"],
                row["revealed_at"],
                row["invalid_at"],
                row["schema_version"],
                row["extractor_version"],
                row["recorded_at"],
                row["retracted_at"],
            ),
        )
    for row in _rows(export, "memory", "event_participants"):
        conn.execute(
            """INSERT INTO event_participants
          (owner_id,book_id,book_incarnation,event_id,entity_id,source_chapter_id,role,revealed_at)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                owner,
                bid,
                inc,
                event_ids[str(row["event_id"])],
                entity_ids[str(row["entity_id"])],
                source_id(row),
                row["role"],
                row["revealed_at"],
            ),
        )
    for table, id_col, target in (
        ("themes", "theme_id", "themes"),
        ("entity_state", "state_id", "entity_state"),
    ):
        for row in _rows(export, "memory", table):
            if table == "themes":
                conn.execute(
                    """INSERT INTO themes
                  (owner_id,book_id,book_incarnation,id,source_chapter_id,name,description,revealed_at,invalid_at,schema_version,extractor_version,recorded_at,retracted_at)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        owner,
                        bid,
                        inc,
                        _id(plan, target, row[id_col]),
                        source_id(row),
                        row["name"],
                        row["description"],
                        row["revealed_at"],
                        row["invalid_at"],
                        row["schema_version"],
                        row["extractor_version"],
                        row["recorded_at"],
                        row["retracted_at"],
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO entity_state
                  (owner_id,book_id,book_incarnation,id,entity_id,source_chapter_id,status,revealed_at,invalid_at,schema_version,extractor_version,recorded_at,retracted_at)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        owner,
                        bid,
                        inc,
                        _id(plan, target, row[id_col]),
                        entity_ids[str(row["entity_id"])],
                        source_id(row),
                        json.dumps(_json(row["status_json"], {})),
                        row["revealed_at"],
                        row["invalid_at"],
                        row["schema_version"],
                        row["extractor_version"],
                        row["recorded_at"],
                        row["retracted_at"],
                    ),
                )
    for row in _rows(export, "memory", "entity_corrections"):
        source_entities = [
            str(entity_ids[str(value)]) for value in _json(row["source_entity_ids_json"], [])
        ]
        target_entities = [
            str(entity_ids[str(value)]) for value in _json(row["target_entity_ids_json"], [])
        ]
        assignments = _json(row["assignments_json"], {})
        if not isinstance(assignments, dict):
            raise ValueError("entity correction assignments must be an object")
        assignments = dict(assignments)
        for name, ids in (("aliases", alias_ids), ("edges", edge_ids), ("events", event_ids)):
            values = assignments.get(name)
            if isinstance(values, dict):
                assignments[name] = {str(ids[str(key)]): value for key, value in values.items()}
        copied_edges = assignments.get("copied_edge_ids")
        if isinstance(copied_edges, list):
            assignments["copied_edge_ids"] = [str(edge_ids[str(value)]) for value in copied_edges]
        event_roles = assignments.get("event_roles")
        if isinstance(event_roles, dict):
            assignments["event_roles"] = {
                str(event_ids[str(key)]): value for key, value in event_roles.items()
            }
        conn.execute(
            """INSERT INTO entity_corrections
          (owner_id,book_id,book_incarnation,id,source_chapter_id,correction_kind,source_entity_ids,target_entity_ids,assignments,reason,revealed_at,schema_version,recorded_at,retracted_at)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                owner,
                bid,
                inc,
                _id(plan, "correction", row["correction_id"]),
                source_id(row),
                row["kind"],
                json.dumps(source_entities),
                json.dumps(target_entities),
                json.dumps(assignments),
                row["reason"],
                row["revealed_at"],
                row["schema_version"],
                row["recorded_at"],
                row["retracted_at"],
            ),
        )
    for row in _rows(export, "memory", "chunks"):
        cid = _id(plan, "chunk", row["chunk_id"])
        chapter = chapter_ids[str(row["chapter_key"])]
        conn.execute(
            """INSERT INTO chunks
          (owner_id,book_id,book_incarnation,id,chapter_id,revealed_at,text,recorded_at,retracted_at)
          VALUES (%s,%s,%s,%s,%s,%s,%s,now(),%s)""",
            (owner, bid, inc, cid, chapter, row["revealed_at"], row["text"], row["retracted_at"]),
        )
        vector = _vector(row["vec"], row["embed_dim"])
        if vector is not None:
            vector_text, vector_dimension = vector
            conn.execute(
                """INSERT INTO chunk_embeddings
              (owner_id,book_id,book_incarnation,chunk_id,embedding_model,embedding_dimension,embedding_space,distance_metric,embedding,retracted_at)
              VALUES (%s,%s,%s,%s,%s,%s,'legacy-local','cosine',%s::vector,%s)""",
                (
                    owner,
                    bid,
                    inc,
                    cid,
                    row["embed_model"] or "legacy:unstamped",
                    vector_dimension,
                    vector_text,
                    row["retracted_at"],
                ),
            )
    for row in _rows(export, "catalog", "cost_ledger"):
        phase = "embedding" if row["phase"] == "search-embedding" else row["phase"]
        conn.execute(
            """INSERT INTO cost_ledger
          (owner_id,id,book_id,book_incarnation,chapter_ordinal,phase,model,input_tokens,output_tokens,usd,idempotency_key,recorded_at)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                owner,
                _id(plan, "cost", row["entry_id"]),
                bid,
                inc,
                row["chapter_ordinal"],
                phase,
                row["model"],
                row["input_tokens"],
                row["output_tokens"],
                row["usd"],
                f"local-migration:{plan.source_checksum}:{row['entry_id']}",
                row["at"],
            ),
        )


def import_archive(
    dsn: str, plan: MigrationPlan, storage: MigrationStorage | None, *, dry_run: bool = False
) -> MigrationResult:
    """Import one verified backup exactly once; a failed DB transaction is safely resumable."""
    checksum = plan_checksum(plan)
    ref = SourceObjectRef(plan.owner_id, plan.source_object_id)
    with psycopg.connect(dsn, autocommit=True) as conn:
        if (
            conn.execute("SELECT 1 FROM users WHERE id=%s", (plan.owner_id.value,)).fetchone()
            is None
        ):
            raise ValueError("selected hosted owner does not exist")
        if dry_run:
            return MigrationResult("dry-run", checksum, plan.row_counts)
        if storage is None:
            raise ValueError("object storage is required for an applied migration")
        existing = conn.execute(
            "SELECT source_checksum,plan_checksum,status FROM local_library_migrations WHERE owner_id=%s AND source_book_id=%s",
            (plan.owner_id.value, plan.source_book_id),
        ).fetchone()
        if existing:
            if existing[0] != plan.source_checksum or existing[1] != checksum:
                raise ValueError("this local book was already imported from different content")
            if existing[2] == "complete":
                stored = storage.get(ref)
                if stored.sha256 != plan.source_sha256:
                    raise ValueError("stored source checksum mismatch")
                _verify_import(conn, plan)
                return MigrationResult("already-complete", checksum, plan.row_counts)
            raise ValueError("this local book has a rollback in progress")
        if storage.exists(ref):
            if storage.get(ref).sha256 != plan.source_sha256:
                raise ValueError("stored source checksum mismatch")
        else:
            try:
                storage.put(
                    ref,
                    plan.bundle.source_epub,
                    media_type=EPUB_MEDIA_TYPE,
                    expected_sha256=plan.source_sha256,
                )
            except ObjectConflictError:
                if storage.get(ref).sha256 != plan.source_sha256:
                    raise
        with conn.transaction():
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"{plan.owner_id.value}:{plan.source_book_id}",),
            )
            raced = conn.execute(
                "SELECT source_checksum,plan_checksum,status FROM local_library_migrations WHERE owner_id=%s AND source_book_id=%s",
                (plan.owner_id.value, plan.source_book_id),
            ).fetchone()
            if raced is not None:
                if raced != (plan.source_checksum, checksum, "complete"):
                    raise ValueError("concurrent migration has a conflicting plan or status")
                _verify_import(conn, plan)
                return MigrationResult("already-complete", checksum, plan.row_counts)
            conn.execute(
                """INSERT INTO local_library_migrations
              (owner_id,source_book_id,source_checksum,plan_checksum,book_id,book_incarnation,source_object_id,status)
              VALUES (%s,%s,%s,%s,%s,%s,%s,'importing')""",
                (
                    plan.owner_id.value,
                    plan.source_book_id,
                    plan.source_checksum,
                    checksum,
                    plan.book_id,
                    plan.incarnation,
                    plan.source_object_id,
                ),
            )
            _insert_all(conn, plan, storage.provider, storage.encryption)
            conn.execute(
                "UPDATE local_library_migrations SET status='complete',report=%s::jsonb,completed_at=now(),updated_at=now() WHERE owner_id=%s AND source_book_id=%s",
                (json.dumps(_report(plan)), plan.owner_id.value, plan.source_book_id),
            )
            record_event(
                conn,
                owner_id=plan.owner_id.value,
                actor_kind="system",
                action="book.import",
                target_kind="book",
                target_id=plan.book_id,
                result="succeeded",
            )
            _verify_import(conn, plan)
    return MigrationResult("imported", checksum, plan.row_counts)


def _verify_import(conn: psycopg.Connection[Any], plan: MigrationPlan) -> None:
    """Fail closed unless counts, boundaries, state, and source checksums match the plan."""
    owner, bid, inc = plan.owner_id.value, plan.book_id, plan.incarnation

    def timestamp(value: object) -> str | None:
        if value is None:
            return None
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()

    expected = {
        "books": 1,
        "source_objects": 1,
        "reading_state": 1,
        "chapters": plan.row_counts["memory.chapters"],
        "ingested_chapters": plan.row_counts["memory.ingested_chapters"],
        "chapter_summaries": plan.row_counts["memory.chapter_summaries"],
        "entities": plan.row_counts["memory.entities"],
        "aliases": plan.row_counts["memory.aliases"],
        "edges": plan.row_counts["memory.edges"],
        "events": plan.row_counts["memory.events"],
        "event_participants": plan.row_counts["memory.event_participants"],
        "themes": plan.row_counts["memory.themes"],
        "entity_state": plan.row_counts["memory.entity_state"],
        "entity_corrections": plan.row_counts["memory.entity_corrections"],
        "chunks": plan.row_counts["memory.chunks"],
        "chunk_embeddings": sum(
            row["vec"] is not None for row in _rows(plan.bundle.export, "memory", "chunks")
        ),
        "cost_ledger": plan.row_counts["catalog.cost_ledger"],
    }
    for table, wanted in expected.items():
        if table == "books":
            actual = conn.execute(
                "SELECT count(*) FROM books WHERE owner_id=%s AND id=%s AND incarnation=%s",
                (owner, bid, inc),
            ).fetchone()[0]
        else:
            actual = conn.execute(
                f"SELECT count(*) FROM {table} WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s",
                (owner, bid, inc),
            ).fetchone()[0]
        if actual != wanted:
            raise ValueError(f"hosted verification count mismatch for {table}")
    source = conn.execute(
        "SELECT sha256,byte_size FROM source_objects WHERE owner_id=%s AND id=%s",
        (owner, plan.source_object_id),
    ).fetchone()
    if source != (plan.source_sha256, len(plan.bundle.source_epub)):
        raise ValueError("hosted source object metadata does not match the archive")
    migration_report = conn.execute(
        "SELECT report FROM local_library_migrations WHERE owner_id=%s AND source_book_id=%s",
        (owner, plan.source_book_id),
    ).fetchone()
    if migration_report is None or migration_report[0] != _report(plan):
        raise ValueError("hosted migration report does not match the archive")
    raw_hashes = {
        str(row["chapter_key"]): hashlib.sha256(str(row["text"]).encode()).hexdigest()
        for row in _rows(plan.bundle.export, "memory", "raw_chapters")
    }
    expected_chapters = sorted(
        (
            str(row["chapter_key"]),
            int(row["revealed_at"]),
            raw_hashes[str(row["chapter_key"])],
            timestamp(row["retracted_at"]),
        )
        for row in _rows(plan.bundle.export, "memory", "chapters")
    )
    actual_chapters = sorted(
        (key, reveal, content_hash, timestamp(retracted))
        for key, reveal, content_hash, retracted in conn.execute(
            "SELECT chapter_key,revealed_at,content_hash,retracted_at FROM chapters WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s",
            (owner, bid, inc),
        ).fetchall()
    )
    if actual_chapters != expected_chapters:
        raise ValueError("hosted chapter atoms or checksums do not match the archive")
    local_book = _rows(plan.bundle.export, "catalog", "books")[0]
    local_state = _rows(plan.bundle.export, "catalog", "reading_state")[0]
    hosted_state = conn.execute(
        "SELECT bookmark,current_cfi,position_epoch,last_opened_at FROM reading_state WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s",
        (owner, bid, inc),
    ).fetchone()
    if (
        hosted_state[0],
        hosted_state[1],
        hosted_state[2],
        timestamp(hosted_state[3]),
    ) != (
        local_state["bookmark"],
        local_state["cfi"],
        local_state["position_epoch"],
        timestamp(local_book["last_opened_at"] or local_book["added_at"]),
    ):
        raise ValueError("hosted reading state does not match the archive")
    memory_tables = (
        "chapter_summaries",
        "entities",
        "aliases",
        "edges",
        "events",
        "themes",
        "entity_state",
    )

    expected_boundaries = sorted(
        (int(row["revealed_at"]), row.get("invalid_at"), timestamp(row.get("retracted_at")))
        for table in memory_tables
        for row in _rows(plan.bundle.export, "memory", table)
    )
    actual_boundaries = []
    for table in memory_tables:
        actual_boundaries.extend(
            (revealed_at, invalid_at, timestamp(retracted_at))
            for revealed_at, invalid_at, retracted_at in conn.execute(
                f"SELECT revealed_at,invalid_at,retracted_at FROM {table} WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s",
                (owner, bid, inc),
            ).fetchall()
        )
    if sorted(actual_boundaries) != expected_boundaries:
        raise ValueError("hosted memory validity boundaries do not match the archive")


def rollback_archive(dsn: str, plan: MigrationPlan, storage: MigrationStorage) -> MigrationResult:
    """Remove only rows proven to belong to this migration, then delete its source object."""
    checksum = plan_checksum(plan)
    ref = SourceObjectRef(plan.owner_id, plan.source_object_id)
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT source_checksum,plan_checksum,status FROM local_library_migrations WHERE owner_id=%s AND source_book_id=%s FOR UPDATE",
                (plan.owner_id.value, plan.source_book_id),
            ).fetchone()
            if row is None:
                return MigrationResult("not-found", checksum, plan.row_counts)
            if row[:2] != (plan.source_checksum, checksum):
                raise ValueError("rollback plan does not match imported content")
            if row[2] == "complete":
                conn.execute(
                    "UPDATE local_library_migrations SET status='rolling_back',updated_at=now() WHERE owner_id=%s AND source_book_id=%s",
                    (plan.owner_id.value, plan.source_book_id),
                )
                conn.execute(
                    "DELETE FROM cost_ledger WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s",
                    (plan.owner_id.value, plan.book_id, plan.incarnation),
                )
                for table in (
                    "event_participants",
                    "aliases",
                    "edges",
                    "entity_state",
                    "events",
                    "themes",
                    "entity_corrections",
                    "chapter_summaries",
                    "chunks",
                    "entities",
                    "ingested_chapters",
                    "chapters",
                ):
                    conn.execute(
                        f"DELETE FROM {table} WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s",
                        (plan.owner_id.value, plan.book_id, plan.incarnation),
                    )
                conn.execute(
                    "DELETE FROM books WHERE owner_id=%s AND id=%s AND incarnation=%s",
                    (plan.owner_id.value, plan.book_id, plan.incarnation),
                )
                record_event(
                    conn,
                    owner_id=plan.owner_id.value,
                    actor_kind="system",
                    action="book.delete",
                    target_kind="book",
                    target_id=plan.book_id,
                    result="succeeded",
                )
            elif row[2] != "rolling_back":
                raise ValueError("migration is not in a rollbackable state")
        storage.delete(ref)
        with conn.transaction():
            conn.execute(
                "DELETE FROM local_library_migrations WHERE owner_id=%s AND source_book_id=%s",
                (plan.owner_id.value, plan.source_book_id),
            )
    return MigrationResult("rolled-back", checksum, plan.row_counts)


def backup_library(data_dir: str | Path, backup_dir: str | Path) -> list[Path]:
    """Create one verified immutable archive per catalog book without changing local data."""
    source = Path(data_dir).expanduser().resolve()
    destination = Path(backup_dir).expanduser().resolve(strict=False)
    if destination.is_relative_to(source):
        raise ValueError("backup directory must be outside the local data directory")
    destination.mkdir(parents=True, exist_ok=True)
    catalog = source / "catalog.db"
    try:
        with sqlite3.connect(f"{catalog.as_uri()}?mode=ro", uri=True) as conn:
            book_ids = [
                str(row[0]) for row in conn.execute("SELECT book_id FROM books ORDER BY book_id")
            ]
    except sqlite3.Error as exc:
        raise ValueError("local catalog cannot be read") from exc
    if not book_ids:
        raise ValueError("local catalog contains no books")
    archives: list[Path] = []
    for book_id in book_ids:
        filename = f"{uuid.uuid5(_NAMESPACE, book_id).hex}.rcbackup"
        output = destination / filename
        backup_book(source, book_id, output)
        archives.append(output)
    return archives


def _dsn(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"database DSN environment variable {name!r} is not set")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate verified local library backups to hosted mode"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup", help="create verified pre-migration backups")
    backup_parser.add_argument("--data-dir", required=True)
    backup_parser.add_argument("--backup-dir", required=True)
    for command in ("plan", "apply", "rollback"):
        action = subparsers.add_parser(command)
        action.add_argument("--archive", action="append", required=True)
        action.add_argument("--owner", type=uuid.UUID, required=True)
        action.add_argument("--dsn-env", default="DATABASE_URL")
    args = parser.parse_args(argv)
    if args.command == "backup":
        paths = backup_library(args.data_dir, args.backup_dir)
        print(json.dumps({"status": "backed-up", "archives": [str(path) for path in paths]}))
        return 0
    owner = OwnerId(args.owner)
    plans = [plan_archive(path, owner) for path in args.archive]
    dsn = _dsn(args.dsn_env)
    storage = None if args.command == "plan" else build_object_storage(Settings())
    try:
        results = []
        for plan in reversed(plans) if args.command == "rollback" else plans:
            if args.command == "plan":
                result = import_archive(dsn, plan, None, dry_run=True)
            elif args.command == "apply":
                result = import_archive(dsn, plan, storage)
            else:
                result = rollback_archive(dsn, plan, storage)
            results.append(
                {
                    "book_id": plan.source_book_id,
                    "status": result.status,
                    "plan_checksum": result.plan_checksum,
                    "row_counts": result.row_counts,
                }
            )
        print(json.dumps({"results": results}, sort_keys=True))
    finally:
        if storage is not None:
            storage.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
