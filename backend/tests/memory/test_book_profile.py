"""LIT-9 schema-v4 book profile storage and v3 forward migration."""
import sqlite3

import pytest

from app.catalog.catalog import Catalog
from app.memory import migrations
from app.memory.store import Store


def test_book_profile_round_trips_and_rejects_unknown_enum_values(tmp_path):
    store = Store(str(tmp_path))
    try:
        with store.book("b", meta={"title": "Book"}) as mem:
            mem.set_book_profile(
                book_type="poetry",
                confidence=0.91,
                detector_version="lit9-test",
                signals=("verse_titles", "short_sections"),
            )
            assert mem.book_profile() == {
                "book_type": "poetry",
                "confidence": 0.91,
                "detector_version": "lit9-test",
                "signals": ["verse_titles", "short_sections"],
            }
        with pytest.raises(ValueError, match="unsupported book type"):
            mem.set_book_profile(
                book_type="cookbook-but-unversioned",
                confidence=0.5,
                detector_version="test",
                signals=(),
            )
        with pytest.raises(ValueError, match="bounded detector vocabulary"):
            mem.set_book_profile(
                book_type="unknown",
                confidence=0.2,
                detector_version="lit9-test",
                signals=("The Culprit Revealed",),
            )
    finally:
        store.close()


def test_schema_v3_forward_migrates_to_legacy_novel_profile(tmp_path):
    """Existing stores were extracted under the novel prompt, so migration preserves that behaviour.

    It records an explicit legacy detector marker rather than pretending a new classifier ran.
    """
    path = tmp_path / "books" / "b" / "memory.db"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    migrations.ensure_baseline(connection)
    connection.execute(
        "INSERT INTO book_meta(book_id,title,schema_version,created_at) VALUES (?,?,?,?)",
        ("b", "Legacy", 1, "now"),
    )
    for version in (2, 3):
        step = migrations.MIGRATIONS[version]
        if callable(step):
            step(connection)
        else:
            connection.executescript(step)
        connection.execute("UPDATE book_meta SET schema_version=?", (version,))
    connection.commit()
    connection.close()

    catalog = Catalog(str(tmp_path / "catalog.db"), schema_version_default=3)
    catalog.add_book("b", title="Legacy", schema_version=3)
    store = Store(str(tmp_path), schema_version_callback=catalog.set_schema_version)
    try:
        with store.book("b") as mem:
            assert mem.book_profile() == {
                "book_type": "novel",
                "confidence": 0.0,
                "detector_version": "legacy-novel-v1",
                "signals": ["migrated_existing_store"],
            }
            assert mem._audit_all("book_meta")[0]["schema_version"] == migrations.CURRENT_VERSION
        assert catalog.get_book("b")["schema_version"] == migrations.CURRENT_VERSION
    finally:
        store.close()
        catalog.close()


def test_content_language_round_trips_and_existing_v4_store_migrates_to_undetermined(tmp_path):
    store = Store(str(tmp_path))
    try:
        with store.book("new", meta={"title": "Book"}) as mem:
            assert mem.content_language() == "und"
            mem.set_content_language("zh-hant")
            assert mem.content_language() == "zh-hant"
            with pytest.raises(ValueError, match="content language"):
                mem.set_content_language("not a language!")
    finally:
        store.close()

    path = tmp_path / "books" / "legacy" / "memory.db"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    migrations.ensure_baseline(connection)
    connection.execute(
        "INSERT INTO book_meta(book_id,title,schema_version,created_at) VALUES (?,?,?,?)",
        ("legacy", "Legacy", 1, "now"),
    )
    for version in range(2, 5):
        step = migrations.MIGRATIONS[version]
        step(connection) if callable(step) else connection.executescript(step)
        connection.execute("UPDATE book_meta SET schema_version=?", (version,))
    connection.commit()
    connection.close()
    migrated = Store(str(tmp_path))
    try:
        with migrated.book("legacy") as mem:
            assert mem.content_language() == "und"
            assert mem._audit_all("book_meta")[0]["schema_version"] == migrations.CURRENT_VERSION
    finally:
        migrated.close()
