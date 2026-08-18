"""LIT-6 — the ingestion pipeline: a validated per-chapter extraction -> the LIT-5 store. Re-ported
from ``spikes/lit-6-extraction/pipeline.py`` (ADR 0007 D-A1 (b)) with the named, behaviour-relevant
production changes:

  * **append-once is re-sourced off the explicit v2 ``ingested_chapters`` completion marker**, NOT the
    ``_audit_all`` table-dump or a legacy chapter row (D-A3/LIT-7). It is the SOLE append-once guard for
    derived rows, so the early-return is load-bearing;
  * **the embedding model is PINNED before the first ``add_chunk``** (Inv 6) via LIT-20
    ``current_identity`` (pinned once per book; KNN then compares same-(model,dim) only);
  * **roster comes from ``mem.view(ordinal-1)``** so extraction stays bookmark-agnostic — ``revealed_at``
    is the chapter ordinal at ingest, no future/whole-book context (Inv 11);
  * **cost + ingest-progress are atomically reconciled through the Catalog** from a durable receipt only
    after the per-book memory transaction commits;
  * **LIT-7 crash atomicity:** every chapter-derived row plus the explicit completion marker is written
    inside one explicit MemoryDB transaction. A mid-write exception or process death leaves no partial
    facts, while a double-ingest of a committed chapter remains an exact no-op.
"""
from app.ingest.extraction import resolve as R
from app.ingest.extraction.chapter_text import content_hash_of
from app.ingest.extraction.resolve import _norm
from app.ingest.extraction.schema import Extraction
from app.llm import versioning


def all_entities(view):
    """Running roster from the DAL: every revealed entity (all types) + its aliases. Pass the
    bookmark-bounded ``mem.view(ordinal-1)`` so only earlier chapters' cast is visible (Inv 11)."""
    roster = []
    for t in ("character", "place", "faction", "object"):
        for e in view.entities_of_type(t):
            roster.append({"entity_id": e["entity_id"], "canonical_name": e["canonical_name"],
                           "type": e["type"],
                           "aliases": [a["surface_form"] for a in view.aliases_of(e["entity_id"])]})
    return roster


def _ensure_pinned(mem, identity):
    """Pin the extractor/embedding identity ONCE per book, BEFORE any chunk (Inv 6). Idempotent: if the
    book already carries an embed pin, skip (avoids a redundant canary embed call per chapter)."""
    if (mem.pinned_identity() or {}).get("embed_model"):
        return
    if identity is None:
        raise RuntimeError("model identity must be prepared before the chapter transaction")
    mem.pin_models(**identity)


def _changed_content_error(key):
    return ValueError(
        f"chapter {key!r} is already present without a matching LIT-7 completion marker or at a "
        f"different content_hash. It may be a pre-LIT-7 partial chapter; re-import/rebuild the book "
        f"rather than promoting an indistinguishable legacy row or appending duplicate facts.")


def prepare_chapter(ch, extraction, client, *, roster, usage=None, usd=0.0, embed_fn=None,
                    resolve_embed=None, identity=None, threshold=0.82, embed_chars=2000):
    """Validate and prepare every model-derived value without accepting a ``MemoryDB`` handle.

    Callers run this outside ``Store.book()``. The returned object contains only values consumed by the
    database-only :func:`ingest_chapter` commit phase, making network/model callbacks impossible there.
    """
    extraction = Extraction.model_validate(extraction).model_dump(mode="json")
    text = ch.get("text", "") or ""
    actual_content_hash = content_hash_of(text)
    if ch.get("content_hash") not in (None, actual_content_hash):
        raise ValueError("chapter content_hash does not match its text")
    if resolve_embed is not None and client.embed_identity().startswith("stub:"):
        raise ValueError(
            "resolve_embed (layer-4 resolution embedding) requires a REAL semantic embedder; the lexical "
            "stub over-merges siblings (Dmitri/Ivan cosine ~0.82) and must never be the merge authority")
    prepared_identity = identity or versioning.current_identity(client)
    extractor_version = client.extractor_version("cheap")
    completion_cost = None
    if usage:
        completion_cost = {"model": extractor_version,
                           "input_tokens": usage.get("in", 0), "output_tokens": usage.get("out", 0),
                           "usd": usd}
    chunk_embed = embed_fn or (lambda texts: client.embed(texts)[0])
    chunk_vec = chunk_embed([text[:embed_chars]])[0] if text else None
    decisions = R.resolve_chapter(extraction["entities"], roster, resolve_embed, threshold)
    return {
        "chapter_key": ch["key"],
        "ordinal": ch["ordinal"],
        "content_hash": actual_content_hash,
        "extraction": extraction,
        "identity": prepared_identity,
        "extractor_version": extractor_version,
        "roster": roster,
        "decisions": decisions,
        "chunk_vec": chunk_vec,
        "completion_cost": completion_cost,
    }


