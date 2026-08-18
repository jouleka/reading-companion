from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.catalog.catalog import Catalog
from app.ingest.manifest import _version, write_manifest
from app.lifecycle.archive import (
    LEGACY_MEMORY_TABLES,
    MEMORY_TABLES,
    DataDirLock,
    DataDirLocked,
    LifecycleError,
    _archive_manifest,
    _import_tables,
    _portable_export,
    _validate_portable,
    _validate_data_tree,
    _write_bundle,
    _write_json,
    backup_book,
    restore_book,
    read_portable_archive,
    verify_archive,
)
from app.memory import migrations
from app.memory.store import Store
from app.config import Settings
from app.main import create_app


def _seed(data_dir: Path, book_id: str = "bkfixture0001", *, title: str = "Fixture"):
    data_dir.mkdir(parents=True, exist_ok=True)
    source = b"fixture epub bytes"
    file_hash = hashlib.sha256(source).hexdigest()
    raw = "Aldric crossed the bridge."
    content_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
    atom = {
        "ordinal": 1,
        "key": f"{book_id}:chapter-1",
        "href": "chapter-1.xhtml",
        "title": "Chapter I",
        "part_label": "",
        "char_len": len(raw),
    }
    manifest = {
        "book_id": book_id,
        "mode": "file-driven",
        "flags": [],
        "atoms": [atom],
        "atom_set_version": _version([atom]),
    }
    book_dir = data_dir / "books" / book_id
    book_dir.mkdir(parents=True)
    (book_dir / "source.epub").write_bytes(source)
    write_manifest(str(data_dir), manifest)

    store = Store(str(data_dir))
    with store.book(
        book_id,
        meta={"title": title, "source": "upload", "file_hash": file_hash},
    ) as mem:
        with mem.transaction():
            mem.add_chapter(
                atom["key"],
                revealed_at=1,
                href=atom["href"],
                title=atom["title"],
                content_hash=content_hash,
            )
            mem.add_raw(atom["key"], 1, raw, content_hash=content_hash)
            mem.add_summary(atom["key"], 1, "Aldric crossed a bridge.")
            mem.mark_chapter_ingested(
                atom["key"],
                content_hash,
                cost={"model": "fixture", "input_tokens": 3, "output_tokens": 2, "usd": 0.01},
            )

    catalog = Catalog(
        str(data_dir / "catalog.db"),
        schema_version_default=migrations.CURRENT_VERSION,
    )
    catalog.add_book(
        book_id,
        title=title,
        source="upload",
        file_hash=file_hash,
        schema_version=migrations.CURRENT_VERSION,
    )
    catalog.set_position(book_id, "epubcfi(/6/2)", 1)
    catalog.finalize_ingest(
        book_id,
        1,
        cost={"model": "fixture", "input_tokens": 3, "output_tokens": 2, "usd": 0.01},
    )
    return store, catalog, book_id


def _scalar(db: Path, sql: str):
    connection = sqlite3.connect(db)
    try:
        return connection.execute(sql).fetchone()[0]
    finally:
        connection.close()


