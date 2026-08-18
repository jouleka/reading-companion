"""LIT-10 bookmark-effective entity identity corrections.

An identity correction is story knowledge. It must not rewrite an earlier bookmark's world model.
Instead, sources end at ``effective_at`` and replacement identity rows begin there. Dependencies are
copied forward at the same boundary; the original rows remain intact for audit and earlier views.
"""
import json
from collections import defaultdict
from datetime import datetime, timezone

from . import migrations


def _now():
    return datetime.now(timezone.utc).isoformat()


def _require_effective_at(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("effective_at must be a positive integer bookmark")


def _unique_ids(entity_ids):
    if not isinstance(entity_ids, (list, tuple)):
        raise ValueError("entity_ids must be a list or tuple")
    ids = list(entity_ids)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in ids):
        raise ValueError("entity_ids must contain integer ids")
    if len(set(ids)) != len(ids):
        raise ValueError("entity_ids must be distinct")
    return ids


def _rows(db, sql, params=()):
    with db._writer():
        return [dict(row) for row in db._conn.execute(sql, params).fetchall()]


def correction_inventory(db, entity_ids, effective_at):
    """Return the complete visible dependency inventory an operator must resolve.

    This is trusted correction tooling, not a reader view. Queries are nevertheless bounded to the
    proposed correction frontier and never consult facts revealed later than ``effective_at``.
    """
    _require_effective_at(effective_at)
    ids = _unique_ids(entity_ids)
    if not ids:
        raise ValueError("at least one source entity is required")
    marks = ",".join("?" for _ in ids)
    entities = _rows(
        db,
        f"SELECT entity_id,canonical_name,type,revealed_at,invalid_at FROM entities "
        f"WHERE book_id=? AND entity_id IN ({marks}) AND retracted_at IS NULL "
        "AND revealed_at < ? AND (invalid_at IS NULL OR invalid_at > ?)",
        (db._book_id, *ids, effective_at, effective_at),
    )
    found = {row["entity_id"] for row in entities}
    if found != set(ids):
        raise ValueError(
            "effective_at must be strictly later than every live source reveal"
        )

    common = (db._book_id, *ids, effective_at)
    aliases = _rows(
        db,
        f"SELECT alias_id,entity_id,surface_form,revealed_at FROM aliases WHERE book_id=? "
        f"AND entity_id IN ({marks}) AND revealed_at<=? AND retracted_at IS NULL "
        "ORDER BY alias_id",
        common,
    )
    edges = _rows(
        db,
        "SELECT e.edge_id,e.src_entity,e.dst_entity,e.rel_type,e.label,e.revealed_at,e.invalid_at "
        "FROM edges e JOIN entities src ON src.entity_id=e.src_entity AND src.book_id=e.book_id "
        "JOIN entities dst ON dst.entity_id=e.dst_entity AND dst.book_id=e.book_id "
        f"WHERE e.book_id=? AND (e.src_entity IN ({marks}) OR e.dst_entity IN ({marks})) "
        "AND e.revealed_at<=? AND e.retracted_at IS NULL "
        "AND (e.invalid_at IS NULL OR e.invalid_at>?) "
        "AND src.revealed_at<=? AND src.retracted_at IS NULL "
        "AND (src.invalid_at IS NULL OR src.invalid_at>?) "
        "AND dst.revealed_at<=? AND dst.retracted_at IS NULL "
        "AND (dst.invalid_at IS NULL OR dst.invalid_at>?) ORDER BY e.edge_id",
        (
            db._book_id,
            *ids,
            *ids,
            effective_at,
            effective_at,
            effective_at,
            effective_at,
            effective_at,
            effective_at,
        ),
    )
    participants = _rows(
        db,
        f"SELECT ep.event_id,ep.entity_id,ep.role,ep.revealed_at FROM event_participants ep "
        "JOIN events ev ON ev.event_id=ep.event_id AND ev.book_id=ep.book_id "
        f"WHERE ep.book_id=? AND ep.entity_id IN ({marks}) AND ep.revealed_at<=? "
        "AND ev.revealed_at<=? AND ev.retracted_at IS NULL "
        "AND (ev.invalid_at IS NULL OR ev.invalid_at>?) ORDER BY ep.event_id,ep.entity_id",
        (db._book_id, *ids, effective_at, effective_at, effective_at),
    )
    states = _rows(
        db,
        f"SELECT state_id,entity_id,revealed_at,status_json FROM entity_state WHERE book_id=? "
        f"AND entity_id IN ({marks}) AND revealed_at<=? AND retracted_at IS NULL "
        "AND (invalid_at IS NULL OR invalid_at>?) "
        "ORDER BY entity_id,revealed_at DESC,state_id DESC",
        (db._book_id, *ids, effective_at, effective_at),
    )
    current_states = []
    seen = set()
    for row in states:
        if row["entity_id"] not in seen:
            seen.add(row["entity_id"])
            current_states.append(row)
    return {
        "effective_at": effective_at,
        "entities": entities,
        "aliases": aliases,
        "edges": edges,
        "event_participants": participants,
        "current_states": current_states,
    }


