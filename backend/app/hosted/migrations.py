"""Ordered, forward-only PostgreSQL migration runner for hosted mode (LIT-38)."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg


_MIGRATION_DIR = Path(__file__).with_name("schema")
_MIGRATION_RE = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql$")
_TRANSACTION_CONTROL_RE = re.compile(
    r"(?im)^\s*(?:BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE)\b"
)
_ADVISORY_LOCK_KEY = 7_338_321_038


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str


def discover_migrations(directory: Path = _MIGRATION_DIR) -> tuple[Migration, ...]:
    """Load the immutable migration stream and reject gaps or embedded transaction control."""
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_RE.fullmatch(path.name)
        if match is None:
            raise RuntimeError(f"invalid PostgreSQL migration filename: {path.name}")
        sql = path.read_text(encoding="utf-8")
        if _TRANSACTION_CONTROL_RE.search(sql):
            raise RuntimeError(f"migration {path.name} contains transaction control")
        migrations.append(
            Migration(
                version=int(match.group("version")),
                name=match.group("name"),
                path=path,
                sql=sql,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )
    expected = list(range(1, len(migrations) + 1))
    actual = [migration.version for migration in migrations]
    if actual != expected:
        raise RuntimeError(f"PostgreSQL migration versions must be contiguous: {actual!r}")
    return tuple(migrations)


def apply_migrations(dsn: str) -> None:
    """Apply every pending migration in its own transaction and verify applied checksums."""
    migrations = discover_migrations()
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_schema_migrations (
                  version     INTEGER PRIMARY KEY CHECK (version > 0),
                  name        TEXT NOT NULL,
                  checksum    TEXT NOT NULL CHECK (length(checksum) = 64),
                  applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )

        applied = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT version, name, checksum FROM app_schema_migrations ORDER BY version"
            ).fetchall()
        }
        known_versions = {migration.version for migration in migrations}
        unknown = sorted(set(applied) - known_versions)
        if unknown:
            raise RuntimeError(f"database has migrations newer than this code: {unknown!r}")
        applied_versions = sorted(applied)
        if applied_versions != list(range(1, len(applied_versions) + 1)):
            raise RuntimeError(f"database migration history is not a contiguous prefix: {applied_versions!r}")

        for migration in migrations:
            recorded = applied.get(migration.version)
            if recorded is not None:
                if recorded != (migration.name, migration.checksum):
                    raise RuntimeError(
                        f"applied migration {migration.version:04d} differs from committed file"
                    )
                continue
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
                raced = conn.execute(
                    "SELECT name, checksum FROM app_schema_migrations WHERE version = %s",
                    (migration.version,),
                ).fetchone()
                if raced is not None:
                    if raced != (migration.name, migration.checksum):
                        raise RuntimeError(
                            f"applied migration {migration.version:04d} differs from committed file"
                        )
                    continue
                conn.execute(migration.sql)
                conn.execute(
                    "INSERT INTO app_schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                    (migration.version, migration.name, migration.checksum),
                )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply hosted PostgreSQL migrations")
    parser.add_argument(
        "--dsn-env",
        default="DATABASE_URL",
        help="name of the environment variable containing the PostgreSQL DSN",
    )
    args = parser.parse_args(argv)
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        parser.error(f"environment variable {args.dsn_env!r} is not set")
    apply_migrations(dsn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
