"""Opt-in live background preparation, full-answer cache and restart audit on synthetic text."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.reader_prepare import PreparationScope
from live_quality_audit import fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--extraction", action="store_true", help="Include Jev's negative-only extraction screen")
    args = parser.parse_args()
    if not args.run:
        parser.error("--run is required for this capped paid audit")
    report = {"scope": "Synthetic three-chapter book; only first two chapters completed.",
              "calls": [], "answers": [], "jev_calls": [], "extraction_screen_enabled": args.extraction,
              "book_cap_usd": 1, "preparation_cap_usd": .25}
    def save():
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="reader-preparation-") as scratch:
        settings = Settings(_env_file=str(args.env_file), data_dir=str(Path(scratch)/"data"),
            frontend_dist_dir=None, trusted_hosts="testserver", deployment_mode="local",
            openai_base_url="https://api.openai.com/v1", jev_enabled=True,
            jev_windows_credential=True, reader_fast_mode=False, reader_answer_cache=True,
            jev_extraction_enabled=args.extraction,
            reader_prepare_enabled=True, reader_reuse_enabled=True, recap_prewarm=True,
            recap_disk_cache=True, reader_prepare_delay_seconds=.05,
            reader_prepare_max_usd_per_book=.25, cost_max_usd_per_book=1)
        app = create_app(settings)
        with TestClient(app) as api:
            client = app.state.reader_client
            jev_evaluate = client.jev.evaluate
            def evaluate(state, questions, **kwargs):
                started = time.perf_counter()
                result = jev_evaluate(state, questions, **kwargs)
                report["jev_calls"].append({"seconds": time.perf_counter()-started,
                    "kind": "extraction" if "source_passages" in state and "fields" in state else "reader",
                    "unread_fact_in_input": "Velora" in json.dumps(state) or "silver compass" in json.dumps(state),
                    "questions": len(questions)})
                return result
            client.jev.evaluate = evaluate
            original = client.complete
            def observed(system, user, tier="cheap", schema=None, max_output_tokens=None):
                started = time.perf_counter()
                try:
                    value, usage = original(system, user, tier=tier, schema=schema,
                                            max_output_tokens=max_output_tokens)
                    report["calls"].append({"schema": getattr(schema, "__name__", None),
                        "seconds": time.perf_counter()-started,
                        "unread_fact_in_input": "Velora" in user or "silver compass" in user,
                        "usage": usage})
                    save()
                    return value, usage
                finally:
                    pass
            client.complete = observed
            imported = api.post("/api/books", files={"file": (
                "synthetic.epub", fixture(), "application/epub+zip")})
            assert imported.status_code == 201, imported.text
            bid = imported.json()["book_id"]
            atoms = json.loads((Path(settings.data_dir)/"books"/bid/"atoms.json").read_text())["atoms"]
            offset = sum(a["char_len"] for a in atoms[:2])
            started = time.perf_counter()
            assert api.put(f"/api/books/{bid}/position", json={
                "cfi": "chapter-three-start", "offset": offset, "position_epoch": 0}).status_code == 200
            print("Synthetic ingestion started", flush=True)
            deadline = time.monotonic()+240
            while app.state.catalog.get_state(bid)["ingest_progress"] < 2:
                if time.monotonic() > deadline:
                    raise RuntimeError("Synthetic ingestion did not finish within its bounded wait")
                time.sleep(.2)
            report["ingestion_seconds"] = time.perf_counter()-started
            print("Ingestion cleared; preparing complete answers", flush=True)
            # Same-frontier page-turn reports must finish while preparation is in progress.
            times = []
            for index in range(5):
                started = time.perf_counter()
                response = api.put(f"/api/books/{bid}/position", json={
                    "cfi": f"page-{index}", "offset": offset+index, "position_epoch": 0})
                assert response.status_code == 200
                times.append(time.perf_counter()-started)
            report["position_seconds"] = times
            api.portal.call(app.state.recap_warmer.queue.join)
            scope = PreparationScope.capture(app, bid, app.state.catalog.get_book(bid)["incarnation"])
            candidates = client.answers.candidates(scope.answer_key, "completed chapter", limit=5)
            report["prepared_count"] = len(candidates)
            for candidate in candidates:
                question = candidate["reviewed_question"]
                started = time.perf_counter()
                value = api.post(f"/api/books/{bid}/ask", json={"question": question}).json()
                report["answers"].append({"question": question, "seconds": time.perf_counter()-started,
                    "cached": value.get("cached", False), "claims": value.get("claims", []),
                    "citations": value.get("citations", []), "cost": value.get("cost")})
                assert value.get("cached") and value["claims"] == candidate["payload"]["claims"]
            started = time.perf_counter()
            recap = api.get(f"/api/books/{bid}/catch-me-up").json()
            report["recap_seconds"] = time.perf_counter()-started
            report["recap"] = recap
            ledger = app.state.catalog.get_costs(bid)
            report["total_usd"] = sum(r["usd"] for r in ledger)
            report["extraction_screen_calls"] = sum(r["phase"].startswith("jev-extraction-screen") for r in ledger)
            report["full_extraction_review_calls"] = sum(r["phase"] == "extraction-review" for r in ledger)
            if args.extraction:
                assert report["extraction_screen_calls"] >= 1
                assert report["full_extraction_review_calls"] >= 2
            report["preparation_usd"] = sum(r["usd"] for r in ledger
                if r["phase"].startswith("reader-prepare:"))
            report["preparation_calls"] = sum(r["phase"].startswith("reader-prepare:") for r in ledger)
            assert not any(r["unread_fact_in_input"] for r in report["calls"])
            assert not any(r["unread_fact_in_input"] for r in report["jev_calls"])
            save()
        # No provider call should be needed for a reviewed answer after restart.
        restarted = create_app(settings)
        with TestClient(restarted) as api:
            before = restarted.state.catalog.total_cost(bid)
            if candidates:
                question = candidates[0]["reviewed_question"]
                started = time.perf_counter()
                value = api.post(f"/api/books/{bid}/ask", json={"question": question}).json()
                report["restart_answer_seconds"] = time.perf_counter()-started
                report["restart_answer_cached"] = value.get("cached", False)
                assert value.get("cached")
            api.portal.call(restarted.state.recap_warmer.queue.join)
            report["restart_new_usd"] = restarted.state.catalog.total_cost(bid)-before
            assert report["restart_new_usd"] == 0
            api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 0})
            if candidates:
                value = api.post(f"/api/books/{bid}/ask", json={
                    "question": candidates[0]["reviewed_question"], "bookmark": 999}).json()
                assert value["as_of_chapter"] == 0 and not value["claims"]
            report["reset_safe"] = True
        report["summary"] = {"prepared_count": report["prepared_count"],
            "median_complete_answer_seconds": statistics.median(a["seconds"] for a in report["answers"])
                if report["answers"] else None,
            "max_position_seconds": max(times), "preparation_usd": report["preparation_usd"],
            "restart_new_usd": report["restart_new_usd"], "reset_safe": True}
        save()
        print(json.dumps(report["summary"]), flush=True)
    return int(not report["prepared_count"])


if __name__ == "__main__":
    raise SystemExit(main())