def test_online_backup_round_trip_and_portable_json_import(tmp_path):
    source = tmp_path / "source-data"
    store, catalog, book_id = _seed(source)
    catalog.reset_position(book_id, expected_epoch=0)
    catalog.set_position(book_id, "chapter-1-reread", 1, expected_epoch=1)
    with store.book(book_id) as mem:
        mem.set_book_profile(
            book_type="reference",
            confidence=0.88,
            detector_version="lit9-test",
            signals=("reference_titles",),
        )
    archive = tmp_path / "fixture.rcbackup"

    # Both WAL-mode application connections remain open: sqlite backup must still take consistent
    # database snapshots and must not depend on copying a live .db/.wal pair.
    result = backup_book(source, book_id, archive)
    assert result.archive == archive
    assert result.sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    report = verify_archive(archive)
    assert report.book_id == book_id
    assert report.durable_frontier == 1
    assert report.receipt_count == 1
    portable_archive = read_portable_archive(archive)
    assert portable_archive.book_id == book_id
    assert portable_archive.source_epub == b"fixture epub bytes"
    assert portable_archive.archive_sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()

    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        assert names == {
            "manifest.json",
            "export.json",
            "files/atoms.json",
            "files/source.epub",
            "snapshot/catalog.db",
            "snapshot/memory.db",
        }
        portable = json.loads(bundle.read("export.json"))
        assert portable["format"] == "reading-companion-portable"
        assert portable["book_id"] == book_id
        assert "openai_api_key" not in bundle.read("export.json").decode().lower()

    restored = tmp_path / "restored"
    restore_result = restore_book(archive, restored)
    assert restore_result.rollback is None
    assert (restored / "books" / book_id / "source.epub").read_bytes() == b"fixture epub bytes"
    assert _scalar(restored / "catalog.db", "SELECT bookmark FROM reading_state") == 1
    assert _scalar(restored / "catalog.db", "SELECT position_epoch FROM reading_state") == 1
    assert (
        _scalar(
            restored / "books" / book_id / "memory.db",
            "SELECT COUNT(*) FROM ingested_chapters",
        )
        == 1
    )
    assert (
        _scalar(
            restored / "books" / book_id / "memory.db",
            "SELECT book_type FROM book_meta",
        )
        == "reference"
    )

    portable_restored = tmp_path / "portable-restored"
    restore_book(archive, portable_restored, portable=True)
    assert _scalar(portable_restored / "catalog.db", "SELECT COUNT(*) FROM cost_ledger") == 1
    assert (
        _scalar(portable_restored / "catalog.db", "SELECT position_epoch FROM reading_state") == 1
    )
    assert (
        _scalar(
            portable_restored / "books" / book_id / "memory.db",
            "SELECT COUNT(*) FROM chapter_summaries",
        )
        == 1
    )
    assert (
        _scalar(
            portable_restored / "books" / book_id / "memory.db",
            "SELECT book_type FROM book_meta",
        )
        == "reference"
    )

    store.close()
    catalog.close()


def test_portable_restore_preserves_entity_correction_history(tmp_path):
    source = tmp_path / "source-data"
    store, catalog, book_id = _seed(source)
    with store.book(book_id) as mem:
        merged = mem.add_entity("Alex", "character", revealed_at=0)
        targets = mem.split_entity(
            merged,
            effective_at=1,
            replacements=[
                {"canonical_name": "Alexander", "type": "character", "state": None},
                {"canonical_name": "Alexandra", "type": "character", "state": None},
            ],
            alias_assignments={},
            edge_assignments={},
            event_assignments={},
            reason="fixture correction",
        )["target_entity_ids"]

    archive = tmp_path / "correction.rcbackup"
    backup_book(source, book_id, archive)
    restored = tmp_path / "portable-restored"
    restore_book(archive, restored, portable=True)

    restored_store = Store(str(restored))
    with restored_store.book(book_id) as mem:
        assert [row["canonical_name"] for row in mem.view(0).characters()] == ["Alex"]
        assert {row["entity_id"] for row in mem.view(1).characters()} == set(targets)
        corrections = mem._audit_all("entity_corrections")
        assert len(corrections) == 1 and corrections[0]["reason"] == "fixture correction"
    restored_store.close()
    store.close()
    catalog.close()