def _assignment_map(name, assignments, expected_ids, target_count):
    if not isinstance(assignments, dict):
        raise ValueError(f"{name} must be a mapping")
    normalized = {}
    for raw_id, raw_targets in assignments.items():
        try:
            record_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} keys must be record ids") from exc
        if not isinstance(raw_targets, (list, tuple)):
            raise ValueError(f"{name}[{record_id}] must be a list of target indexes")
        targets = list(raw_targets)
        if any(
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or index >= target_count
            for index in targets
        ):
            raise ValueError(f"{name}[{record_id}] contains an invalid target index")
        if len(set(targets)) != len(targets):
            raise ValueError(f"{name}[{record_id}] contains duplicate target indexes")
        normalized[record_id] = targets
    if set(normalized) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(normalized))
        extra = sorted(set(normalized) - set(expected_ids))
        raise ValueError(
            f"{name} must explicitly cover every active dependency (missing={missing}, extra={extra}); "
            "use [] to drop one intentionally"
        )
    return normalized


def _invalidate_sources(db, source_ids, effective_at):
    marks = ",".join("?" for _ in source_ids)
    with db._writer():
        cur = db._conn.execute(
            f"UPDATE entities SET invalid_at=? WHERE book_id=? AND entity_id IN ({marks}) "
            "AND retracted_at IS NULL AND (invalid_at IS NULL OR invalid_at>?)",
            (effective_at, db._book_id, *source_ids, effective_at),
        )
    if cur.rowcount != len(source_ids):
        raise RuntimeError("source identity validity changed during correction")


def _record(db, kind, effective_at, source_ids, target_ids, assignments, reason):
    return db._ins(
        "entity_corrections",
        book_id=db._book_id,
        kind=kind,
        revealed_at=effective_at,
        source_entity_ids_json=json.dumps(source_ids, separators=(",", ":")),
        target_entity_ids_json=json.dumps(target_ids, separators=(",", ":")),
        assignments_json=json.dumps(assignments, sort_keys=True, separators=(",", ":")),
        reason=reason or None,
        schema_version=migrations.CURRENT_VERSION,
        recorded_at=_now(),
        retracted_at=None,
    )


def correction_history(db, effective_at):
    """Return immutable correction provenance visible at ``effective_at`` only."""
    _require_effective_at(effective_at)
    rows = _rows(
        db,
        "SELECT correction_id,kind,revealed_at,source_entity_ids_json,"
        "target_entity_ids_json,reason,recorded_at FROM entity_corrections "
        "WHERE book_id=? AND revealed_at<=? AND retracted_at IS NULL "
        "ORDER BY revealed_at,correction_id",
        (db._book_id, effective_at),
    )
    ids = set()
    for row in rows:
        row["source_entity_ids"] = json.loads(row.pop("source_entity_ids_json"))
        row["target_entity_ids"] = json.loads(row.pop("target_entity_ids_json"))
        ids.update(row["source_entity_ids"])
        ids.update(row["target_entity_ids"])
    names = {}
    if ids:
        marks = ",".join("?" for _ in ids)
        entities = _rows(
            db,
            f"SELECT entity_id,canonical_name FROM entities WHERE book_id=? "
            f"AND entity_id IN ({marks}) AND retracted_at IS NULL",
            (db._book_id, *sorted(ids)),
        )
        names = {row["entity_id"]: row["canonical_name"] for row in entities}
    return [
        {
            "correction_id": row["correction_id"],
            "kind": row["kind"],
            "effective_at": row["revealed_at"],
            "source_entities": [
                {"entity_id": value, "name": names.get(value, "Earlier memory")}
                for value in row["source_entity_ids"]
            ],
            "target_entities": [
                {"entity_id": value, "name": names.get(value, "Corrected memory")}
                for value in row["target_entity_ids"]
            ],
            "reason": row["reason"],
            "recorded_at": row["recorded_at"],
        }
        for row in rows
    ]


