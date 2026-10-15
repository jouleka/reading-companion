"""Opt-in paired live API benchmark. Only the authored synthetic EPUB is sent to providers."""
from __future__ import annotations

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
from app.llm.jev import JevClient, windows_credential
from app.main import create_app
from live_quality_audit import CASES, InlineExecutor, fixture, install_call_recording, cost_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.run:
        parser.error("--run is required for the capped paid comparison")
    report = {"scope": "Paired live synthetic API sample, one shared ingested book, same model roles. "
                       "Small sample; terms are triage, inspect complete claims and citations.",
              "per_book_budget_usd": 1.0, "cases": [], "jev_calls": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="jev-integration-") as scratch:
        settings = Settings(_env_file=str(args.env_file), data_dir=str(Path(scratch)/"data"),
                            frontend_dist_dir=None, trusted_hosts="testserver", deployment_mode="local",
                            jev_enabled=False, recap_prewarm=False, cost_max_usd_per_book=1.0,
                            openai_base_url="https://api.openai.com/v1",
                            embed_provider="openai-compatible", embed_base_url="https://api.openai.com/v1",
                            embed_api_key=None, embed_model="text-embedding-3-small")
        report["models"] = {"cheap": settings.llm_cheap_model, "large": settings.llm_large_model,
                             "jev": settings.jev_model}
        app = create_app(settings, ingest_executor=InlineExecutor())
        with TestClient(app, raise_server_exceptions=False) as api:
            provider = app.state.client
            provider.max_retries = 0
            provider._oai = provider._oai.with_options(max_retries=0)
            provider._embed_oai = provider._embed_oai.with_options(max_retries=0)
            install_call_recording(provider, report, settings.cost_max_output_tokens_per_call)
            jev = JevClient(api_key=windows_credential(), model=settings.jev_model)
            original = jev.evaluate
            def evaluate(state, questions):
                started = time.perf_counter()
                try:
                    answers, usage = original(state, questions)
                    report["jev_calls"].append({"seconds": round(time.perf_counter()-started, 4),
                                                "questions": len(questions), "usage": usage,
                                                "answers": answers})
                    return answers, usage
                finally:
                    save()
            jev.evaluate = evaluate
            imported = api.post("/api/books", files={"file": (
                "synthetic.epub", fixture(), "application/epub+zip")})
            if imported.status_code != 201:
                raise RuntimeError("Synthetic import failed")
            bid = imported.json()["book_id"]
            try:
                atoms = json.loads((Path(settings.data_dir)/"books"/bid/"atoms.json").read_text())["atoms"]
                started = time.perf_counter()
                api.put(f"/api/books/{bid}/position", json={"cfi": "synthetic", "position_epoch": 0,
                                                           "offset": sum(a["char_len"] for a in atoms[:2])})
                ingest = api.get(f"/api/books/{bid}/ingest").json()
                report["ingestion"] = {"seconds": time.perf_counter()-started, "result": ingest}
                save()
                if ingest.get("ingest_progress") != 2:
                    report["status"] = "ingestion-not-cleared"
                    return 1
                # Alternate order by case to reduce time-of-run/order bias; no request retries.
                for i, case in enumerate(CASES):
                    for mode in (["gpt", "jev"] if i % 2 == 0 else ["jev", "gpt"]):
                        provider.jev = jev if mode == "jev" else None
                        before = len(report.get("model_calls", [])), len(report["jev_calls"])
                        started = time.perf_counter()
                        response = api.post(f"/api/books/{bid}/ask", json={
                            "question": case["question"], "bookmark": case["bookmark"],
                        })
                        value = response.json()
                        claims = value.get("claims", [])
                        text = " ".join(c["text"] for c in claims).lower()
                        citations = value.get("citations", [])
                        passed = (response.status_code == 200
                                  and value.get("as_of_chapter") == case["bookmark"]
                                  and all(c["ordinal"] <= case["bookmark"] for c in citations)
                                  and (value.get("insufficient_evidence") is True and not claims
                                       if case["expected"] == "refuse" else
                                       bool(claims) and bool(citations)
                                       and all(t in text for t in case["required"])))
                        report["cases"].append({**case, "mode": mode,
                            "seconds": round(time.perf_counter()-started, 4), "status": response.status_code,
                            "basic_check": passed, "response": value,
                            "model_calls": len(report.get("model_calls", []))-before[0],
                            "jev_calls": len(report["jev_calls"])-before[1]})
                        save()
                for mode in ("gpt", "jev"):
                    provider.jev = jev if mode == "jev" else None
                    app.state.recaps.invalidate_book(bid)
                    for cache in ("cold", "warm"):
                        started = time.perf_counter()
                        response = api.get(f"/api/books/{bid}/catch-me-up", params={"bookmark": 999})
                        report.setdefault("recaps", []).append({
                            "mode": mode, "cache": cache, "seconds": round(time.perf_counter()-started, 4),
                            "status": response.status_code, "response": response.json()})
                        save()
                api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 0})
                reset = api.get(f"/api/books/{bid}/catch-me-up", params={"bookmark": 999})
                report["reset"] = reset.json()
                report["status"] = "completed-needs-manual-review"
            finally:
                report["cost"] = cost_snapshot(app.state.catalog, bid)
                provider.jev = None
                jev.close()
                report["latency"] = {mode: {"median_seconds": statistics.median(
                    c["seconds"] for c in report["cases"] if c["mode"] == mode)}
                    for mode in ("gpt", "jev") if any(c["mode"] == mode for c in report["cases"])}
                save()
    print(json.dumps({"status": report["status"], "latency": report["latency"],
                      "basic_passed": sum(c["basic_check"] for c in report["cases"]),
                      "cases": len(report["cases"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