def test_v2_archive_forward_migrates_on_exact_and_portable_restore(tmp_path):
    source = tmp_path / "source-data"
    store, catalog, book_id = _seed(source)
    report = _validate_data_tree(source, book_id)
    legacy = _portable_export(source, book_id)
    store.close()
    catalog.close()

    del legacy["memory"]["entity_corrections"]
    position = legacy["catalog"]["reading_state"]
    epoch_index = position["columns"].index("position_epoch")
    position["columns"].pop(epoch_index)
    for row in position["rows"]:
        row.pop(epoch_index)
    for column in (
        "book_type",
        "book_type_confidence",
        "book_type_detector_version",
        "book_type_signals",
        "content_language",
    ):
        item = legacy["memory"]["book_meta"]
        index = item["columns"].index(column)
        item["columns"].pop(index)
        for row in item["rows"]:
            row.pop(index)
    for table, column in (("entities", "invalid_at"), ("event_participants", "revealed_at")):
        item = legacy["memory"][table]
        index = item["columns"].index(column)
        item["columns"].pop(index)
        for row in item["rows"]:
            row.pop(index)
    for section, table in (("memory", "book_meta"), ("catalog", "books")):
        item = legacy[section][table]
        schema_index = item["columns"].index("schema_version")
        for row in item["rows"]:
            row[schema_index] = 2

    stage = tmp_path / "legacy-stage"
    data = stage / "data"
    book_dir = data / "books" / book_id
    book_dir.mkdir(parents=True)
    shutil.copy2(source / "books" / book_id / "source.epub", book_dir / "source.epub")
    shutil.copy2(source / "books" / book_id / "atoms.json", book_dir / "atoms.json")
    source_catalog = sqlite3.connect(source / "catalog.db")
    target_catalog = sqlite3.connect(data / "catalog.db")
    source_catalog.backup(target_catalog)
    source_catalog.close()
    target_catalog.close()
    connection = sqlite3.connect(data / "catalog.db")
    connection.execute("ALTER TABLE reading_state DROP COLUMN position_epoch")
    connection.commit()
    connection.close()
    _import_tables(
        data / "catalog.db", legacy["catalog"], ("books", "reading_state", "cost_ledger")
    )

    memory = sqlite3.connect(book_dir / "memory.db")
    migrations.ensure_baseline(memory)
    memory.executescript(migrations.MIGRATIONS[2])
    memory.commit()
    memory.close()
    _import_tables(book_dir / "memory.db", legacy["memory"], LEGACY_MEMORY_TABLES)
    _write_json(stage / "export.json", legacy)
    archive = tmp_path / "legacy-v2.rcbackup"
    _write_bundle(stage, archive, _archive_manifest(stage, report))

    assert verify_archive(archive).book_id == book_id
    for portable in (False, True):
        restored = tmp_path / ("legacy-portable" if portable else "legacy-exact")
        restore_book(archive, restored, portable=portable)
        restored_store = Store(str(restored))
        with restored_store.book(book_id) as mem:
            assert mem._audit_all("book_meta")[0]["schema_version"] == migrations.CURRENT_VERSION
            assert "invalid_at" in mem._columns("entities")
            assert "revealed_at" in mem._columns("event_participants")
        restored_store.close()
        assert _scalar(restored / "catalog.db", "SELECT position_epoch FROM reading_state") == 0


