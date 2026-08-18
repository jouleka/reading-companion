"""LIT-8 vector 2 — RAG / quote-path leak eval. Lifted near-verbatim from
``spikes/lit-8-spoiler-eval/harness.py`` (ADR 0007 D-A1 group (a)).

``search()`` must never return a chunk with ``revealed_at > bookmark``. In-text foreshadow inside an
ALREADY-READ chunk ("...which I shall describe later") is reader-parity-safe (the reader read it too) —
COUNTED (informational), not failed. The genuine residual is sub-chapter granularity (-> LIT-12) +
synthesis elaboration (vector 3). The drop-filter / raw-deny D-A4 BruteForce probes live in the
merge-gate test.

LOCK CAUTION (pass-2 review): ``rag_eval`` calls ``embed`` INSIDE the per-book-lock session that owns
``db``. That is fine for the eval (offline stub / a dedicated eval run), but a NETWORK embedder here
would hold the book's lock across IO — runtime code must never do that (D-A3): the D-A11 ``/search``
route embeds the query BEFORE entering ``store.book()`` and passes only the vector in.
"""
import re

FORESHADOW_RE = re.compile(
    r"\b(later|afterwards?|would (?:not )?\w+|destined|shall (?:see|describe|tell)|"
    r"years? (?:later|after)|in time|eventually|as we shall)\b", re.I)

_QUERIES = ["the murder and the family fortune", "Alyosha at the monastery and the elder",
            "the marriage and the wife who ran away", "money inheritance lawsuit"]


def rag_eval(db, max_bm, embed):
    """``search()`` must never return a chunk with ``revealed_at > bookmark``. Also detect in-text
    foreshadow (reader-parity-safe; reported, not failed). ``embed`` is ``texts -> list[vec]`` (the
    chunk/query embedder); returns ``(reads, leaks, foreshadow)``."""
    reads = leaks = foreshadow = 0
    for bm in range(1, max_bm + 1):
        v = db.view(bm)
        for q in _QUERIES:
            qv = embed([q])[0]
            for score, text, rev, key in v.search(qv, k=5):
                reads += 1
                if rev > bm:
                    leaks += 1
                if FORESHADOW_RE.search(text):
                    foreshadow += 1
    return reads, leaks, foreshadow
