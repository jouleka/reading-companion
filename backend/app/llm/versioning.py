"""LIT-20 — model identity + version-pinning + safe-swap policy on top of the one provider-agnostic
LLM/embedding interface (``app.llm.client``). Lifted near-verbatim from
``spikes/lit-20-llm-interface/versioning.py`` (ADR 0007 D-A1 group (a): pure, behaviour-defining
safety logic). Stdlib only.

THE PROBLEM: the pluggable interface makes swapping a provider/model an env change — operationally
trivial, but SEMANTICALLY UNSAFE. Swapping the EMBEDDING model silently breaks entity-resolution +
RAG (cosine across two embedding spaces is meaningless, with NO error). Mixing EXTRACTOR models across
a book yields inconsistent entity granularity. So:

DECISION: PIN the extractor + embedding model per book at first ingestion (stamped in ``book_meta`` +
on every vector). A later model change is NEVER silently mixed — it is detected and forces an
explicit, costed migration. The large/synthesis model is the one component free to change mid-book
(synthesis is stateless: it re-reads the bookmark-filtered facts each time).
"""
import math

# decisions
OK = "OK"                                # may change freely mid-book
FORCE_RE_EMBED = "FORCE_RE_EMBED"        # all vectors invalid -> re-embed before any KNN
FORCE_RE_EXTRACT = "FORCE_RE_EXTRACT"    # granularity drift -> explicit costed re-extract
MIGRATE_SCHEMA = "MIGRATE_SCHEMA"        # schema/prompt change -> migrate (LIT-19)

# The safe-swap matrix: per component, may-it-change-mid-book + what a change triggers.
SAFE_SWAP_MATRIX = {
    "synth_model":     ("YES — stateless; re-reads the bookmark-filtered facts each call", OK),
    "extractor_model": ("NO — pinned; silent mixing = granularity drift", FORCE_RE_EXTRACT),
    "embed_model":     ("NO — pinned; cosine across two embedding spaces is meaningless", FORCE_RE_EMBED),
    "embed_dim":       ("NO — a dim change IS an embed-model change", FORCE_RE_EMBED),
    "schema_version":  ("NO — append-only schema; a change needs migration", MIGRATE_SCHEMA),
}

CANARY_COSINE_MIN = 0.999   # below this, the embedder's space changed -> FORCE_RE_EMBED
EMBED_CANARY_TEXT = "__lit20_embed_canary__"


class ModelMigrationRequired(RuntimeError):
    """A configured model cannot safely extend or query an existing book's model space."""

    def __init__(self, decisions):
        self.decisions = tuple(decisions)
        super().__init__(self.public_detail()["message"])

    def public_detail(self) -> dict[str, str]:
        return {
            "code": "model_migration_required",
            "message": (
                "This book's AI memory uses different model settings. Restore the previous "
                "settings or explicitly rebuild its AI memory before continuing. Reading "
                "and existing memory remain available."
            ),
        }


def current_identity(client, embed_call=None):
    """The model identity of a live client. ``embed_model`` is the FULL embed identity
    (provider@base_url:model — never a bare name, never a configured-but-unhonored name), plus the
    DIM and a CANARY fingerprint of the embedder's actual output. Shape matches ``book_meta``'s
    pinned columns, so it slots straight into ``MemoryDB.pin_models(**identity)``."""
    v = client.version            # {provider, cheap, large, embed=full embed identity}
    embed_call = embed_call or client.embed
    canary = embed_call([EMBED_CANARY_TEXT])[0][0]
    return {"extractor_model": f"{v['provider']}:{v['cheap']}",
            "synth_model": f"{v['provider']}:{v['large']}",
            "embed_model": v["embed"], "embed_dim": len(canary),
            "embed_canary": canary}


def _cos(a, b):
    try:
        if not a or not b or len(a) != len(b):
            return 0.0
        a, b = [float(x) for x in a], [float(x) for x in b]
        if not all(math.isfinite(x) for x in (*a, *b)):
            return 0.0
        na, nb = math.hypot(*a), math.hypot(*b)
        if not na or not nb or not math.isfinite(na) or not math.isfinite(nb):
            return 0.0
        return sum((x / na) * (y / nb) for x, y in zip(a, b))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _has_pin(pinned):
    return bool(pinned and any(pinned.get(key) is not None for key in (
        "extractor_model", "embed_model", "embed_dim", "embed_canary"
    )))


def assert_compatible_config(pinned, client, *, check_extractor=True):
    """Reject a known configuration change before any paid IO; synthesis may change freely."""
    if not _has_pin(pinned):
        return
    decisions = []
    if check_extractor and pinned.get("extractor_model") != client.extractor_version("cheap"):
        decisions.append(("extractor_model", FORCE_RE_EXTRACT))
    if pinned.get("embed_model") != client.embed_identity():
        decisions.append(("embed_model", FORCE_RE_EMBED))
    if decisions:
        raise ModelMigrationRequired(decisions)


def assert_compatible_identity(pinned, current, *, check_extractor=True):
    """Fence a prepared embedding space at use/commit time, including same-name canary drift."""
    if not _has_pin(pinned):
        return
    decisions = [
        (component, decision) for component, decision in safe_swap(pinned, current)
        if decision != OK and (check_extractor or component == "embed_model")
    ]
    if decisions:
        raise ModelMigrationRequired(decisions)


def safe_swap(pinned, current):
    """Compare a book's PINNED identity to a CURRENT client identity. Returns a list of
    ``(component, decision)``; only ``[('none', OK)]`` means safe to proceed. The embedding check is
    the hard one: a change to the full embed identity, the dim, OR a canary COSINE below
    ``CANARY_COSINE_MIN`` forces a re-embed — so a same-NAME space change (stub-vs-real, base_url
    repoint, silent re-train) is caught, while a real embedder's run-to-run float noise
    (cosine ~0.99996) does NOT false-trigger. Extractor change forces re-extract; synth change is OK
    for spoiler-safety (recap cache keys on it)."""
    out = []
    pc, cc = pinned.get("embed_canary"), current.get("embed_canary")
    model_or_dim_changed = (current.get("embed_model") != pinned.get("embed_model")
                            or current.get("embed_dim") != pinned.get("embed_dim"))
    # The canary is the ONLY proof of "same embedding space" once the model NAME and dim match — it
    # catches a same-name re-train / base_url repoint / stub-vs-real that name+dim cannot see. A canary
    # ABSENT on either side therefore CANNOT certify same-space, so it must fail CLOSED (re-embed),
    # never silently proceed to a meaningless cross-space KNN. Only a present-on-both, high-cosine
    # canary clears the check. (Pass-1 review HIGH: the old `bool(pc and cc) and ...` failed OPEN on a
    # NULL pinned canary — reachable via dal.pin_models(embed_canary=None).)
    canary_uncertain = (not pc) or (not cc) or _cos(pc, cc) < CANARY_COSINE_MIN
    if model_or_dim_changed or canary_uncertain:
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