def test_v3_archive_forward_migrates_on_exact_and_portable_restore(tmp_path):
    """The immediate pre-LIT-9 checkpoint remains restorable in both archive modes."""
    source = tmp_path / "source-data"
    store, catalog, book_id = _seed(source)
    report = _validate_data_tree(source, book_id)
    legacy = _portable_export(source, book_id)
    store.close()
    catalog.close()

    item = legacy["memory"]["book_meta"]
    for column in (
        "book_type",
        "book_type_confidence",
        "book_type_detector_version",
        "book_type_signals",
        "content_language",
    ):
        index = item["columns"].index(column)
        item["columns"].pop(index)
        for row in item["rows"]:
            row.pop(index)
    position = legacy["catalog"]["reading_state"]
    epoch_index = position["columns"].index("position_epoch")
    position["columns"].pop(epoch_index)
    for row in position["rows"]:
        row.pop(epoch_index)
    for section, table in (("memory", "book_meta"), ("catalog", "books")):
        versioned = legacy[section][table]
        schema_index = versioned["columns"].index("schema_version")
        for row in versioned["rows"]:
            row[schema_index] = 3

    stage = tmp_path / "legacy-v3-stage"
    data = stage / "data"
    book_dir = data / "books" / book_id
    book_dir.mkdir(parents=True)
    shutil.copy2(source / "books" / book_id / "source.epub", book_dir / "source.epub")
    shutil.copy2(source / "books" / book_id / "atoms.json", book_dir / "atoms.json")
    stage_catalog = Catalog(str(data / "catalog.db"), schema_version_default=3)
    stage_catalog.close()
    connection = sqlite3.connect(data / "catalog.db")
    connection.execute("ALTER TABLE reading_state DROP COLUMN position_epoch")
    connection.commit()
    connection.close()
    _import_tables(
        data / "catalog.db", legacy["catalog"], ("books", "reading_state", "cost_ledger")
    )
    memory = sqlite3.connect(book_dir / "memory.db")
    migrations.ensure_baseline(memory)
    memory.executescript(migrations.MIGRATIONS[2])
    migrations.MIGRATIONS[3](memory)
    memory.commit()
    memory.close()
    _import_tables(book_dir / "memory.db", legacy["memory"], MEMORY_TABLES)
    _write_json(stage / "export.json", legacy)
    archive = tmp_path / "legacy-v3.rcbackup"
    _write_bundle(stage, archive, _archive_manifest(stage, report))

    assert verify_archive(archive).book_id == book_id
    for portable in (False, True):
        restored = tmp_path / ("v3-portable" if portable else "v3-exact")
        restore_book(archive, restored, portable=portable)
        restored_store = Store(str(restored))
        with restored_store.book(book_id) as mem:
            assert mem.book_profile() == {
                "book_type": "novel",
                "confidence": 0.0,
                "detector_version": "legacy-novel-v1",
                "signals": ["migrated_existing_store"],
            }
            assert mem.content_language() == "und"
        restored_store.close()
        assert _scalar(restored / "catalog.db", "SELECT position_epoch FROM reading_state") == 0


def test_exact_and_portable_restore_preserve_vec0_search_without_provider_calls(tmp_path):
    source = tmp_path / "source-data"
    store, catalog, book_id = _seed(source)
    with store.book(book_id) as mem:
        mem.pin_models(embed_model="m1", embed_dim=3, embed_canary=[1.0, 0.0, 0.0])
        mem.add_chunk(
            f"{book_id}:chapter-1", 1, "Aldric vector fact.", [2.0, 1.0, 0.0], embed_model="m1"
        )
    archive = tmp_path / "vectors.rcbackup"
    backup_book(source, book_id, archive)
    store.close()
    catalog.close()

    for portable in (False, True):
        restored = tmp_path / ("vec-portable" if portable else "vec-exact")
        restore_book(archive, restored, portable=portable)
        restored_store = Store(str(restored), vector_backend="vec0")
        with restored_store.book(book_id) as mem:
            hits = mem.view(1).search([1.0, 0.0, 0.0], k=3)
            assert [hit[1] for hit in hits] == ["Aldric vector fact."]
        restored_store.close()


def test_portable_table_set_is_bound_to_its_declared_schema_version(tmp_path):
    source = tmp_path / "source-data"
    store, catalog, book_id = _seed(source)
    portable = _portable_export(source, book_id)
    v4 = json.loads(json.dumps(portable))
    item = v4["memory"]["book_meta"]
    language_index = item["columns"].index("content_language")
    item["columns"].pop(language_index)
    for row in item["rows"]:
        row.pop(language_index)
    for section, table in (("memory", "book_meta"), ("catalog", "books")):
        versioned = v4[section][table]
        schema_index = versioned["columns"].index("schema_version")
        for row in versioned["rows"]:
            row[schema_index] = 4
    assert _validate_portable(v4, book_id) is v4
    del portable["memory"]["entity_corrections"]
    with pytest.raises(LifecycleError, match="schema version 2"):
        _validate_portable(portable, book_id)
    store.close()
    catalog.close()


