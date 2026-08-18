"""The vectors.py ranker seam (ADR 0007 D-A4): the spoiler FILTER stays in BookmarkView.search;
vectors.rank only RANKS already-filtered candidate rows and holds NO DB handle / issues NO SQL.

Written test-first: targets app.memory.vectors (does not exist yet) -> RED, then implemented +
search() refactored to delegate to it -> GREEN.
"""
import json

from app.memory import vectors


def _row(vec, text, revealed_at, chapter_key):
    return {"vec": json.dumps(vec), "text": text, "revealed_at": revealed_at, "chapter_key": chapter_key}


def test_rank_orders_by_cosine_and_truncates():
    rows = [
        _row([1.0, 0.0, 0.0], "exact", 1, "k1"),
        _row([0.7, 0.7, 0.0], "partial", 2, "k2"),
        _row([0.0, 1.0, 0.0], "orthogonal", 3, "k3"),
    ]
    out = vectors.rank(rows, [1.0, 0.0, 0.0], k=2)
    assert len(out) == 2
    assert out[0][1] == "exact" and out[1][1] == "partial"
    # returns the proven 4-tuple (cosine, text, revealed_at, chapter_key)
    assert out[0][2] == 1 and out[0][3] == "k1"


def test_rank_skips_dim_mismatched_rows():
    rows = [_row([1.0, 0.0], "wrong_dim", 1, "k1"), _row([1.0, 0.0, 0.0], "ok", 2, "k2")]
    out = vectors.rank(rows, [1.0, 0.0, 0.0], k=5)
    assert [h[1] for h in out] == ["ok"]


def test_rank_tiebreak_is_deterministic_by_chapter_key():
    # identical vectors -> identical cosine; tie broken by chapter_key (descending, as in search())
    rows = [_row([1.0, 0.0], "b", 1, "kB"), _row([1.0, 0.0], "a", 1, "kA")]
    out = vectors.rank(rows, [1.0, 0.0], k=2)
    assert [h[3] for h in out] == ["kB", "kA"]


def test_vectors_holds_no_db_and_issues_no_sql():
    """Conjunct 2 of the D-A4 acceptance criterion: the ranker takes pre-fetched rows only — it does
    not import sqlite3, hold a connection, or run SQL. Proven by it working on plain dicts with no DB."""
    assert not hasattr(vectors, "sqlite3")
    rows = [_row([1.0, 0.0], "a", 1, "k1"), _row([0.0, 1.0], "b", 2, "k2")]
    out = vectors.rank(rows, [1.0, 0.0], k=1)
    assert out[0][1] == "a"
