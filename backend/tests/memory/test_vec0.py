"""LIT-34 production vec0 backend: spoiler prefilter, parity, and fail-closed recovery."""

from __future__ import annotations

import math
import sqlite3

import pytest

from app.config import Settings
from app.memory import dal, migrations, vectors
from app.memory.store import Store


def _seed(mem) -> None:
    for ordinal in range(1, 5):
        key = f"b:c{ordinal}.xhtml"
        mem.add_chapter(key, ordinal, f"c{ordinal}.xhtml", content_hash=f"h{ordinal}")
    mem.pin_models(embed_model="m1", embed_dim=3, embed_canary=[1.0, 0.0, 0.0])
    # Deliberately non-unit vectors: vec0 must normalize before L2 ranking to preserve cosine order.
    mem.add_chunk("b:c1.xhtml", 1, "past-best", [9.0, 1.0, 0.0], embed_model="m1")
    mem.add_chunk("b:c2.xhtml", 2, "past-second", [3.0, 2.0, 0.0], embed_model="m1")
    mem.add_chunk("b:c2.xhtml", 2, "retracted-chunk", [0.0, 1000.0, 0.0], embed_model="m1")
    mem._retract("chunks", "text=?", ("retracted-chunk",))
    mem.add_chunk("b:c3.xhtml", 3, "future-exact", [100.0, 0.0, 0.0], embed_model="m1")
    mem.add_chunk("b:c4.xhtml", 4, "retracted-chapter", [1000.0, 0.0, 0.0], embed_model="m1")
    mem.retract_chapter("b:c4.xhtml")


def _search(data_dir, backend: str, bookmark: int, k: int = 3):
    store = Store(str(data_dir), vector_backend=backend)
    try:
        with store.book("b") as mem:
            return mem.view(bookmark).search([1.0, 0.0, 0.0], k=k)
    finally:
        store.close()


def test_vec0_is_the_validated_production_default():
    assert Settings(_env_file=None).vector_backend == "vec0"
    with pytest.raises(ValueError):
        Settings(_env_file=None, vector_backend="unknown")
    with pytest.raises(ValueError):
        Store("unused", vector_backend="unknown")


def test_vec0_prefilters_future_retracted_and_non_live_chapter_rows(tmp_path):
    store = Store(str(tmp_path), vector_backend="vec0", trace=True)
    with store.book("b", meta={"title": "B"}) as mem:
        _seed(mem)
        hits = mem.view(2).search([1.0, 0.0, 0.0], k=10)
        assert [hit[1] for hit in hits] == ["past-best", "past-second"]

        knn = [sql for sql in mem.executed_sql if "embedding MATCH" in sql][-1]
        for predicate in (
            "book_id =",
            "revealed_at <=",
            "retracted = 0",
            "chapter_revealed_at <=",
            "chapter_retracted = 0",
        ):
            assert predicate in knn

        # Falsifiability: removing the bookmark bound from the KNN candidate set makes the exact
        # future vector win. This proves the bound above is load-bearing, not decorative post-filtering.
        with mem._writer():
            unbounded = mem._conn.execute(
                "SELECT rowid FROM chunks_vec WHERE embedding MATCH ? AND k = 1 "
                "AND book_id = ? AND retracted = 0 AND chapter_retracted = 0 ORDER BY distance",
                (vectors.serialize([1.0, 0.0, 0.0]), "b"),
            ).fetchone()[0]
            future = mem._conn.execute(
                "SELECT chunk_id FROM chunks WHERE text='future-exact'"
            ).fetchone()[0]
        assert unbounded == future

        with mem._writer():
            without_retraction_bound = mem._conn.execute(
                "SELECT rowid FROM chunks_vec WHERE embedding MATCH ? AND k = 1 AND book_id = ? "
                "AND revealed_at <= 2 AND chapter_revealed_at <= 2 AND chapter_retracted = 0 "
                "ORDER BY distance",
                (vectors.serialize([0.0, 1.0, 0.0]), "b"),
            ).fetchone()[0]
            retracted = mem._conn.execute(
                "SELECT chunk_id FROM chunks WHERE text='retracted-chunk'"
            ).fetchone()[0]
        assert without_retraction_bound == retracted


def test_vec0_matches_bruteforce_cosine_with_float32_tolerance(tmp_path):
    data = tmp_path / "data"
    store = Store(str(data), vector_backend="vec0")
    with store.book("b", meta={"title": "B"}) as mem:
        _seed(mem)
    store.close()

    brute = _search(data, "bruteforce", bookmark=3)
    vec0 = _search(data, "vec0", bookmark=3)
    assert [hit[1:] for hit in vec0] == [hit[1:] for hit in brute]
    assert len(vec0) == len(brute)
    for actual, expected in zip(vec0, brute, strict=True):
        assert math.isclose(actual[0], expected[0], abs_tol=vectors.FLOAT32_TIE_EPS)


