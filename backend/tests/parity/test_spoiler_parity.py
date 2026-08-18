"""LIT-39 cutover gate: the same corpus must preserve spoiler and memory semantics on both stores."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import conninfo

from app.hosted.migrations import apply_migrations

from _adapters import PostgresParityAdapter, SQLiteParityAdapter, load_corpus


pytestmark = pytest.mark.postgres
FIXTURES = Path(__file__).with_name("fixtures")
ROOT = Path(__file__).parents[3]


@pytest.fixture()
def postgres_database():
    admin_dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for the SQLite/PostgreSQL parity gate")
    database_name = f"lit39_{uuid.uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{database_name}"')
    dsn = conninfo.make_conninfo(admin_dsn, dbname=database_name)
    try:
        apply_migrations(dsn)
        yield dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            admin.execute(f'DROP DATABASE "{database_name}"')


@pytest.fixture()
def pair(tmp_path, postgres_database):
    corpus = load_corpus(FIXTURES / "spoiler_parity.json")
    sqlite = SQLiteParityAdapter(tmp_path / "sqlite", corpus)
    postgres = PostgresParityAdapter(postgres_database, corpus)
    try:
        yield corpus, sqlite, postgres
    finally:
        sqlite.close()
        postgres.close()


def _assert_snapshot_is_independently_bounded(snapshot: dict, bookmark: int, corpus: dict) -> None:
    expected_entities = {
        entity["key"]
        for entity in corpus["entities"]
        if entity["revealed_at"] <= bookmark
        and (entity.get("invalid_at") is None or entity["invalid_at"] > bookmark)
    }
    expected_chapters = {
        chapter["key"]
        for chapter in corpus["chapters"]
        if chapter["ordinal"] <= bookmark and not chapter.get("retracted")
    }
    assert set(snapshot["entity_keys"]) == expected_entities
    assert {chapter["key"] for chapter in snapshot["chapters"]} == expected_chapters
    for collection in (
        snapshot["chapters"],
        snapshot["entities"],
        snapshot["relationships"],
        snapshot["events"],
        snapshot["themes"],
        snapshot["summaries"],
    ):
        assert all(item["revealed_at"] <= bookmark for item in collection)
    for relationship in snapshot["relationships"]:
        assert relationship["src"] in expected_entities
        assert relationship["dst"] in expected_entities
    for participants in snapshot["participants"].values():
        assert {item["key"] for item in participants} <= expected_entities
    for aliases in snapshot["aliases"].values():
        assert all(item["revealed_at"] <= bookmark for item in aliases)
    for events in snapshot["events_for"].values():
        assert all(item["revealed_at"] <= bookmark for item in events)
    for state in snapshot["states"].values():
        assert state is None or state["revealed_at"] <= bookmark
    visible_chapters = [
        chapter
        for chapter in corpus["chapters"]
        if chapter["ordinal"] <= bookmark and not chapter.get("retracted")
    ]
    expected_recap = visible_chapters[-1]["rolling_summary"] if visible_chapters else None
    assert snapshot["catch_me_up"] == {
        "as_of_chapter": bookmark,
        "recap": expected_recap,
        "cast_size": sum(entity["type"] == "character" for entity in snapshot["entities"]),
        "open_threads": len(snapshot["relationships"]),
    }
    assert "shadow" in expected_entities if bookmark >= 5 else "shadow" not in expected_entities
    assert "ch6" not in expected_chapters


def test_expected_differences_are_explicit_reviewed_and_never_spoiler_semantics() -> None:
    differences = json.loads((FIXTURES / "expected_differences.json").read_text(encoding="utf-8"))
    assert {item["id"] for item in differences} == {
        "raw-chapter-retention",
        "physical-identifiers-and-vector-encoding",
        "cache-token-bytes",
        "ingest-progress-source",
    }
    assert all(item["status"] == "accepted" and item["reviewed_in"] for item in differences)
    assert all(item["behavioral_impact"] == "none" for item in differences)
    assert {item["scope"] for item in differences} <= {"representation", "storage-omission"}
    assert all("spoiler" not in item["id"] for item in differences)


def test_same_corpus_matches_at_every_bookmark_and_never_reads_ahead(pair) -> None:
    corpus, sqlite, postgres = pair
    assert sqlite.corpus_digest == postgres.corpus_digest

    for bookmark in range(corpus["book"]["max_bookmark"] + 1):
        sqlite_snapshot = sqlite.snapshot(bookmark)
        postgres_snapshot = postgres.snapshot(bookmark)
        assert postgres_snapshot == sqlite_snapshot
        _assert_snapshot_is_independently_bounded(sqlite_snapshot, bookmark, corpus)

        for query in corpus["queries"]:
            sqlite_hits = sqlite.search(bookmark, query)
            postgres_hits = postgres.search(bookmark, query)
            assert postgres_hits == sqlite_hits
            assert all(hit["revealed_at"] <= bookmark for hit in sqlite_hits)
            assert all("retracted" not in hit["key"] for hit in sqlite_hits)

    before = sqlite.snapshot(2)
    after = sqlite.snapshot(3)
    assert "alexander" in before["entity_keys"] and "alexandra" not in before["entity_keys"]
    assert "alexander" not in after["entity_keys"] and "alexandra" in after["entity_keys"]
    assert sqlite.receipts() == postgres.receipts()
    assert sqlite.completion_frontier() == postgres.completion_frontier() == 5


def test_prefilter_canary_blocks_future_and_retracted_neighbors_before_top_k(pair) -> None:
    _corpus, sqlite, postgres = pair
    query = {"vector": [1.0, 0.0, 0.0], "k": 1}
    expected = [{
        "key": "chunk-engagement",
        "chapter": "ch2",
        "revealed_at": 2,
        "text": "Dmitri and Katerina announce their engagement.",
    }]
    assert sqlite.search(2, query) == expected
    assert postgres.search(2, query) == expected


def test_reset_epoch_and_receipts_have_parity(pair) -> None:
    _corpus, sqlite, postgres = pair
    receipts_before = sqlite.receipts()
    assert sqlite.reading_state() == postgres.reading_state()

    assert sqlite.reset_position(expected_epoch=0) == postgres.reset_position(expected_epoch=0)
    assert sqlite.receipts() == postgres.receipts() == receipts_before
    assert sqlite.advance_position(5, "stale", expected_epoch=0) is False
    assert postgres.advance_position(5, "stale", expected_epoch=0) is False
    assert sqlite.advance_position(2, "new-pass", expected_epoch=1) is True
    assert postgres.advance_position(2, "new-pass", expected_epoch=1) is True
    assert sqlite.reading_state() == postgres.reading_state()


def test_cache_identity_transitions_and_reextraction_have_parity(pair) -> None:
    _corpus, sqlite, postgres = pair
    before_sqlite = sqlite.cache_token(2)
    before_postgres = postgres.cache_token(2)
    assert before_sqlite == sqlite.cache_token(2)
    assert before_postgres == postgres.cache_token(2)

    revised = "The engagement is announced with corrected wording."
    sqlite.reextract_summary("ch2", revised)
    postgres.reextract_summary("ch2", revised)

    assert sqlite.cache_token(2) != before_sqlite
    assert postgres.cache_token(2) != before_postgres
    assert sqlite.snapshot(2) == postgres.snapshot(2)


def test_parity_gate_is_required_by_ci_and_the_local_real_database_harness() -> None:
    workflow = (ROOT / ".github" / "workflows" / "postgres-schema.yml").read_text(encoding="utf-8")
    harness = (ROOT / "backend" / "scripts" / "test_postgres.sh").read_text(encoding="utf-8")
    assert "backend/tests/parity" in workflow
    assert "tests/parity" in harness
