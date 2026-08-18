"""Operator CLI for LIT-24 lifecycle archives."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from app.catalog.catalog import Catalog

from .archive import backup_book, restore_book, verify_archive


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.lifecycle")
    commands = parser.add_subparsers(dest="command", required=True)

    backup = commands.add_parser("backup", help="create an online, verified per-book archive")
    backup.add_argument("--data-dir", type=Path, required=True)
    backup.add_argument("--book-id", required=True)
    backup.add_argument("--output", type=Path, required=True)

    backup_all = commands.add_parser("backup-all", help="create rotating archives for every book")
    backup_all.add_argument("--data-dir", type=Path, required=True)
    backup_all.add_argument("--output-dir", type=Path, required=True)
    backup_all.add_argument("--keep", type=int, default=7)
    backup_all.add_argument("--min-age-hours", type=float, default=0)

    verify = commands.add_parser("verify", help="verify an archive without restoring it")
    verify.add_argument("archive", type=Path)

    restore = commands.add_parser("restore", help="restore into an atomic data-directory stage")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--target", type=Path, required=True)
    restore.add_argument("--replace", action="store_true")
    restore.add_argument("--portable", action="store_true", help="rebuild databases from export.json")
    return parser


def _backup_all(args) -> dict[str, object]:
    if args.keep < 1 or args.min_age_hours < 0:
        raise ValueError("--keep must be positive and --min-age-hours must be non-negative")
    data_dir = args.data_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve(strict=False)
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog = Catalog(str(data_dir / "catalog.db"))
    try:
        book_ids = [book["book_id"] for book in catalog.list_books()]
    finally:
        catalog.close()
    created: list[str] = []
    skipped: list[str] = []
    now = time.time()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for book_id in book_ids:
        safe_id = "".join(char if char.isalnum() or char in "-_" else "_" for char in book_id)
        existing = sorted(
            (path for path in output_dir.glob("*.rcbackup") if path.name.startswith(f"{safe_id}-")),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if existing and now - existing[0].stat().st_mtime < args.min_age_hours * 3600:
            skipped.append(book_id)
            continue
        result = backup_book(data_dir, book_id, output_dir / f"{safe_id}-{stamp}.rcbackup")
        created.append(str(result.archive))
        archives = sorted(
            (path for path in output_dir.glob("*.rcbackup") if path.name.startswith(f"{safe_id}-")),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for stale in archives[args.keep:]:
            stale.unlink()
    return {"archives": created, "skipped": skipped}


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "backup":
        result = backup_book(args.data_dir, args.book_id, args.output)
        body = {"archive": str(result.archive), "book_id": result.book_id, "sha256": result.sha256}
    elif args.command == "backup-all":
        body = _backup_all(args)
    elif args.command == "verify":
        result = verify_archive(args.archive)
        body = result.__dict__
    else:
        result = restore_book(
            args.archive,
            args.target,
            replace=args.replace,
            portable=args.portable,
        )
        body = {
            "target": str(result.target),
            "book_id": result.book_id,
            "rollback": str(result.rollback) if result.rollback else None,
        }
    print(json.dumps(body, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