def test_vec0_virtual_fact_is_guarded_and_only_known_shadows_are_infra(tmp_path):
    store = Store(str(tmp_path), vector_backend="vec0")
    with store.book("b", meta={"title": "B"}) as mem:
        _seed(mem)
        assert "chunks_vec" in dal.FACT_TABLES
        assert vectors.VEC0_SHADOW_TABLES <= dal.INFRA_TABLES
        with pytest.raises(sqlite3.DatabaseError, match="prohibited|not authorized"):
            mem._conn.execute("SELECT rowid FROM chunks_vec").fetchall()
        with mem._writer():
            actual = {
                row[0]
                for row in mem._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'chunks_vec_%'"
                )
            }
        assert actual == vectors.VEC0_SHADOW_TABLES


def test_vec0_backfills_a_bruteforce_store_idempotently(tmp_path):
    store = Store(str(tmp_path), vector_backend="bruteforce")
    with store.book("b", meta={"title": "B"}) as mem:
        _seed(mem)
        assert "chunks_vec" not in mem._base_tables()
    store.close()

    first = _search(tmp_path, "vec0", bookmark=3)
    second = _search(tmp_path, "vec0", bookmark=3)
    assert first == second


def test_v5_migration_backfills_vec0_from_canonical_chunks(tmp_path):
    store = Store(str(tmp_path), vector_backend="bruteforce")
    with store.book("b", meta={"title": "B"}) as mem:
        _seed(mem)
    store.close()
    raw = sqlite3.connect(store._path("b"))
    raw.execute("DROP TABLE vector_index_meta")
    raw.execute("UPDATE book_meta SET schema_version=5")
    raw.commit()
    raw.close()

    hits = _search(tmp_path, "vec0", bookmark=3)
    assert [hit[1] for hit in hits] == ["future-exact", "past-best", "past-second"]
    assert migrations.CURRENT_VERSION == 6


def test_configured_vec0_refuses_to_fall_back_when_extension_load_fails(tmp_path, monkeypatch):
    def unavailable(_connection):
        raise RuntimeError("sqlite-vec unavailable")

    monkeypatch.setattr(vectors, "load_extension", unavailable)
    with pytest.raises(RuntimeError, match="unavailable"):
        with Store(str(tmp_path), vector_backend="vec0").book("b", meta={"title": "B"}):
            pass


def test_vec0_refuses_partial_index_state(tmp_path):
    store = Store(str(tmp_path), vector_backend="vec0")
    with store.book("b", meta={"title": "B"}) as mem:
        _seed(mem)
        with mem._writer():
            mem._conn.execute("DELETE FROM vector_index_meta")
    store.close()

    with pytest.raises(RuntimeError, match="partial|metadata"):
        with Store(str(tmp_path), vector_backend="vec0").book("b"):
            pass


def test_vec0_refuses_extension_schema_version_mismatch(tmp_path):
    store = Store(str(tmp_path), vector_backend="vec0")
    with store.book("b", meta={"title": "B"}) as mem:
        _seed(mem)
        with mem._writer():
            mem._conn.execute("UPDATE vector_index_meta SET extension_version='v999.0.0'")
    store.close()

    with pytest.raises(RuntimeError, match="metadata mismatch"):
        with Store(str(tmp_path), vector_backend="vec0").book("b"):
            pass


def test_vec0_refuses_index_content_mismatch_instead_of_silent_fallback(tmp_path):
    store = Store(str(tmp_path), vector_backend="vec0")
    with store.book("b", meta={"title": "B"}) as mem:
        _seed(mem)
        with mem._writer():
            mem._conn.execute(
                "DELETE FROM chunks_vec WHERE rowid=(SELECT MIN(rowid) FROM chunks_vec)"
            )
    store.close()

    with pytest.raises(RuntimeError, match="mismatch"):
        with Store(str(tmp_path), vector_backend="vec0").book("b"):
            pass


def test_vec0_chunk_insert_is_atomic_on_index_failure(tmp_path, monkeypatch):
    store = Store(str(tmp_path), vector_backend="vec0")
    with store.book("b", meta={"title": "B"}) as mem:
        mem.add_chapter("b:c1.xhtml", 1, "c1.xhtml", content_hash="h1")
        mem.pin_models(embed_model="m1", embed_dim=3, embed_canary=[1.0, 0.0, 0.0])

        def fail(*_args, **_kwargs):
            raise RuntimeError("injected vec0 failure")

        monkeypatch.setattr(mem, "_vec0_insert", fail)
        with pytest.raises(RuntimeError, match="injected"):
            mem.add_chunk("b:c1.xhtml", 1, "must-roll-back", [1.0, 0.0, 0.0], embed_model="m1")
        assert mem._audit_all("chunks") == []
        with mem._writer():
            assert mem._conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0] == 0
