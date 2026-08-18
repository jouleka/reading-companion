"""LIT-8 vector 4 — recap-cache coherence. Lifted near-verbatim from
``spikes/lit-8-spoiler-eval/harness.py`` (ADR 0007 D-A1 group (a)).

A recap cached at bookmark B must be invalidated when a later ``invalid_at`` or a re-extraction
retroactively changes what is valid at B. The cache key hashes the live ``(id, invalid_at, content
fingerprint)`` set visible at B across every fact table that affects a recap/RAG, so a retroactive
``invalid_at``, a transaction-time retraction, an in-place ``reextract_entity``, a new alias or a
re-chunk all flip the key -> cache miss -> regenerate.

Pass-2 review hardening (beyond the spike):
  * ``chapters`` joined the keyed set with a ``revealed_at:content_hash:title`` fingerprint — the
    live-chapter SEMIJOIN makes summary/raw/chunk visibility depend on ``chapters.revealed_at``, and
    the ``add_chapter`` UPDATE branch can move it WITHOUT touching ``recorded_at``, so the spike's
    snapshot served a stale recap across a reveal move (PROVED).
  * ``events`` fingerprint includes ``order_idx`` (it orders ``timeline()`` = the recap's KEY EVENTS).
  * ``event_participants`` are hashed only when the link, parent event, and participant identity are
    all visible — future correction links must not churn earlier bookmarks.
  * fail-closed int-not-bool bookmark guard (mirrors the DAL's).

``validity_snapshot`` reads the named audit hatch (``db._audit_all``) — it is an audit/cache-coherence
computation over ground truth, NOT a view read; like the rest of the gate it runs under the per-book
lock.
"""
import hashlib

# (table, id column) for every fact table whose live membership at B affects a recap/RAG.
_KEYED = (("entities", "entity_id"), ("aliases", "alias_id"), ("edges", "edge_id"),
          ("events", "event_id"), ("entity_state", "state_id"), ("themes", "theme_id"),
          ("chapter_summaries", "summary_id"), ("raw_chapters", "chapter_key"),
          ("chunks", "chunk_id"), ("chapters", "chapter_key"))
# Extra recap-visible fingerprint columns the default recorded_at/content_hash misses (pass-2):
# chapters' UPDATE branch moves revealed_at without touching recorded_at; order_idx orders timeline().
_FP_EXTRA = {"chapters": ("revealed_at", "content_hash", "title"), "events": ("order_idx",)}


def _require_int_bookmark(bm):
    if not isinstance(bm, int) or isinstance(bm, bool):
        raise ValueError(f"bookmark must be an int chapter ordinal, got {bm!r}")


def validity_snapshot(db, bm):
    """Hash the LIVE set visible at bookmark ``bm`` across EVERY fact table that affects a recap/RAG,
    with a CONTENT fingerprint (``recorded_at`` / ``content_hash`` + the per-table extras above) so an
    in-place re-extraction, a new alias, a re-chunk, a raw-text edit, a chapter reveal move, or an
    event reorder ALSO flips the key — not just retraction / retroactive ``invalid_at`` (ADR 0004
    review MED #4 + pass-2 F4)."""
    _require_int_bookmark(bm)
    parts = []
    live_events = set()
    live_entities = set()
    for table, idcol in _KEYED:
        for r in db._audit_all(table):
            k = r.keys()
            if "revealed_at" in k and r["revealed_at"] > bm:
                continue
            if "retracted_at" in k and r["retracted_at"] is not None:
                continue
            if "invalid_at" in k and r["invalid_at"] is not None and r["invalid_at"] <= bm:
                continue
            if table == "events":
                live_events.add(r[idcol])
            if table == "entities":
                live_entities.add(r[idcol])
            # A row reaching this point has invalid_at NULL or > bm. Hash the raw value ONLY where a
            # view read actually SURFACES it (edges: relationships() returns invalid_at) — for the
            # other valid-time tables nothing visible at bm changes when a FUTURE-dated invalid_at is
            # stamped, and the pipeline's replace_state stamps one on every state advance, which
            # churned every earlier bookmark's key per ingested chapter (pass-3 F-P3-2, proved).
            inv = r["invalid_at"] if (table == "edges" and "invalid_at" in k) else None
            fp = r["recorded_at"] if "recorded_at" in k else (r["content_hash"] if "content_hash" in k else "")
            extra = ":".join(str(r[c]) for c in _FP_EXTRA.get(table, ()))
            parts.append(f"{table}:{r[idcol]}:{inv}:{fp}:{extra}")
    # Participant links gained their own reveal frontier in LIT-10: a correction can attach a new
    # identity to an old event without making that identity/link visible at earlier bookmarks.
    for r in db._audit_all("event_participants"):
        if (r["event_id"] in live_events and r["entity_id"] in live_entities
                and r["revealed_at"] <= bm):
            parts.append(f"event_participants:{r['event_id']}-{r['entity_id']}")
    parts.sort()
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def cache_key(book_id, bm, snapshot, synth_model="", recap_prompt_version="", atom_set_version=""):
    """The recap-cache key (ADR 0007 Inv 7). The recap is produced by the synth (large) model from the
    bookmark-filtered facts; the synth model may change freely for spoiler-safety, but the CACHED recap
    must miss when it (or the recap prompt) changes — else a model upgrade silently has no effect
    (LIT-20 review). ``atom_set_version`` keys the segmentation/atom numbering so a renumber forces a
    miss (Inv 7); ``bm`` here is the clamped, version-current ``effective_bookmark`` (D-A9). NB for the
    endpoints arc: pass the PINNED ``synth_model`` (from ``pinned_identity()``) — the ``""`` default
    exists for eval convenience and would never miss on a model swap if a runtime caller forgot it."""
    _require_int_bookmark(bm)
    return f"{book_id}:{bm}:{snapshot}:{synth_model}:{recap_prompt_version}:{atom_set_version}"
