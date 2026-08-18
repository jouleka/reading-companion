#!/usr/bin/env python3
"""LIT-6 — the ingestion pipeline: a validated per-chapter extraction -> the LIT-5 store.

For each chapter, in order:
  1. validate the extraction against the contract (reject, never partial-accept);
  2. APPEND-ONCE: skip if this chapter_key+content_hash is already live (delta semantics);
  3. build the bookmark-bounded running roster from the DAL (entities revealed in earlier chapters);
  4. resolve this chapter's entities against the roster (merge/create) — anti-drift;
  5. write entities/aliases/state/edges/events/themes/summary/raw/chunk through the LIT-5 DAL,
     every derived row stamped with the extractor_version (LIT-20 hook).

Atomic-per-chapter + crash-resume is LIT-7's exit criterion; here each chapter validates BEFORE
any write and append-once makes a re-run a no-op. Imports the accepted LIT-5 DAL unchanged.
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lit-5-schema"))
import dal  # noqa: E402

import resolve as R  # noqa: E402
from extract_schema import validate  # noqa: E402
from resolve import _norm  # noqa: E402


def all_entities(view):
    """Running roster from the DAL: every revealed entity (all types) + its aliases."""
    roster = []
    for t in ("character", "place", "faction", "object"):
        for e in view.entities_of_type(t):
            roster.append({"entity_id": e["entity_id"], "canonical_name": e["canonical_name"],
                           "type": e["type"],
                           "aliases": [a["surface_form"] for a in view.aliases_of(e["entity_id"])]})
    return roster


def ingest_chapter(db, ch, extraction, client, chunk_embed=None, resolve_embed=None, threshold=0.82):
    """resolve_embed: embedding fn for resolution layer 4 — pass ONLY a real semantic backend
    (None disables layer 4; the lexical stand-in over-merges siblings). chunk_embed: embedding fn
    for storing RAG vectors (lexical stand-in acceptable for plumbing)."""
    ok, errs = validate(extraction)
    if not ok:
        raise ValueError(f"invalid extraction for {ch['key']}: {errs}")
    ordinal, key = ch["ordinal"], ch["key"]
    text = ch.get("text", "")
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    # 2. append-once: already ingested (same content) -> no-op
    if any(r["chapter_key"] == key and r["content_hash"] == content_hash and r["retracted_at"] is None
           for r in db._audit_all("chapters")):
        return {"skipped": True, "key": key}

    xv = client.extractor_version("cheap")
    href = key.split(":", 1)[1] if ":" in key else key
    db.add_chapter(key, ordinal, href=href, title=ch.get("title", ""),
                   content_hash=content_hash, extractor_version=xv)
    if text:
        db.add_raw(key, ordinal, text, content_hash=content_hash)
    db.add_summary(key, ordinal, extraction["chapter_summary"], extractor_version=xv)

    # 3. + 4. roster (earlier chapters only) -> resolve this chapter's entities
    roster = all_entities(db.view(max(ordinal - 1, 0)))
    decisions = R.resolve_chapter(extraction["entities"], roster, resolve_embed, threshold)

    name2id = {}
    for r in roster:
        name2id[_norm(r["canonical_name"])] = r["entity_id"]
        for a in r["aliases"]:
            name2id.setdefault(_norm(a), r["entity_id"])

    stats = {"merge": 0, "create": 0, "by_method": {}}
    resolved = []   # one record per extracted entity occurrence, for precision/recall scoring
    pending = {}    # create-decision-index -> real entity_id, to resolve within-chapter merges
    for i, (e, d) in enumerate(decisions):
        if d["action"] == "merge":
            eid = d["entity_id"]
            if isinstance(eid, tuple):          # ('PENDING', idx): a same-chapter dup of a NEW entity
                eid = pending[eid[1]]
            stats["merge"] += 1
            for a in e.get("aliases", []):
                if _norm(a) not in name2id:
                    db.add_alias(eid, a, ordinal)
            if e.get("state"):                          # advance the entity_state timeline on merge
                cur = db.view(ordinal).current_state(eid)
                if cur and cur["revealed_at"] < ordinal:
                    db.replace_state(cur["state_id"], at=ordinal,
                                     status={"note": e["state"]}, extractor_version=xv)
                elif not cur:
                    db.add_state(eid, ordinal, {"note": e["state"]}, extractor_version=xv)
        else:
            eid = db.add_entity(e["canonical_name"], e["type"], ordinal, extractor_version=xv)
            pending[i] = eid                    # so a later same-chapter mention can merge to it
            stats["create"] += 1
            for a in e.get("aliases", []):
                db.add_alias(eid, a, ordinal)
            if e.get("state"):
                db.add_state(eid, ordinal, {"note": e["state"]}, extractor_version=xv)
        stats["by_method"][d["method"]] = stats["by_method"].get(d["method"], 0) + 1
        name2id[_norm(e["canonical_name"])] = eid
        for a in e.get("aliases", []):
            name2id.setdefault(_norm(a), eid)
        resolved.append({"chapter": ordinal, "canonical_name": e["canonical_name"],
                         "aliases": list(e.get("aliases", [])), "type": e["type"],
                         "entity_id": eid, "method": d["method"]})

    # 5. edges / events / themes (references resolved against known entities; unknowns flagged)
    unresolved = 0
    for rel in extraction["relationships"]:
        s, t = name2id.get(_norm(rel["src"])), name2id.get(_norm(rel["dst"]))
        if s and t:
            db.add_edge(s, t, rel["rel_type"], rel["label"], ordinal, extractor_version=xv)
        else:
            unresolved += 1
    dropped_parts = 0
    for ev in extraction["events"]:
        parts, seen_p = [], set()                       # dedupe: two surface forms -> one entity_id
        for p in ev.get("participants", []):
            eid = name2id.get(_norm(p))
            if eid and eid not in seen_p:
                seen_p.add(eid)
                parts.append((eid, "participant"))
            elif not eid:
                dropped_parts += 1                      # observability: a ref that didn't resolve
        db.add_event(ev["summary"], ordinal, order_idx=1, participants=parts, extractor_version=xv)
    for th in extraction["themes"]:
        db.add_theme(th["name"], th.get("description") or "", ordinal, extractor_version=xv)

    if chunk_embed and text:
        db.add_chunk(key, ordinal, text[:500], chunk_embed([text[:2000]])[0])

    return {"skipped": False, "key": key, "entities": stats,
            "unresolved_rel_refs": unresolved, "dropped_event_participants": dropped_parts,
            "resolved": resolved}
