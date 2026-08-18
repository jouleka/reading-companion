#!/usr/bin/env python3
"""LIT-20 — model identity + version-pinning + safe-swap policy on top of the one provider-agnostic
LLM/embedding interface (spikes/lit-6-extraction/llm.py).

THE PROBLEM: the pluggable interface makes swapping a provider/model an env change — operationally
trivial, but SEMANTICALLY UNSAFE. Swapping the EMBEDDING model silently breaks entity-resolution +
RAG (cosine across two embedding spaces is meaningless, with NO error). Mixing EXTRACTOR models across
a book yields inconsistent entity granularity. So:

DECISION: PIN the extractor + embedding model per book at first ingestion (stamped in book_meta +
on every vector). A later model change is NEVER silently mixed — it is detected and forces an
explicit, costed migration. The large/synthesis model is the one component free to change mid-book
(synthesis is stateless: it re-reads the bookmark-filtered facts each time).

This module is the policy; the interface itself is llm.LLMClient (one tier-aware complete() + embed()
+ version). Stdlib only.
"""

# decisions
OK = "OK"                       # may change freely mid-book
FORCE_RE_EMBED = "FORCE_RE_EMBED"      # all vectors invalid -> re-embed before any KNN
FORCE_RE_EXTRACT = "FORCE_RE_EXTRACT"  # granularity drift -> explicit costed re-extract
MIGRATE_SCHEMA = "MIGRATE_SCHEMA"      # schema/prompt change -> migrate (LIT-19)

# The safe-swap matrix: per component, may-it-change-mid-book + what a change triggers.
SAFE_SWAP_MATRIX = {
    "synth_model":     ("YES — stateless; re-reads the bookmark-filtered facts each call", OK),
    "extractor_model": ("NO — pinned; silent mixing = granularity drift", FORCE_RE_EXTRACT),
    "embed_model":     ("NO — pinned; cosine across two embedding spaces is meaningless", FORCE_RE_EMBED),
    "embed_dim":       ("NO — a dim change IS an embed-model change", FORCE_RE_EMBED),
    "schema_version":  ("NO — append-only schema; a change needs migration", MIGRATE_SCHEMA),
}


def current_identity(client):
    """The model identity of a live client. `embed_model` is the FULL embed identity
    (provider@base_url:model — never a bare name, never a configured-but-unhonored name), plus the
    DIM and a CANARY fingerprint of the embedder's actual output. Shape matches book_meta's columns."""
    v = client.version            # {provider, cheap, large, embed=full embed identity}
    dim = len(client.embed(["__probe__"])[0][0])
    return {"extractor_model": f"{v['provider']}:{v['cheap']}",
            "synth_model": f"{v['provider']}:{v['large']}",
            "embed_model": v["embed"], "embed_dim": dim,
            "embed_canary": client.embed_canary()}


CANARY_COSINE_MIN = 0.999   # below this, the embedder's space changed -> FORCE_RE_EMBED


def _cos(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def safe_swap(pinned, current):
    """Compare a book's PINNED identity to a CURRENT client identity. Returns a list of
    (component, decision); only `[('none', OK)]` means safe to proceed. The embedding check is the
    hard one: a change to the full embed identity, the dim, OR a canary COSINE below CANARY_COSINE_MIN
    forces a re-embed — so a same-NAME space change (stub-vs-real, base_url repoint, silent re-train)
    is caught, while a real embedder's run-to-run float noise (cosine ~0.99996) does NOT false-trigger.
    Extractor change forces re-extract; synth change is OK for spoiler-safety (recap cache keys on it)."""
    out = []
    pc, cc = pinned.get("embed_canary"), current.get("embed_canary")
    canary_drift = bool(pc and cc) and _cos(pc, cc) < CANARY_COSINE_MIN
    if (current.get("embed_model") != pinned.get("embed_model")
            or current.get("embed_dim") != pinned.get("embed_dim")
            or canary_drift):
        out.append(("embed_model", FORCE_RE_EMBED))
    if current.get("extractor_model") != pinned.get("extractor_model"):
        out.append(("extractor_model", FORCE_RE_EXTRACT))
    if current.get("schema_version") is not None and pinned.get("schema_version") is not None \
            and current["schema_version"] != pinned["schema_version"]:
        out.append(("schema_version", MIGRATE_SCHEMA))
    # synth_model differences are intentionally NOT flagged for spoiler-safety (recap-cache keys on it).
    return out or [("none", OK)]


def render_matrix():
    lines = ["component         may change mid-book?                                            on change"]
    for comp, (note, dec) in SAFE_SWAP_MATRIX.items():
        lines.append(f"  {comp:15s}  {note:60s}  {dec}")
    return "\n".join(lines)