def replace_entity(db, entity_id, *, effective_at, canonical_name, reason):
    """Correct one identity without rewriting the reader's earlier story-time view.

    The old identity ends at the correction frontier and a one-for-one replacement carries every
    currently visible dependency forward. The previous canonical name becomes an alias, preserving
    grounded mentions while making the reader's correction the displayed name.
    """
    if not isinstance(canonical_name, str) or not canonical_name.strip():
        raise ValueError("canonical_name must be non-empty")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be non-empty")
    inventory = correction_inventory(db, [entity_id], effective_at)
    source = inventory["entities"][0]
    name = canonical_name.strip()
    if name.casefold() == source["canonical_name"].strip().casefold():
        raise ValueError("corrected name must differ from the current name")

    with db.transaction():
        _invalidate_sources(db, [entity_id], effective_at)
        target = db.add_entity(name, source["type"], effective_at, "lit59-reader-correction")
        state = inventory["current_states"][0] if inventory["current_states"] else None
        if state is not None:
            db.add_state(
                target,
                effective_at,
                json.loads(state["status_json"]),
                extractor_version="lit59-reader-correction",
            )
        aliases = [source["canonical_name"], *(row["surface_form"] for row in inventory["aliases"])]
        seen = {name.casefold()}
        for alias in aliases:
            key = alias.casefold()
            if key not in seen:
                seen.add(key)
                db.add_alias(target, alias, effective_at)
        copied_edges = []
        for edge in inventory["edges"]:
            src = target if edge["src_entity"] == entity_id else edge["src_entity"]
            dst = target if edge["dst_entity"] == entity_id else edge["dst_entity"]
            db.add_edge(
                src,
                dst,
                edge["rel_type"],
                edge["label"],
                effective_at,
                invalid_at=edge["invalid_at"],
                extractor_version="lit59-reader-correction",
            )
            copied_edges.append(edge["edge_id"])
        copied_events = []
        for participant in inventory["event_participants"]:
            db.add_event_participant(
                participant["event_id"], target, participant["role"], effective_at
            )
            copied_events.append(participant["event_id"])
        correction_id = _record(
            db,
            "replace",
            effective_at,
            [entity_id],
            [target],
            {
                "previous_name": source["canonical_name"],
                "corrected_name": name,
                "copied_edge_ids": copied_edges,
                "copied_event_ids": copied_events,
            },
            reason.strip(),
        )
    return {"correction_id": correction_id, "target_entity_id": target}


def split_entity(
    db,
    entity_id,
    *,
    effective_at,
    replacements,
    alias_assignments,
    edge_assignments,
    event_assignments,
    reason="",
):
    """Replace one mistakenly merged identity with two or more bookmark-effective identities."""
    if not isinstance(replacements, (list, tuple)) or len(replacements) < 2:
        raise ValueError("split requires at least two replacements")
    inventory = correction_inventory(db, [entity_id], effective_at)
    source = inventory["entities"][0]
    specs = []
    for replacement in replacements:
        if not isinstance(replacement, dict):
            raise ValueError("each replacement must be a mapping")
        name = replacement.get("canonical_name")
        entity_type = replacement.get("type")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("replacement canonical_name must be non-empty")
        if entity_type != source["type"]:
            raise ValueError("every split replacement must keep the source entity type")
        specs.append({"canonical_name": name.strip(), "type": entity_type, "state": replacement.get("state")})

    alias_map = _assignment_map(
        "alias_assignments",
        alias_assignments,
        [row["alias_id"] for row in inventory["aliases"]],
        len(specs),
    )
    edge_map = _assignment_map(
        "edge_assignments",
        edge_assignments,
        [row["edge_id"] for row in inventory["edges"]],
        len(specs),
    )
    event_map = _assignment_map(
        "event_assignments",
        event_assignments,
        [row["event_id"] for row in inventory["event_participants"]],
        len(specs),
    )
    if any(row["src_entity"] == entity_id == row["dst_entity"] for row in inventory["edges"]):
        raise ValueError("split of a self-referential edge requires manual re-extraction")

    with db.transaction():
        _invalidate_sources(db, [entity_id], effective_at)
        targets = [
            db.add_entity(spec["canonical_name"], spec["type"], effective_at, "lit10-correction")
            for spec in specs
        ]
        for spec, target in zip(specs, targets):
            if spec["state"] is not None:
                db.add_state(target, effective_at, spec["state"], extractor_version="lit10-correction")
        for alias in inventory["aliases"]:
            for index in alias_map[alias["alias_id"]]:
                db.add_alias(targets[index], alias["surface_form"], effective_at)
        for edge in inventory["edges"]:
            for index in edge_map[edge["edge_id"]]:
                src = targets[index] if edge["src_entity"] == entity_id else edge["src_entity"]
                dst = targets[index] if edge["dst_entity"] == entity_id else edge["dst_entity"]
                db.add_edge(
                    src,
                    dst,
                    edge["rel_type"],
                    edge["label"],
                    effective_at,
                    invalid_at=edge["invalid_at"],
                    extractor_version="lit10-correction",
                )
        participant_by_event = {row["event_id"]: row for row in inventory["event_participants"]}
        for event_id, indexes in event_map.items():
            for index in indexes:
                db.add_event_participant(
                    event_id,
                    targets[index],
                    participant_by_event[event_id]["role"],
                    effective_at,
                )
        correction_id = _record(
            db,
            "split",
            effective_at,
            [entity_id],
            targets,
            {"aliases": alias_map, "edges": edge_map, "events": event_map, "replacements": specs},
            reason,
        )
    return {"correction_id": correction_id, "target_entity_ids": targets}


