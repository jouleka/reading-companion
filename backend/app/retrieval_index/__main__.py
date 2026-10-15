"""Explicit local index backfill. Stop the local server before taking this directory lock."""
import argparse
import json
from pathlib import Path

from app.catalog.catalog import Catalog
from app.config import Settings
from app.lifecycle.archive import DataDirLock
from app.llm.client import LLMClient
from app.memory import migrations
from app.memory.store import Store
from app.retrieval_index import backfill_source_index


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m app.retrieval_index")
    parser.add_argument("command", choices=["backfill"])
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--book-id", required=True)
    parser.add_argument("--through", type=int)
    parser.add_argument("--max-additional-usd", type=float, required=True)
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).expanduser().resolve()
    if not (data_dir / "catalog.db").is_file():
        raise SystemExit("catalog database does not exist")
    lock = DataDirLock(data_dir)
    lock.acquire()
    catalog = store = client = None
    report = {"book_id": args.book_id, "status": "failed",
              "max_additional_usd": args.max_additional_usd}
    cost_start = initial_usd = initial_exposure = None
    try:
        settings = Settings(data_dir=str(data_dir))
        if settings.deployment_mode != "local":
            raise ValueError("source backfill supports local SQLite books only")
        catalog = Catalog(str(data_dir / "catalog.db"),
                          schema_version_default=migrations.CURRENT_VERSION)
        if catalog.get_book(args.book_id) is None:
            raise ValueError("unknown book")
        initial_costs = catalog.get_costs(args.book_id)
        cost_start = len(initial_costs)
        initial_usd = sum(row["usd"] for row in initial_costs)
        initial_exposure = initial_usd + sum(row["reserved_usd"] for row in
                                             catalog.get_cost_reservations(args.book_id))
        store = Store(str(data_dir), vector_backend=settings.vector_backend,
                      schema_version_callback=catalog.set_schema_version)
        # An explicit backfill makes one provider attempt per reserved batch. No SDK retry or
        # completion/extraction call is hidden behind this operator command.
        client = LLMClient(env=settings.llm_env(), allow_stub=settings.allow_stub, max_retries=0)
        report.update(backfill_source_index(
            store, catalog, client, settings, args.book_id,
            max_additional_usd=args.max_additional_usd, through=args.through,
        ))
        report["status"] = "completed"
    except Exception as exc:
        # Never serialize provider exception messages, request bodies, settings, or credentials.
        report["error_type"] = type(exc).__name__
        if getattr(exc, "public_detail", None):
            report["detail"] = exc.public_detail()
    finally:
        if catalog is not None and catalog.get_book(args.book_id) is not None:
            costs = catalog.get_costs(args.book_id)
            pending = catalog.get_cost_reservations(args.book_id)
            report["costs"] = costs[cost_start:] if cost_start is not None else []
            report["pending_reservations"] = pending
            report["historical_usd"] = initial_usd
            report["additional_usd"] = sum(row["usd"] for row in report["costs"])
            if initial_exposure is not None:
                report["additional_exposure_usd"] = (sum(row["usd"] for row in costs)
                    + sum(row["reserved_usd"] for row in pending) - initial_exposure)
        if store is not None:
            store.close()
        if catalog is not None:
            catalog.close()
        if client is not None:
            for name in ("_oai", "_embed_oai", "_http"):
                handle = getattr(client, name, None)
                if handle is not None:
                    handle.close()
        lock.release()
        payload = json.dumps(report, indent=2, sort_keys=True)
        if args.report:
            Path(args.report).write_text(payload, encoding="utf-8")
        print(payload)
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