def ingest_chapter(mem, ch, prepared, *, chunk_chars=500):
    """Commit one prepared chapter atomically using database operations only.

    Every memory row, including the completion/cost receipt marker, commits in one SQLite transaction.
    The caller releases ``Store.book()`` before reconciling the external catalog from that receipt.
    """
    ordinal, key = ch["ordinal"], ch["key"]
    text = ch.get("text", "") or ""
    content_hash = content_hash_of(text)
    if (prepared["chapter_key"] != key or prepared["ordinal"] != ordinal
            or prepared["content_hash"] != content_hash
            or ch.get("content_hash") not in (None, content_hash)):
        raise ValueError(
            f"prepared chapter mismatch: prepared {prepared['chapter_key']!r}@{prepared['ordinal']}/"
            f"{prepared['content_hash']}, commit requested {key!r}@{ordinal}/{content_hash}"
        )
    receipt = mem.chapter_completion(key, ordinal, content_hash)
    if receipt is not None:
        return {"skipped": True, "key": key, "ordinal": ordinal}
    if mem.chapter_live(key):
        raise _changed_content_error(key)
    with mem.transaction():
        result = _write_chapter(
            mem,
            ch,
            prepared["extraction"],
            identity=prepared["identity"],
            extractor_version=prepared["extractor_version"],
            roster=prepared["roster"],
            decisions=prepared["decisions"],
            chunk_vec=prepared["chunk_vec"],
            completion_cost=prepared["completion_cost"],
            chunk_chars=chunk_chars,
        )
    if mem.chapter_completion(key, ordinal, content_hash) is None:
        raise RuntimeError(f"chapter {key!r} committed without its LIT-7 completion marker")
    return result