def test_backup_publish_failure_leaves_no_partial_destination(tmp_path, monkeypatch):
    source = tmp_path / "source-data"
    store, catalog, book_id = _seed(source)
    archive = tmp_path / "fixture.rcbackup"
    real_replace = os.replace

    def fail_publish(src, dst):
        if Path(dst) == archive:
            raise OSError("simulated interruption")
        return real_replace(src, dst)

    monkeypatch.setattr("app.lifecycle.archive.os.replace", fail_publish)
    with pytest.raises(OSError, match="simulated interruption"):
        backup_book(source, book_id, archive)
    assert not archive.exists()


def test_backup_refuses_unknown_inflight_spend(tmp_path):
    source, archive = tmp_path / "source", tmp_path / "book.rcbackup"
    store, catalog, book_id = _seed(source)
    catalog.reserve_cost(
        book_id,
        phase="synthesis",
        model="m",
        input_tokens=10,
        output_tokens=2,
        usd=0.01,
        max_input_tokens=100,
        max_output_tokens=100,
        max_usd=1,
    )
    store.close()
    catalog.close()
    with pytest.raises(LifecycleError, match="outstanding cost reservations"):
        backup_book(source, book_id, archive)
    assert not archive.exists()
    assert not list(tmp_path.glob(".*.partial-*"))
    store.close()
    catalog.close()


def test_archive_must_be_outside_source_and_restore_target(tmp_path):
    source = tmp_path / "source-data"
    store, catalog, book_id = _seed(source)
    with pytest.raises(LifecycleError, match="outside DATA_DIR"):
        backup_book(source, book_id, source / "unsafe.rcbackup")
    archive = tmp_path / "safe.rcbackup"
    backup_book(source, book_id, archive)
    with pytest.raises(LifecycleError, match="outside the target"):
        restore_book(archive, tmp_path)
    store.close()
    catalog.close()


def test_corrupted_archive_is_rejected_before_destination_exists(tmp_path):
    source = tmp_path / "source-data"
    store, catalog, book_id = _seed(source)
    good = tmp_path / "good.rcbackup"
    bad = tmp_path / "bad.rcbackup"
    backup_book(source, book_id, good)

    with zipfile.ZipFile(good) as reader, zipfile.ZipFile(bad, "w") as writer:
        for info in reader.infolist():
            payload = reader.read(info.filename)
            if info.filename == "files/source.epub":
                payload += b"tampered"
            writer.writestr(info, payload)

    target = tmp_path / "must-not-exist"
    with pytest.raises(LifecycleError, match="mismatch"):
        restore_book(bad, target)
    assert not target.exists()
    store.close()
    catalog.close()


def test_restore_collision_requires_replace_and_keeps_rollback(tmp_path):
    source = tmp_path / "source-data"
    source_store, source_catalog, book_id = _seed(source, title="New title")
    archive = tmp_path / "fixture.rcbackup"
    backup_book(source, book_id, archive)

    target = tmp_path / "target-data"
    target_store, target_catalog, _ = _seed(target, book_id=book_id, title="Old title")
    target_store.close()
    target_catalog.close()
    with pytest.raises(LifecycleError, match="already exists"):
        restore_book(archive, target)

    result = restore_book(archive, target, replace=True)
    assert result.rollback is not None and result.rollback.is_dir()
    assert _scalar(target / "catalog.db", "SELECT COUNT(*) FROM books WHERE title='New title'") == 1
    assert (
        _scalar(
            result.rollback / "catalog.db", "SELECT COUNT(*) FROM books WHERE title='Old title'"
        )
        == 1
    )
    source_store.close()
    source_catalog.close()


