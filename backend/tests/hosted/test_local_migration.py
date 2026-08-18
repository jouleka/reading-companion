from __future__ import annotations

import uuid
import os
import json
import time

import psycopg
import pytest
from psycopg import conninfo

import app.hosted.local_migration as migration_module
from app.hosted.local_migration import (
    backup_library,
    import_archive,
    plan_archive,
    plan_checksum,
    rollback_archive,
)
from app.hosted.migrations import apply_migrations
from app.hosted.storage import EncryptedFilesystemStorage
from app.hosted.tenant.models import OwnerId
from app.lifecycle.archive import backup_book
from tests.lifecycle.test_archive import _seed


@pytest.fixture()
def database():
    admin_dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for the real PostgreSQL migration test")
    database_name = f"lit50_{uuid.uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{database_name}"')
    dsn = conninfo.make_conninfo(admin_dsn, dbname=database_name)
    try:
        apply_migrations(dsn)
        yield dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()",
                (database_name,),
            )
            admin.execute(f'DROP DATABASE "{database_name}"')


def _archive(tmp_path, *, title="Fixture"):
    source = tmp_path / "source"
    store, catalog, book_id = _seed(source, title=title)
    with store.book(book_id) as memory:
        memory.pin_models(embed_model="fixture-embed", embed_dim=3, embed_canary=[1.0, 0.0, 0.0])
        alex = memory.add_entity("Alex", "character", 1)
        morgan = memory.add_entity("Morgan", "character", 1)
        alias = memory.add_alias(alex, "Lex", 1)
        edge = memory.add_edge(alex, morgan, "friendship", "old friends", 1)
        event = memory.add_event("Alex crosses the bridge.", 1, 0, participants=[(alex, "subject")])
        memory.add_theme("Crossings", "A recurring threshold.", 1)
        memory.add_state(alex, 1, {"location": "bridge"})
        memory._ins(
            "entity_corrections",
            book_id=book_id,
            kind="replace",
            revealed_at=1,
            source_entity_ids_json=json.dumps([alex]),
            target_entity_ids_json=json.dumps([alex]),
            assignments_json=json.dumps(
                {
                    "aliases": {str(alias): [0]},
                    "edges": {str(edge): [0]},
                    "events": {str(event): [0]},
                }
            ),
            reason="migration fixture",
            schema_version=5,
            recorded_at="2026-07-19T00:00:00Z",
            retracted_at=None,
        )
        memory.add_chunk(
            f"{book_id}:chapter-1",
            1,
            "Aldric crossed the bridge.",
            [1.0, 0.0, 0.0],
            embed_model="fixture-embed",
        )
    store.close()
    catalog.close()
    archive = tmp_path / "backup.rcbackup"
    backup_book(source, book_id, archive)
    return archive


def test_dry_run_plan_is_deterministic_owner_scoped_and_write_free(tmp_path):
    archive = _archive(tmp_path)
    before = archive.read_bytes()
    owner_a = OwnerId(uuid.uuid4())
    owner_b = OwnerId(uuid.uuid4())

    first = plan_archive(archive, owner_a)
    repeated = plan_archive(archive, owner_a)
    other_owner = plan_archive(archive, owner_b)

    assert plan_checksum(first) == plan_checksum(repeated)
    assert first.book_id == repeated.book_id
    assert first.book_id != other_owner.book_id
    assert first.atom_count == 1
    assert first.row_counts["catalog.books"] == 1
    assert first.row_counts["memory.ingested_chapters"] == 1
    assert archive.read_bytes() == before


def test_equivalent_rebuilt_archives_have_one_content_plan(tmp_path):
    source = tmp_path / "source"
    store, catalog, book_id = _seed(source)
    store.close()
    catalog.close()
    first_archive = tmp_path / "first.rcbackup"
    second_archive = tmp_path / "second.rcbackup"
    backup_book(source, book_id, first_archive)
    time.sleep(1.01)
    backup_book(source, book_id, second_archive)
    owner = OwnerId(uuid.uuid4())
    first = plan_archive(first_archive, owner)
    second = plan_archive(second_archive, owner)
    assert first.source_checksum == second.source_checksum
    assert plan_checksum(first) == plan_checksum(second)
    assert first.archive_sha256 != second.archive_sha256


def test_backup_library_writes_only_verified_archives_outside_data_dir(tmp_path):
    source = tmp_path / "source"
    store, catalog, _ = _seed(source)
    store.close()
    catalog.close()
    before = {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file() and not path.name.endswith(("-wal", "-shm"))
    }
    archives = backup_library(source, tmp_path / "backups")
    after = {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file() and not path.name.endswith(("-wal", "-shm"))
    }
    assert len(archives) == 1 and archives[0].suffix == ".rcbackup"
    assert before == after


