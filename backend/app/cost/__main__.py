"""Operator commands for inspecting or conservatively reconciling crash-left cost reservations."""
import argparse
import json
from pathlib import Path

from app.catalog.catalog import Catalog
from app.lifecycle.archive import DataDirLock


def _parser():
    parser = argparse.ArgumentParser(prog="python -m app.cost")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "reconcile"):
        item = sub.add_parser(command)
        item.add_argument("--data-dir", required=True)
        item.add_argument("--book-id", required=True)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    data_dir = Path(args.data_dir).expanduser().resolve()
    catalog_path = data_dir / "catalog.db"
    if not catalog_path.is_file():
        raise SystemExit(f"catalog database does not exist: {catalog_path}")
    lock = DataDirLock(data_dir)
    lock.acquire()
    catalog = None
    try:
        catalog = Catalog(str(catalog_path))
        if catalog.get_book(args.book_id) is None:
            raise SystemExit(f"unknown book {args.book_id!r}")
        reservations = catalog.get_cost_reservations(args.book_id)
        if args.command == "reconcile":
            for reservation in reservations:
                catalog.settle_cost(
                    args.book_id,
                    reservation["reservation_id"],
                    phase=f"{reservation['phase']}-reconciled-reserved",
                )
        print(json.dumps({
            "book_id": args.book_id,
            "outstanding": len(reservations) if args.command == "status" else 0,
            "reservations": reservations if args.command == "status" else [],
            "reconciled": len(reservations) if args.command == "reconcile" else 0,
        }, sort_keys=True))
    finally:
        if catalog is not None:
            catalog.close()
        lock.release()


if __name__ == "__main__":
    main()