def test_interrupted_replace_rolls_original_destination_back(tmp_path, monkeypatch):
    source = tmp_path / "source-data"
    source_store, source_catalog, book_id = _seed(source, title="New title")
    archive = tmp_path / "fixture.rcbackup"
    backup_book(source, book_id, archive)
    target = tmp_path / "target-data"
    target_store, target_catalog, _ = _seed(target, book_id=book_id, title="Old title")
    target_store.close()
    target_catalog.close()
    real_replace = os.replace
    failed = False

    def fail_stage_publish(src, dst):
        nonlocal failed
        if (
            not failed
            and Path(dst) == target
            and Path(src).name.startswith(".target-data.restore-")
        ):
            failed = True
            raise OSError("simulated restore interruption")
        return real_replace(src, dst)

    monkeypatch.setattr("app.lifecycle.archive.os.replace", fail_stage_publish)
    with pytest.raises(OSError, match="simulated restore interruption"):
        restore_book(archive, target, replace=True)
    assert _scalar(target / "catalog.db", "SELECT COUNT(*) FROM books WHERE title='Old title'") == 1
    assert not list(tmp_path.glob("target-data.rollback-*"))
    assert not list(tmp_path.glob(".target-data.restore-*"))
    source_store.close()
    source_catalog.close()


def test_replace_refuses_to_discard_a_different_book(tmp_path):
    source = tmp_path / "source-data"
    source_store, source_catalog, book_id = _seed(source)
    archive = tmp_path / "fixture.rcbackup"
    backup_book(source, book_id, archive)

    target = tmp_path / "target-data"
    target_store, target_catalog, _ = _seed(target, book_id="bkother000001")
    target_store.close()
    target_catalog.close()
    with pytest.raises(LifecycleError, match="different books"):
        restore_book(archive, target, replace=True)
    assert target.exists()
    source_store.close()
    source_catalog.close()


def test_data_dir_lock_blocks_restore_until_service_releases_it(tmp_path):
    source = tmp_path / "source-data"
    store, catalog, book_id = _seed(source)
    archive = tmp_path / "fixture.rcbackup"
    backup_book(source, book_id, archive)
    target = tmp_path / "target-data"

    lock = DataDirLock(target)
    lock.acquire()
    try:
        with pytest.raises(DataDirLocked):
            restore_book(archive, target)
    finally:
        lock.release()
    restore_book(archive, target)
    assert target.is_dir()
    store.close()
    catalog.close()


def test_app_lifespan_holds_the_restore_lock(tmp_path):
    data_dir = tmp_path / "app-data"
    app = create_app(Settings(_env_file=None, allow_stub=True, data_dir=str(data_dir)))
    with TestClient(app):
        with pytest.raises(DataDirLocked):
            DataDirLock(data_dir).acquire()
    lock = DataDirLock(data_dir)
    lock.acquire()
    lock.release()


def test_corrupt_source_database_produces_no_archive(tmp_path):
    source = tmp_path / "source-data"
    store, catalog, book_id = _seed(source)
    store.close()
    catalog.close()
    memory = source / "books" / book_id / "memory.db"
    memory.write_bytes(memory.read_bytes()[:512])

    archive = tmp_path / "fixture.rcbackup"
    with pytest.raises((LifecycleError, sqlite3.DatabaseError)):
        backup_book(source, book_id, archive)
    assert not archive.exists()


def test_corrupt_snapshot_with_recomputed_member_hash_still_fails_integrity(tmp_path):
    source = tmp_path / "source-data"
    store, catalog, book_id = _seed(source)
    good = tmp_path / "good.rcbackup"
    bad = tmp_path / "bad.rcbackup"
    backup_book(source, book_id, good)

    with zipfile.ZipFile(good) as reader:
        payloads = {info.filename: reader.read(info.filename) for info in reader.infolist()}
    payloads["snapshot/memory.db"] = payloads["snapshot/memory.db"][:512]
    manifest = json.loads(payloads["manifest.json"])
    manifest["members"]["snapshot/memory.db"] = {
        "bytes": len(payloads["snapshot/memory.db"]),
        "sha256": hashlib.sha256(payloads["snapshot/memory.db"]).hexdigest(),
    }
    payloads["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(bad, "w") as writer:
        for name, payload in payloads.items():
            writer.writestr(name, payload)

    target = tmp_path / "must-not-exist"
    with pytest.raises(LifecycleError, match="SQLite"):
        restore_book(bad, target)
    assert not target.exists()
    store.close()
    catalog.close()