def _write_chapter(mem, ch, extraction, *, identity, extractor_version, roster, decisions,
                   chunk_vec, completion_cost, chunk_chars=500):
    """Ingest ONE chapter's validated extraction into ``mem`` (a sole-owned MemoryDB, accessed under the
    Store's per-book lock and ``mem.transaction()``). ``extraction`` has already passed the schema.

    External identity, resolution, and embedding work is already prepared before this helper opens.
    This private helper writes memory.db only; the worker updates the catalog after releasing the lock.
    """
    ordinal, key = ch["ordinal"], ch["key"]
    text = ch.get("text", "") or ""
    content_hash = content_hash_of(text)

    # append-once (D-A3): already live at this content_hash -> exact no-op (the SOLE guard for derived rows)
    if mem.chapter_is_ingested(key, content_hash):
        return {"skipped": True, "key": key, "ordinal": ordinal}
    # changed-content re-ingest is NOT supported here: fail loud + write nothing (rather than UPDATE the
    # chapter row then crash on the raw_chapters PK, freezing stale rows). Atomic re-extraction is LIT-19;
    # retract_chapter alone does NOT free the chapter_key PK or retract derived rows, so retract-then-
    # reingest is NOT a working remedy and must not be prescribed.
    if mem.chapter_live(key):
        raise _changed_content_error(key)

    _ensure_pinned(mem, identity)                          # PIN before the first add_chunk (Inv 6)

    xv = extractor_version
    href = ch.get("href") or (key.split(":", 1)[1] if ":" in key else key)
    # FK parent; the explicit completion marker is written last and remains invisible until commit.
    mem.add_chapter(key, ordinal, href=href, title=ch.get("title", ""),
                    part_label=ch.get("part_label", ""), content_hash=content_hash, extractor_version=xv)
    if text:
        mem.add_raw(key, ordinal, text, content_hash=content_hash)
    mem.add_summary(key, ordinal, extraction["chapter_summary"], extractor_version=xv)

    name2id = {}
    for r in roster:
        name2id[_norm(r["canonical_name"])] = r["entity_id"]
        for a in r["aliases"]:
            name2id.setdefault(_norm(a), r["entity_id"])

    stats = {"merge": 0, "create": 0, "by_method": {}}
    resolved = []                                          # one record per occurrence, for P/R scoring
    pending = {}                                           # create-decision-index -> real entity_id
    state_notes = {}                                       # eid -> latest state note this chapter (last wins)
    for i, (e, d) in enumerate(decisions):
        if d["action"] == "merge":
            eid = d["entity_id"]
            if isinstance(eid, tuple):                     # ('PENDING', idx): same-chapter dup of a NEW entity
                eid = pending[eid[1]]
            stats["merge"] += 1
            # Preserve a newly observed canonical variant (e.g. "Father Zossima") as an alias of the
            # resolved identity so search still finds the exact surface form the reader saw.
            if _norm(e["canonical_name"]) not in name2id:
                mem.add_alias(eid, e["canonical_name"], ordinal)
            for a in e.get("aliases", []):
                if _norm(a) not in name2id:
                    mem.add_alias(eid, a, ordinal)
        else:
            eid = mem.add_entity(e["canonical_name"], e["type"], ordinal, extractor_version=xv)
            pending[i] = eid                               # so a later same-chapter mention can merge to it
            stats["create"] += 1
            for a in e.get("aliases", []):
                mem.add_alias(eid, a, ordinal)
        if e.get("state"):
            state_notes[eid] = e["state"]                  # defer: write once per entity (last mention wins)
        stats["by_method"][d["method"]] = stats["by_method"].get(d["method"], 0) + 1
        name2id[_norm(e["canonical_name"])] = eid
        for a in e.get("aliases", []):
            name2id.setdefault(_norm(a), eid)
        resolved.append({"chapter": ordinal, "canonical_name": e["canonical_name"],
                         "aliases": list(e.get("aliases", [])), "type": e["type"],
                         "entity_id": eid, "method": d["method"]})

    # entity_state: ONE row per entity for this chapter (the LAST mention's state wins — no silent drop of
    # a second same-chapter state). Advance an earlier-chapter state, else create. A same-ordinal replace
    # is neither needed (one write per eid) nor legal (it would violate the invalid_at > revealed_at CHECK).
    for eid, note in state_notes.items():
        cur = mem.view(ordinal).current_state(eid)
        if cur is None:
            mem.add_state(eid, ordinal, {"note": note}, extractor_version=xv)
        elif cur["revealed_at"] < ordinal:
            mem.replace_state(cur["state_id"], at=ordinal, status={"note": note}, extractor_version=xv)

    # edges / events / themes (references resolved against known entities; unknowns flagged, not crashed)
    unresolved = 0
    for rel in extraction["relationships"]:
        s, t = name2id.get(_norm(rel["src"])), name2id.get(_norm(rel["dst"]))
        if s and t:
            mem.add_edge(s, t, rel["rel_type"], rel["label"], ordinal, extractor_version=xv)
        else:
            unresolved += 1
    dropped_parts = 0
    for idx, ev in enumerate(extraction["events"], start=1):
        parts, seen_p = [], set()                          # dedupe: two surface forms -> one entity_id
        for p in ev.get("participants", []):
            eid = name2id.get(_norm(p))
            if eid and eid not in seen_p:
                seen_p.add(eid)
                parts.append((eid, "participant"))
            elif not eid:
                dropped_parts += 1                         # observability: a ref that didn't resolve
        mem.add_event(ev["summary"], ordinal, order_idx=idx, participants=parts, extractor_version=xv)
    for th in extraction["themes"]:
        mem.add_theme(th["name"], th.get("description") or "", ordinal, extractor_version=xv)

    if text:                                               # RAG chunk (pinned-space vector)
        mem.add_chunk(key, ordinal, text[:chunk_chars], chunk_vec)

    mem.mark_chapter_ingested(key, content_hash, cost=completion_cost)  # LAST: durable done/receipt

    return {"skipped": False, "key": key, "ordinal": ordinal, "entities": stats,
            "unresolved_rel_refs": unresolved, "dropped_event_participants": dropped_parts,
            "resolved": resolved}
