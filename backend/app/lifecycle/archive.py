"""LIT-24 per-book backup, portable export, verification, and atomic restore.

The archive contains two independent representations of one shelved book:

* SQLite online-backup snapshots for exact local recovery; and
* a schema-described JSON export for portability / a future hosted importer.

The source EPUB and immutable atom manifest accompany both representations. Restore always builds and
verifies a sibling staging directory before a same-filesystem rename. Existing destinations fail
closed unless explicit replacement is requested; replacement is allowed only for an empty directory
or a one-book directory containing the same book, and the previous directory is retained as rollback.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from app.catalog.catalog import Catalog
from app.ingest.manifest import _version
from app.memory import migrations
from app.memory.store import Store


ARCHIVE_FORMAT = "reading-companion-backup"
ARCHIVE_VERSION = 1
PORTABLE_FORMAT = "reading-companion-portable"
PORTABLE_VERSION = 1
MAX_MEMBER_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024

CATALOG_TABLES = ("books", "reading_state", "cost_ledger")
MEMORY_TABLES = (
    "book_meta",
    "chapters",
    "ingested_chapters",
    "raw_chapters",
    "chapter_summaries",
    "entities",
    "entity_corrections",
    "aliases",
    "edges",
    "events",
    "event_participants",
    "themes",
    "entity_state",
    "chunks",
)
LEGACY_MEMORY_TABLES = tuple(table for table in MEMORY_TABLES if table != "entity_corrections")
MEMBERS = {
    "export.json",
    "files/atoms.json",
    "files/source.epub",
    "snapshot/catalog.db",
    "snapshot/memory.db",
}
ALL_MEMBERS = MEMBERS | {"manifest.json"}


class LifecycleError(RuntimeError):
    """A backup/restore artifact or operation failed a safety invariant."""


class DataDirLocked(LifecycleError):
    """The service (or another restore) owns the destination data directory."""


@dataclass(frozen=True)
class BackupResult:
    archive: Path
    book_id: str
    sha256: str


@dataclass(frozen=True)
class PortableArchive:
    """Verified, content-addressed input for the hosted migration tool."""

    book_id: str
    export: dict[str, object]
    atoms: dict[str, object]
    source_epub: bytes
    archive_sha256: str


@dataclass(frozen=True)
class VerificationReport:
    book_id: str
    durable_frontier: int
    receipt_count: int
    source_sha256: str
    atom_set_version: str


@dataclass(frozen=True)
class RestoreResult:
    target: Path
    book_id: str
    rollback: Path | None


class DataDirLock:
    """Cross-process exclusive lock held by the app lifespan and by restore operations.

    The lock file lives beside (not inside) the data directory so an atomic directory swap never
    moves the inode that carries the lock.
    """

    def __init__(self, data_dir: str | Path):
        target = Path(data_dir).expanduser().resolve(strict=False)
        self.path = target.parent / f".{target.name}.reading-companion.lock"
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0)
        if handle.read(1) == b"":
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows hosts
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            handle.close()
            raise DataDirLocked(
                f"data directory is active; stop the service before restore ({self.path})"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":  # pragma: no cover - exercised on Windows hosts
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _connect_ro(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except (OSError, sqlite3.Error) as exc:
        raise LifecycleError(f"cannot open SQLite database: {path}") from exc
    connection.row_factory = sqlite3.Row
    return connection


def _online_backup(source: Path, destination: Path) -> None:
    source_connection = _connect_ro(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
    except sqlite3.Error as exc:
        raise LifecycleError(f"SQLite online backup failed for {source}") from exc
    finally:
        destination_connection.close()
        source_connection.close()


def _normalize_snapshot(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("VACUUM")
    finally:
        connection.close()


def _snapshot_catalog(source: Path, destination: Path, book_id: str) -> None:
    _online_backup(source, destination)
    connection = sqlite3.connect(destination)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("SELECT 1 FROM books WHERE book_id=?", (book_id,)).fetchone() is None:
            raise LifecycleError(f"book {book_id!r} is not present in the catalog")
        has_reservations = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cost_reservations'"
        ).fetchone()
        if has_reservations:
            if connection.execute(
                "SELECT 1 FROM cost_reservations WHERE book_id=? LIMIT 1", (book_id,)
            ).fetchone():
                raise LifecycleError(
                    "book has outstanding cost reservations; reconcile unknown in-flight spend before backup"
                )
            # Reservations are transient concurrency state, not portable user data. Removing other
            # books' rows also lets the per-book snapshot delete their parent catalog rows FK-safely.
            connection.execute("DELETE FROM cost_reservations")
        connection.execute("DELETE FROM cost_ledger WHERE book_id<>?", (book_id,))
        connection.execute("DELETE FROM reading_state WHERE book_id<>?", (book_id,))
        connection.execute("DELETE FROM books WHERE book_id<>?", (book_id,))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    _normalize_snapshot(destination)


def _copy_stable(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise LifecycleError(f"required book file is missing: {source}")
    before = _sha256(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    after = _sha256(source)
    if before != after or _sha256(destination) != before:
        raise LifecycleError(f"book file changed while it was being copied: {source}")


def _integrity(path: Path) -> None:
    connection = _connect_ro(path)
    try:
        result = [row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as exc:
        raise LifecycleError(f"SQLite verification failed for {path}") from exc
    finally:
        connection.close()
    if result != ["ok"]:
        raise LifecycleError(f"SQLite integrity_check failed for {path}: {result[:3]}")
    if foreign_keys:
        raise LifecycleError(f"SQLite foreign_key_check failed for {path}")


def _rows(path: Path, sql: str, params=()) -> list[dict[str, object]]:
    connection = _connect_ro(path)
    try:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]
    except sqlite3.Error as exc:
        raise LifecycleError(f"SQLite query failed for {path}") from exc
    finally:
        connection.close()


def _load_atoms(path: Path, book_id: str) -> dict[str, object]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise TypeError("manifest is not an object")
        atoms = manifest["atoms"]
        ordinals = [atom["ordinal"] for atom in atoms]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LifecycleError("atom manifest is missing or malformed") from exc
    if manifest.get("book_id") != book_id:
        raise LifecycleError("atom manifest book_id does not match the catalog")
    if ordinals != list(range(1, len(atoms) + 1)):
        raise LifecycleError("atom manifest ordinals are not contiguous")
    if manifest.get("atom_set_version") != _version(atoms):
        raise LifecycleError("atom manifest fails atom_set_version verification")
    return manifest


def _validate_data_tree(data_dir: Path, expected_book_id: str | None = None) -> VerificationReport:
    catalog = data_dir / "catalog.db"
    _integrity(catalog)
    books = _rows(catalog, "SELECT * FROM books ORDER BY book_id")
    if len(books) != 1:
        raise LifecycleError("a per-book archive catalog must contain exactly one book")
    book = books[0]
    book_id = str(book["book_id"])
    if expected_book_id is not None and book_id != expected_book_id:
        raise LifecycleError("archive book_id does not match its manifest")
    state_rows = _rows(catalog, "SELECT * FROM reading_state WHERE book_id=?", (book_id,))
    if len(state_rows) != 1:
        raise LifecycleError("catalog must contain exactly one reading_state row")
    state = state_rows[0]
    if book["db_path"] != f"books/{book_id}/memory.db":
        raise LifecycleError("catalog db_path is not the canonical per-book path")

    book_dir = data_dir / "books" / book_id
    memory = book_dir / "memory.db"
    source = book_dir / "source.epub"
    atoms_path = book_dir / "atoms.json"
    _integrity(memory)
    meta_rows = _rows(memory, "SELECT * FROM book_meta")
    if len(meta_rows) != 1 or meta_rows[0]["book_id"] != book_id:
        raise LifecycleError("memory book_meta identity does not match the catalog")
    meta = meta_rows[0]
    if book["schema_version"] != meta["schema_version"]:
        raise LifecycleError("catalog and memory schema versions disagree")
    if meta["schema_version"] != migrations.CURRENT_VERSION:
        raise LifecycleError("only the current memory schema can be exported/imported")
    source_hash = _sha256(source)
    if book["file_hash"] != source_hash or meta["file_hash"] != source_hash:
        raise LifecycleError("source EPUB hash disagrees with catalog/book_meta")

    manifest = _load_atoms(atoms_path, book_id)
    atoms = manifest["atoms"]
    if state["bookmark"] < 0 or state["bookmark"] > len(atoms):
        raise LifecycleError("catalog bookmark is outside the atom set")
    if (
        not isinstance(state.get("position_epoch"), int)
        or isinstance(state["position_epoch"], bool)
        or state["position_epoch"] < 0
        or state["position_epoch"] >= 2**63
    ):
        raise LifecycleError("catalog position_epoch is invalid")
    live_chapters = _rows(
        memory,
        "SELECT chapter_key,revealed_at,content_hash FROM chapters "
        "WHERE retracted_at IS NULL ORDER BY revealed_at",
    )
    wanted = {atom["ordinal"]: atom["key"] for atom in atoms}
    for chapter in live_chapters:
        if wanted.get(chapter["revealed_at"]) != chapter["chapter_key"]:
            raise LifecycleError("memory chapters disagree with the atom manifest")
    receipts = _rows(
        memory,
        "SELECT i.chapter_key,i.content_hash AS receipt_hash,c.revealed_at,"
        "c.content_hash AS chapter_hash,r.content_hash AS raw_hash,r.text,i.cost_pending,"
        "i.extractor_model,i.input_tokens,i.output_tokens,i.usd "
        "FROM ingested_chapters i JOIN chapters c "
        "ON c.chapter_key=i.chapter_key AND c.book_id=i.book_id "
        "LEFT JOIN raw_chapters r ON r.chapter_key=c.chapter_key AND r.book_id=c.book_id "
        "AND r.retracted_at IS NULL WHERE c.retracted_at IS NULL ORDER BY c.revealed_at",
    )
    by_ordinal = {row["revealed_at"]: row for row in receipts}
    durable_frontier = 0
    for atom in atoms:
        row = by_ordinal.get(atom["ordinal"])
        if row is None:
            break
        actual_hash = hashlib.sha256((row["text"] or "").encode()).hexdigest()[:16]
        if (
            row["chapter_key"] != atom["key"]
            or row["receipt_hash"] != row["chapter_hash"]
            or row["chapter_hash"] != actual_hash
            or row["raw_hash"] not in (None, actual_hash)
        ):
            break
        durable_frontier = atom["ordinal"]
    if len(receipts) != len(live_chapters) or len(receipts) != durable_frontier:
        raise LifecycleError("memory has non-contiguous or incomplete durable chapter receipts")
    if state["ingest_progress"] > durable_frontier:
        raise LifecycleError("catalog ingest_progress exceeds the durable receipt frontier")
    extraction_costs = _rows(
        catalog,
        "SELECT chapter_ordinal,model,input_tokens,output_tokens,usd FROM cost_ledger "
        "WHERE book_id=? AND phase='extraction' ORDER BY chapter_ordinal",
        (book_id,),
    )
    costs_by_ordinal = {row["chapter_ordinal"]: row for row in extraction_costs}
    expected_costs = {row["revealed_at"] for row in receipts if row["cost_pending"]}
    if set(costs_by_ordinal) != expected_costs:
        raise LifecycleError("catalog extraction costs disagree with durable completion receipts")
    for receipt in receipts:
        if not receipt["cost_pending"]:
            continue
        cost = costs_by_ordinal[receipt["revealed_at"]]
        if any(
            cost[field] != receipt[receipt_field]
            for field, receipt_field in (
                ("model", "extractor_model"),
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
                ("usd", "usd"),
            )
        ):
            raise LifecycleError("catalog extraction cost payload disagrees with its receipt")

    return VerificationReport(
        book_id=book_id,
        durable_frontier=durable_frontier,
        receipt_count=len(receipts),
        source_sha256=source_hash,
        atom_set_version=str(manifest["atom_set_version"]),
    )


def _json_value(value):
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    return value


def _from_json_value(value):
    if isinstance(value, dict) and set(value) == {"$bytes"}:
        try:
            return base64.b64decode(value["$bytes"], validate=True)
        except (ValueError, TypeError) as exc:
            raise LifecycleError("portable export contains invalid base64") from exc
    return value


def _export_table(db: Path, table: str) -> dict[str, object]:
    connection = _connect_ro(db)
    try:
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
        if not columns:
            raise LifecycleError(f"portable export table is missing: {table}")
        rows = [
            [_json_value(value) for value in row]
            for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
        ]
    finally:
        connection.close()
    return {"columns": columns, "rows": rows}


def _portable_export(
    data_dir: Path,
    book_id: str,
    memory_tables: tuple[str, ...] = MEMORY_TABLES,
) -> dict[str, object]:
    memory = data_dir / "books" / book_id / "memory.db"
    return {
        "format": PORTABLE_FORMAT,
        "version": PORTABLE_VERSION,
        "book_id": book_id,
        "catalog": {
            table: _export_table(data_dir / "catalog.db", table) for table in CATALOG_TABLES
        },
        "memory": {table: _export_table(memory, table) for table in memory_tables},
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
        encoding="utf-8",
    )


def _archive_manifest(stage: Path, report: VerificationReport) -> dict[str, object]:
    files = {
        "export.json": stage / "export.json",
        "files/atoms.json": stage / "data" / "books" / report.book_id / "atoms.json",
        "files/source.epub": stage / "data" / "books" / report.book_id / "source.epub",
        "snapshot/catalog.db": stage / "data" / "catalog.db",
        "snapshot/memory.db": stage / "data" / "books" / report.book_id / "memory.db",
    }
    return {
        "format": ARCHIVE_FORMAT,
        "version": ARCHIVE_VERSION,
        "created_at": _utc_now(),
        "book_id": report.book_id,
        "durable_frontier": report.durable_frontier,
        "receipt_count": report.receipt_count,
        "source_sha256": report.source_sha256,
        "atom_set_version": report.atom_set_version,
        "members": {
            name: {"bytes": path.stat().st_size, "sha256": _sha256(path)}
            for name, path in files.items()
        },
    }


def _write_bundle(stage: Path, output: Path, manifest: dict[str, object]) -> None:
    paths = {
        "export.json": stage / "export.json",
        "files/atoms.json": stage / "data" / "books" / manifest["book_id"] / "atoms.json",
        "files/source.epub": stage / "data" / "books" / manifest["book_id"] / "source.epub",
        "snapshot/catalog.db": stage / "data" / "catalog.db",
        "snapshot/memory.db": stage / "data" / "books" / manifest["book_id"] / "memory.db",
    }
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        bundle.writestr(
            "manifest.json",
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )
        for name, path in paths.items():
            bundle.write(path, name)


def backup_book(data_dir: str | Path, book_id: str, output: str | Path) -> BackupResult:
    """Create and verify one atomic ``.rcbackup`` archive from live WAL-mode databases."""
    data_dir = Path(data_dir).expanduser().resolve()
    output = Path(output).expanduser().resolve(strict=False)
    if output.is_relative_to(data_dir):
        raise LifecycleError("backup destination must be outside DATA_DIR")
    if output.exists():
        raise LifecycleError(f"backup destination already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.stage-", dir=output.parent))
    partial = output.parent / f".{output.name}.partial-{uuid.uuid4().hex}"
    try:
        stage_data = stage / "data"
        book_dir = stage_data / "books" / book_id
        _snapshot_catalog(data_dir / "catalog.db", stage_data / "catalog.db", book_id)
        _online_backup(data_dir / "books" / book_id / "memory.db", book_dir / "memory.db")
        _normalize_snapshot(book_dir / "memory.db")
        _copy_stable(data_dir / "books" / book_id / "atoms.json", book_dir / "atoms.json")
        _copy_stable(data_dir / "books" / book_id / "source.epub", book_dir / "source.epub")
        report = _validate_data_tree(stage_data, book_id)
        _write_json(stage / "export.json", _portable_export(stage_data, book_id))
        manifest = _archive_manifest(stage, report)
        _write_bundle(stage, partial, manifest)
        os.chmod(partial, 0o600)
        verify_archive(partial)
        os.replace(partial, output)
        return BackupResult(output, book_id, _sha256(output))
    finally:
        if partial.exists():
            partial.unlink()
        shutil.rmtree(stage, ignore_errors=True)


def _read_bundle(archive: Path) -> tuple[dict[str, object], dict[str, bytes]]:
    try:
        with zipfile.ZipFile(archive) as bundle:
            infos = bundle.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or set(names) != ALL_MEMBERS:
                raise LifecycleError("archive members are missing, duplicated, or unexpected")
            total = 0
            for info in infos:
                if info.is_dir() or info.file_size > MAX_MEMBER_BYTES:
                    raise LifecycleError("archive member size is not allowed")
                total += info.file_size
            if total > MAX_ARCHIVE_BYTES:
                raise LifecycleError("archive expanded size exceeds the safety limit")
            payloads = {name: bundle.read(name) for name in names}
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, LifecycleError):
            raise
        raise LifecycleError("backup archive is unreadable") from exc
    try:
        manifest = json.loads(payloads["manifest.json"])
        if not isinstance(manifest, dict):
            raise TypeError("manifest is not an object")
    except (ValueError, TypeError) as exc:
        raise LifecycleError("archive manifest is malformed") from exc
    if manifest.get("format") != ARCHIVE_FORMAT or manifest.get("version") != ARCHIVE_VERSION:
        raise LifecycleError("archive format/version is unsupported")
    members = manifest.get("members")
    if not isinstance(members, dict) or set(members) != MEMBERS:
        raise LifecycleError("archive manifest member list is invalid")
    for name in MEMBERS:
        expected = members[name]
        if not isinstance(expected, dict):
            raise LifecycleError(f"archive manifest member metadata is invalid: {name}")
        if expected.get("bytes") != len(payloads[name]):
            raise LifecycleError(f"archive member size mismatch: {name}")
        if expected.get("sha256") != _sha256_bytes(payloads[name]):
            raise LifecycleError(f"archive member checksum mismatch: {name}")
    return manifest, payloads


def read_portable_archive(archive: str | Path) -> PortableArchive:
    """Verify and decode migration-safe members without extracting or mutating local data."""
    path = Path(archive).expanduser().resolve()
    report = verify_archive(path)
    manifest, payloads = _read_bundle(path)
    try:
        export = json.loads(payloads["export.json"])
        atoms = json.loads(payloads["files/atoms.json"])
    except (UnicodeError, ValueError, TypeError) as exc:
        raise LifecycleError("portable archive JSON is malformed") from exc
    if not isinstance(export, dict) or not isinstance(atoms, dict):
        raise LifecycleError("portable archive JSON roots must be objects")
    if export.get("book_id") != report.book_id or manifest.get("book_id") != report.book_id:
        raise LifecycleError("portable archive identities disagree")
    return PortableArchive(
        book_id=report.book_id,
        export=export,
        atoms=atoms,
        source_epub=payloads["files/source.epub"],
        archive_sha256=_sha256(path),
    )


def _materialize_payloads(root: Path, book_id: str, payloads: dict[str, bytes]) -> None:
    paths = {
        "files/atoms.json": root / "books" / book_id / "atoms.json",
        "files/source.epub": root / "books" / book_id / "source.epub",
        "snapshot/catalog.db": root / "catalog.db",
        "snapshot/memory.db": root / "books" / book_id / "memory.db",
    }
    for name, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payloads[name])


def _portable_memory_tables(memory: object) -> tuple[str, ...]:
    if not isinstance(memory, dict):
        raise LifecycleError("portable memory table set is invalid")
    tables = set(memory)
    if tables == set(MEMORY_TABLES):
        return MEMORY_TABLES
    if tables == set(LEGACY_MEMORY_TABLES):
        return LEGACY_MEMORY_TABLES
    raise LifecycleError("portable memory table set is unsupported")


def _portable_scalar(section: dict[str, object], table: str, column: str):
    item = section.get(table)
    if not isinstance(item, dict) or set(item) != {"columns", "rows"}:
        raise LifecycleError(f"portable table shape is invalid: {table}")
    columns, rows = item["columns"], item["rows"]
    if (
        not isinstance(columns, list)
        or column not in columns
        or not isinstance(rows, list)
        or len(rows) != 1
    ):
        raise LifecycleError(f"portable table does not contain one {column}: {table}")
    row = rows[0]
    if not isinstance(row, list) or len(row) != len(columns):
        raise LifecycleError(f"portable row shape is invalid: {table}")
    return row[columns.index(column)]


def _upgrade_data_tree(root: Path, book_id: str) -> None:
    """Migrate a staged older snapshot before validation/publication; never mutates the archive."""
    memory = root / "books" / book_id / "memory.db"
    rows = _rows(memory, "SELECT schema_version FROM book_meta WHERE book_id=?", (book_id,))
    if len(rows) != 1:
        raise LifecycleError("staged memory has no unique book schema version")
    stored = rows[0]["schema_version"]
    if not isinstance(stored, int) or stored > migrations.CURRENT_VERSION:
        raise LifecycleError("staged memory schema is newer than this application")
    # Always open through the production Store. vec0 is a derived local index intentionally omitted
    # from portable JSON, so portable restore rebuilds it and exact restore verifies it here.
    store = Store(str(root))
    try:
        with store.book(book_id):
            pass
    finally:
        store.close()
    # Catalog shape changes are intentionally independent of the memory schema version. Opening the
    # staged catalog adds compatible columns (such as LIT-17's epoch) before publication.
    catalog = Catalog(str(root / "catalog.db"), schema_version_default=migrations.CURRENT_VERSION)
    try:
        catalog.set_schema_version(book_id, migrations.CURRENT_VERSION)
    finally:
        catalog.close()


def verify_archive(archive: str | Path) -> VerificationReport:
    """Verify member hashes, both SQLite databases, cross-file identity/frontier, and JSON parity."""
    archive = Path(archive).expanduser().resolve()
    manifest, payloads = _read_bundle(archive)
    book_id = manifest.get("book_id")
    if not isinstance(book_id, str) or not book_id:
        raise LifecycleError("archive book_id is missing")
    with tempfile.TemporaryDirectory(prefix="reading-companion-verify-") as tmp:
        data = Path(tmp) / "data"
        _materialize_payloads(data, book_id, payloads)
        try:
            portable = json.loads(payloads["export.json"])
        except ValueError as exc:
            raise LifecycleError("portable JSON export is malformed") from exc
        portable = _validate_portable(portable, book_id)
        memory_tables = _portable_memory_tables(portable["memory"])
        _integrity(data / "catalog.db")
        _integrity(data / "books" / book_id / "memory.db")
        if portable != _portable_export(data, book_id, memory_tables):
            raise LifecycleError("portable JSON export disagrees with the SQLite snapshots")
        _upgrade_data_tree(data, book_id)
        report = _validate_data_tree(data, book_id)
    for key in ("durable_frontier", "receipt_count", "source_sha256", "atom_set_version"):
        if manifest.get(key) != getattr(report, key):
            raise LifecycleError(f"archive manifest {key} disagrees with its contents")
    return report


def _validate_portable(portable: object, book_id: str) -> dict[str, object]:
    if not isinstance(portable, dict):
        raise LifecycleError("portable export must be an object")
    catalog = portable.get("catalog")
    memory = portable.get("memory")
    if (
        portable.get("format") != PORTABLE_FORMAT
        or portable.get("version") != PORTABLE_VERSION
        or portable.get("book_id") != book_id
        or not isinstance(catalog, dict)
        or not isinstance(memory, dict)
        or set(catalog) != set(CATALOG_TABLES)
    ):
        raise LifecycleError("portable export format, book, or table set is invalid")
    memory_tables = _portable_memory_tables(memory)
    memory_version = _portable_scalar(memory, "book_meta", "schema_version")
    catalog_version = _portable_scalar(catalog, "books", "schema_version")
    if memory_version != catalog_version:
        raise LifecycleError("portable catalog and memory schema versions disagree")
    if memory_tables == LEGACY_MEMORY_TABLES and memory_version != 2:
        raise LifecycleError("legacy portable table set must declare schema version 2")
    if memory_tables == MEMORY_TABLES and memory_version not in range(
        3, migrations.CURRENT_VERSION + 1
    ):
        raise LifecycleError("full portable table set has an unsupported schema version")
    return portable


def _import_tables(
    db: Path,
    exported: dict[str, object],
    tables: tuple[str, ...],
    allowed_missing: dict[str, set[str]] | None = None,
) -> None:
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        for table in reversed(tables):
            connection.execute(f'DELETE FROM "{table}"')
        for table in tables:
            item = exported[table]
            if not isinstance(item, dict) or set(item) != {"columns", "rows"}:
                raise LifecycleError(f"portable table shape is invalid: {table}")
            columns = item["columns"]
            actual = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
            allowed = (allowed_missing or {}).get(table, set())
            if (
                not isinstance(columns, list)
                or not all(isinstance(column, str) for column in columns)
                or len(columns) != len(set(columns))
                or columns != [column for column in actual if column in columns]
                or not set(actual).difference(columns).issubset(allowed)
            ):
                raise LifecycleError(f"portable table columns do not match current schema: {table}")
            placeholders = ",".join("?" for _ in columns)
            names = ",".join(f'"{column}"' for column in columns)
            sql = f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})'
            for row in item["rows"]:
                if not isinstance(row, list) or len(row) != len(columns):
                    raise LifecycleError(f"portable row shape is invalid: {table}")
                connection.execute(sql, [_from_json_value(value) for value in row])
        connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise LifecycleError("portable import violates foreign keys")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("VACUUM")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _portable_restore(root: Path, book_id: str, portable: dict[str, object], payloads) -> None:
    source = root / "books" / book_id / "source.epub"
    atoms = root / "books" / book_id / "atoms.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payloads["files/source.epub"])
    atoms.write_bytes(payloads["files/atoms.json"])

    catalog = Catalog(str(root / "catalog.db"), schema_version_default=migrations.CURRENT_VERSION)
    catalog.close()
    memory_tables = _portable_memory_tables(portable["memory"])
    memory = root / "books" / book_id / "memory.db"
    stored_version = _portable_scalar(portable["memory"], "book_meta", "schema_version")
    if stored_version == migrations.CURRENT_VERSION:
        store = Store(str(root))
        with store.book(book_id, meta={"title": book_id}):
            pass
        store.close()
    else:
        connection = sqlite3.connect(memory)
        try:
            migrations.ensure_baseline(connection)
            for version in range(2, stored_version + 1):
                step = migrations.MIGRATIONS[version]
                if callable(step):
                    step(connection)
                else:
                    connection.executescript(step)
            connection.commit()
        finally:
            connection.close()
    _import_tables(
        root / "catalog.db",
        portable["catalog"],
        CATALOG_TABLES,
        allowed_missing={"reading_state": {"position_epoch"}},
    )
    _import_tables(memory, portable["memory"], memory_tables)
    _upgrade_data_tree(root, book_id)


def _existing_book_ids(target: Path) -> set[str]:
    if not target.exists():
        return set()
    if not target.is_dir():
        raise LifecycleError("restore target exists and is not a directory")
    catalog = target / "catalog.db"
    allowed_top_level = {"catalog.db", "catalog.db-wal", "catalog.db-shm", "books"}
    unexpected = {item.name for item in target.iterdir()} - allowed_top_level
    if unexpected:
        raise LifecycleError("existing target has unknown data outside the catalog layout")
    if not catalog.exists():
        if any(target.iterdir()):
            raise LifecycleError("existing target has unknown non-catalog data")
        return set()
    _integrity(catalog)
    catalog_ids = {str(row["book_id"]) for row in _rows(catalog, "SELECT book_id FROM books")}
    books_dir = target / "books"
    disk_ids = (
        {item.name for item in books_dir.iterdir() if item.is_dir()}
        if books_dir.is_dir()
        else set()
    )
    return catalog_ids | disk_ids


def restore_book(
    archive: str | Path,
    target: str | Path,
    *,
    replace: bool = False,
    portable: bool = False,
) -> RestoreResult:
    """Restore into a verified sibling stage, then atomically publish the whole data directory.

    ``portable=True`` reconstructs both databases from ``export.json`` instead of using the snapshot
    files, exercising the migration/portability representation. Existing multi-book libraries are
    never replaced by a per-book archive.
    """
    archive = Path(archive).expanduser().resolve()
    target = Path(target).expanduser().resolve(strict=False)
    if archive.is_relative_to(target):
        raise LifecycleError("restore archive must be outside the target data directory")
    report = verify_archive(archive)
    manifest, payloads = _read_bundle(archive)
    portable_data = _validate_portable(json.loads(payloads["export.json"]), report.book_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = DataDirLock(target)
    lock.acquire()
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent))
    rollback: Path | None = None
    published = False
    try:
        if portable:
            _portable_restore(stage, report.book_id, portable_data, payloads)
        else:
            _materialize_payloads(stage, report.book_id, payloads)
            _upgrade_data_tree(stage, report.book_id)
        _validate_data_tree(stage, report.book_id)

        existing = _existing_book_ids(target)
        if target.exists() and not replace:
            raise LifecycleError(f"restore target already exists: {target}")
        if existing - {report.book_id}:
            raise LifecycleError(
                "replace would discard different books; restore to a new directory"
            )
        if target.exists():
            rollback = target.parent / (
                f"{target.name}.rollback-{datetime.now().strftime('%Y%m%dT%H%M%S')}-"
                f"{uuid.uuid4().hex[:8]}"
            )
            os.replace(target, rollback)
        try:
            os.replace(stage, target)
            published = True
        except Exception:
            if rollback is not None and rollback.exists() and not target.exists():
                os.replace(rollback, target)
                rollback = None
            raise
        return RestoreResult(target, report.book_id, rollback)
    finally:
        if not published and stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        lock.release()