def merge_entities(
    db,
    entity_ids,
    *,
    effective_at,
    canonical_name,
    state,
    reason="",
    event_roles=None,
):
    """Replace two or more duplicate identities with one bookmark-effective identity."""
    ids = _unique_ids(entity_ids)
    if len(ids) < 2:
        raise ValueError("merge requires at least two distinct source entities")
    if not isinstance(canonical_name, str) or not canonical_name.strip():
        raise ValueError("canonical_name must be non-empty")
    inventory = correction_inventory(db, ids, effective_at)
    types = {row["type"] for row in inventory["entities"]}
    if len(types) != 1:
        raise ValueError("merge sources must have the same type")
    source_set = set(ids)
    overrides = {int(event_id): role for event_id, role in (event_roles or {}).items()}

    roles_by_event = defaultdict(set)
    for row in inventory["event_participants"]:
        roles_by_event[row["event_id"]].add(row["role"])
    unknown_overrides = set(overrides) - set(roles_by_event)
    if unknown_overrides:
        raise ValueError(f"event_roles contains unknown event ids: {sorted(unknown_overrides)}")
    resolved_roles = {}
    for event_id, roles in roles_by_event.items():
        if event_id in overrides:
            resolved_roles[event_id] = overrides[event_id]
        elif len(roles) == 1:
            resolved_roles[event_id] = next(iter(roles))
        else:
            raise ValueError(f"event {event_id} has conflicting roles; event_roles override required")

    with db.transaction():
        _invalidate_sources(db, ids, effective_at)
        target = db.add_entity(canonical_name.strip(), next(iter(types)), effective_at, "lit10-correction")
        if state is not None:
            db.add_state(target, effective_at, state, extractor_version="lit10-correction")

        alias_values = []
        for row in inventory["entities"]:
            alias_values.append(row["canonical_name"])
        alias_values.extend(row["surface_form"] for row in inventory["aliases"])
        seen_aliases = {canonical_name.strip().casefold()}
        for surface in alias_values:
            key = surface.casefold()
            if key not in seen_aliases:
                seen_aliases.add(key)
                db.add_alias(target, surface, effective_at)

        edge_keys = set()
        copied_edges = []
        for edge in inventory["edges"]:
            src = target if edge["src_entity"] in source_set else edge["src_entity"]
            dst = target if edge["dst_entity"] in source_set else edge["dst_entity"]
            if src == dst:
                continue
            key = (src, dst, edge["rel_type"], edge["label"], edge["invalid_at"])
            if key in edge_keys:
                continue
            edge_keys.add(key)
            copied_edges.append(edge["edge_id"])
            db.add_edge(
                src,
                dst,
                edge["rel_type"],
                edge["label"],
                effective_at,
                invalid_at=edge["invalid_at"],
                extractor_version="lit10-correction",
            )
        for event_id, role in resolved_roles.items():
            db.add_event_participant(event_id, target, role, effective_at)
        correction_id = _record(
            db,
            "merge",
            effective_at,
            ids,
            [target],
            {"copied_edge_ids": copied_edges, "event_roles": resolved_roles, "state": state},
            reason,
        )
    return {"correction_id": correction_id, "target_entity_ids": [target]}