@pytest.mark.postgres
def test_import_is_dry_run_safe_idempotent_verified_and_rollbackable(tmp_path, database):
    archive = _archive(tmp_path)
    owner = OwnerId(uuid.uuid4())
    plan = plan_archive(archive, owner)
    with pytest.raises(ValueError, match="owner does not exist"):
        import_archive(database, plan, None, dry_run=True)
    with psycopg.connect(database) as conn:
        conn.execute(
            "INSERT INTO users(id,display_name) VALUES (%s,'Migration owner')", (owner.value,)
        )
    storage = EncryptedFilesystemStorage(
        root=tmp_path / "objects", encryption_key=b"k" * 32, max_object_bytes=100_000
    )

    assert import_archive(database, plan, storage, dry_run=True).status == "dry-run"
    assert not storage.exists(migration_module.SourceObjectRef(owner, plan.source_object_id))
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM books").fetchone()[0] == 0

    assert import_archive(database, plan, storage).status == "imported"
    assert import_archive(database, plan, storage).status == "already-complete"
    with psycopg.connect(database) as conn:
        assert (
            conn.execute("SELECT count(*) FROM books WHERE owner_id=%s", (owner.value,)).fetchone()[
                0
            ]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM chunks WHERE owner_id=%s", (owner.value,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM chunk_embeddings WHERE owner_id=%s", (owner.value,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM entities WHERE owner_id=%s", (owner.value,)
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM aliases WHERE owner_id=%s", (owner.value,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT count(*) FROM edges WHERE owner_id=%s", (owner.value,)).fetchone()[
                0
            ]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM events WHERE owner_id=%s", (owner.value,)
            ).fetchone()[0]
            == 1
        )
        source_ids, assignments = conn.execute(
            "SELECT source_entity_ids,assignments FROM entity_corrections WHERE owner_id=%s",
            (owner.value,),
        ).fetchone()
        assert all(isinstance(uuid.UUID(value), uuid.UUID) for value in source_ids)
        for family in ("aliases", "edges", "events"):
            assert all(isinstance(uuid.UUID(value), uuid.UUID) for value in assignments[family])
        assert (
            conn.execute(
                "SELECT count(*) FROM local_library_migrations WHERE status='complete'"
            ).fetchone()[0]
            == 1
        )

    assert rollback_archive(database, plan, storage).status == "rolled-back"
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM books").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM local_library_migrations").fetchone()[0] == 0


@pytest.mark.postgres
def test_failed_database_import_resumes_from_matching_source_object(
    tmp_path, database, monkeypatch
):
    archive = _archive(tmp_path)
    owner = OwnerId(uuid.uuid4())
    plan = plan_archive(archive, owner)
    storage = EncryptedFilesystemStorage(
        root=tmp_path / "objects", encryption_key=b"z" * 32, max_object_bytes=100_000
    )
    with psycopg.connect(database) as conn:
        conn.execute(
            "INSERT INTO users(id,display_name) VALUES (%s,'Migration owner')", (owner.value,)
        )
    real_insert = migration_module._insert_all
    monkeypatch.setattr(
        migration_module,
        "_insert_all",
        lambda *args: (_ for _ in ()).throw(RuntimeError("failpoint")),
    )
    with pytest.raises(RuntimeError, match="failpoint"):
        import_archive(database, plan, storage)
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM local_library_migrations").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM books").fetchone()[0] == 0
    monkeypatch.setattr(migration_module, "_insert_all", real_insert)
    assert import_archive(database, plan, storage).status == "imported"


@pytest.mark.postgres
def test_interrupted_object_rollback_resumes_without_duplicate_delete(
    tmp_path, database, monkeypatch
):
    archive = _archive(tmp_path)
    owner = OwnerId(uuid.uuid4())
    plan = plan_archive(archive, owner)
    storage = EncryptedFilesystemStorage(
        root=tmp_path / "objects", encryption_key=b"r" * 32, max_object_bytes=100_000
    )
    with psycopg.connect(database) as conn:
        conn.execute(
            "INSERT INTO users(id,display_name) VALUES (%s,'Migration owner')", (owner.value,)
        )
    assert import_archive(database, plan, storage).status == "imported"
    real_delete = storage.delete
    monkeypatch.setattr(
        storage, "delete", lambda ref: (_ for _ in ()).throw(RuntimeError("store down"))
    )
    with pytest.raises(RuntimeError, match="store down"):
        rollback_archive(database, plan, storage)
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM books").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT status FROM local_library_migrations WHERE owner_id=%s", (owner.value,)
            ).fetchone()[0]
            == "rolling_back"
        )
    monkeypatch.setattr(storage, "delete", real_delete)
    assert rollback_archive(database, plan, storage).status == "rolled-back"
    with psycopg.connect(database) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_events WHERE owner_id=%s AND action='book.delete'",
                (owner.value,),
            ).fetchone()[0]
            == 1
        )
