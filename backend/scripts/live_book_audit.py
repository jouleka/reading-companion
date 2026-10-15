"""Opt-in, capped Ask acceptance against a disposable snapshot of an existing local book.

Gold cases must be authored from the imported text before the run. This never re-ingests or edits
the source book: SQLite's backup API takes a consistent copy including WAL, and only that temporary
copy receives simulated reading positions and new provider-cost receipts. Every answer still runs
through the normal HTTP route, retrieval, model generation, source gates, and citation review.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import shutil
import sqlite3
import sys
import tempfile
import time
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.api import views
from app.config import Settings
from app.cost import pricing_known
from app.main import create_app
from app.retrieval_passages import PASSAGE_CHARS
from scripts.live_quality_audit import cost_snapshot, failure_details, install_call_recording


def snapshot_db(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as reader:
        with closing(sqlite3.connect(target)) as writer:
            reader.backup(writer)


def response_data(response):
    try:
        value = response.json()
        return value if isinstance(value, dict) else {"error": "unexpected response shape"}
    except ValueError:
        return {"error": "non-JSON response"}


class AskSourceCapture:
    """Distinguish the retrieval pool from the final evidence without changing either call."""

    def __init__(self):
        self.context_enabled = False
        self.reset()

    def reset(self):
        self.retrieval = None
        self.selection = None
        self.selection_failure = None
        self.context_completion = None
        self.context_failure = None

    def snapshot(self):
        candidates = self.retrieval["sources"] if self.retrieval is not None else None
        if self.context_completion is not None:
            selected, method = self.context_completion, "context_completed"
        elif self.selection is not None and not self.context_enabled:
            selected, method = self.selection, "validated_selection"
        elif self.selection == []:
            selected, method = [], "empty_selection"
        elif self.retrieval is not None and not candidates:
            selected, method = [], "empty_candidates"
        elif self.retrieval is not None and not self.retrieval["candidate_pool"]:
            selected, method = candidates, "direct_retrieval"
        else:
            selected, method = None, "not_captured"
        return {
            "candidate_sources": copy.deepcopy(candidates),
            "model_selected_sources": copy.deepcopy(self.selection),
            "selected_sources": copy.deepcopy(selected),
            # Compatibility with older reports: this always means final evidence, never candidates.
            "retrieved_sources": copy.deepcopy(selected),
            "candidate_sources_captured": self.retrieval is not None,
            "model_selection_captured": self.selection is not None,
            "context_completion_captured": self.context_completion is not None,
            "selected_sources_captured": selected is not None,
            "selected_capture_method": method,
            "selection_failure": self.selection_failure,
            "context_failure": self.context_failure,
        }


@contextmanager
def capture_ask_sources():
    """Observe retrieval, model validation, and final deterministic context without changing them."""
    captured = AskSourceCapture()
    retrieve, validate = views.retrieve_ask_sources, views.validate_selection
    complete_context = getattr(views, "complete_ask_context", None)
    captured.context_enabled = complete_context is not None

    def observed_retrieve(*values, **kwargs):
        sources = retrieve(*values, **kwargs)
        captured.retrieval = {"bookmark": kwargs["bookmark"],
                              "candidate_pool": kwargs.get("candidate_pool", False),
                              "sources": copy.deepcopy(sources)}
        return sources

    def observed_selection(*values, **kwargs):
        try:
            sources = validate(*values, **kwargs)
        except Exception as exc:
            captured.selection_failure = failure_details(exc)
            raise
        captured.selection = copy.deepcopy(sources)
        return sources

    def observed_context(*values, **kwargs):
        try:
            sources = complete_context(*values, **kwargs)
        except Exception as exc:
            captured.context_failure = failure_details(exc)
            raise
        captured.context_completion = copy.deepcopy(sources)
        return sources

    with ExitStack() as stack:
        stack.enter_context(patch.object(views, "retrieve_ask_sources", observed_retrieve))
        stack.enter_context(patch.object(views, "validate_selection", observed_selection))
        if complete_context is not None:
            stack.enter_context(patch.object(views, "complete_ask_context", observed_context))
        yield captured


def source_pool_checks(sources, *, effective, atoms, read_raw):
    """Check exact normalized excerpts and source metadata without loading ineligible chapters."""
    if sources is None:
        return {"boundary_and_normalized_excerpt": None, "check_count": None}
    checks = []
    for source in sources:
        ordinal, key = source.get("ordinal"), source.get("chapter_key")
        atom = atoms.get(key, {})
        if (isinstance(ordinal, bool) or not isinstance(ordinal, int)
                or not 0 < ordinal <= effective or ordinal != atom.get("ordinal")):
            checks.append(False)
            continue
        text = source.get("text")
        raw = read_raw(key) or ""
        checks.append(
            isinstance(text, str) and 0 < len(text) <= PASSAGE_CHARS and text in " ".join(raw.split())
            and source.get("href") == atom.get("href", "")
            and source.get("title") == (atom.get("title", "") or f"Chapter {ordinal}")
        )
    return {"boundary_and_normalized_excerpt": all(checks) if checks else None,
            "check_count": len(checks)}


def selected_subset_check(candidates, selected):
    if candidates is None or selected is None:
        return None
    available = {source["id"]: source for source in candidates}
    return (len(available) == len(candidates)
            and len({source["id"] for source in selected}) == len(selected)
            and all(source["id"] in available and source == available[source["id"]]
                    for source in selected))


def citation_identity_check(citations, selected):
    if not citations:
        return None
    if selected is None:
        return False
    available = {source["id"]: source for source in selected}
    return all(
        citation.get("id") in available
        and citation.get("excerpt") == available[citation["id"]]["text"]
        and all(citation.get(key) == available[citation["id"]][key]
                for key in ("ordinal", "chapter_key", "href", "title"))
        for citation in citations
    )


def run(settings, args, report, cases):
    source_book = args.data_dir.resolve() / "books" / args.book_id
    # Never accept path traversal in the user-supplied identifier.
    if source_book.parent != args.data_dir.resolve() / "books":
        raise ValueError("invalid book identifier")
    with tempfile.TemporaryDirectory(prefix="reading-companion-book-qa-") as scratch:
        data_dir = Path(scratch) / "data"
        book_dir = data_dir / "books" / args.book_id
        book_dir.mkdir(parents=True)
        snapshot_db(args.data_dir / "catalog.db", data_dir / "catalog.db")
        snapshot_db(source_book / "memory.db", book_dir / "memory.db")
        shutil.copy2(source_book / "atoms.json", book_dir / "atoms.json")
        manifest = json.loads((book_dir / "atoms.json").read_text(encoding="utf-8"))
        atoms = {atom["key"]: atom for atom in manifest["atoms"]}
        with closing(sqlite3.connect(data_dir / "catalog.db")) as connection, connection:
            baseline = connection.execute(
                "SELECT COALESCE(SUM(usd),0) FROM cost_ledger WHERE book_id=?", (args.book_id,)
            ).fetchone()[0]
            pending = connection.execute(
                "SELECT COUNT(*) FROM cost_reservations WHERE book_id=?", (args.book_id,)
            ).fetchone()[0]
            if pending:
                raise ValueError("source book has unsettled provider reservations")
            # Simulation fixture only. Keep all historical ledger entries and every memory row.
            connection.execute("UPDATE reading_state SET bookmark=0,cfi=NULL WHERE book_id=?",
                               (args.book_id,))
        local = settings.model_copy(update={
            "data_dir": str(data_dir), "deployment_mode": "local", "anthropic_api_key": None,
            "allow_stub": False, "openai_base_url": "https://api.openai.com/v1",
            "llm_cheap_model": args.model, "llm_large_model": args.model,
            "embed_provider": "openai-compatible", "embed_base_url": "https://api.openai.com/v1",
            "embed_api_key": None, "embed_model": "text-embedding-3-small",
            "cost_max_usd_per_book": baseline + args.budget_usd, "frontend_dist_dir": None,
            "trusted_hosts": "testserver", "cost_max_output_tokens_per_call": 4096,
        })
        app = create_app(local)
        with TestClient(app, raise_server_exceptions=False) as client:
            provider = app.state.client
            provider.max_retries = 0
            provider._oai = provider._oai.with_options(max_retries=0)
            provider._embed_oai = provider._embed_oai.with_options(max_retries=0)
            old_ledger_count = len(app.state.catalog.get_costs(args.book_id))
            install_call_recording(provider, report, local.cost_max_output_tokens_per_call)
            try:
                with capture_ask_sources() as captured:
                    for case in sorted(cases, key=lambda value: value["bookmark"]):
                        frontier = case["bookmark"]
                        state = app.state.catalog.get_state(args.book_id)
                        app.state.catalog.set_position(args.book_id, "audit-simulation", frontier,
                                                       expected_epoch=state["position_epoch"])
                        requested = case.get("requested_bookmark", frontier)
                        with app.state.store.book(args.book_id) as memory:
                            durable = memory.completion_frontier(manifest["atoms"])
                        effective = min(frontier, requested, state["ingest_progress"], durable)
                        captured.reset()
                        start = time.perf_counter()
                        response = client.post(f"/api/books/{args.book_id}/ask", json={
                            "question": case["question"],
                            "bookmark": requested,
                        })
                        payload = response_data(response)
                        evidence = captured.snapshot()
                        with app.state.store.book(args.book_id) as memory:
                            candidate_checks = source_pool_checks(
                                evidence["candidate_sources"], effective=effective, atoms=atoms,
                                read_raw=memory.view(effective).raw_text,
                            )
                            selected_checks = source_pool_checks(
                                evidence["selected_sources"], effective=effective, atoms=atoms,
                                read_raw=memory.view(effective).raw_text,
                            )
                        citations = payload.get("citations", [])
                        citation_checks = citation_identity_check(citations, evidence["selected_sources"])
                        refused = payload.get("insufficient_evidence") is True
                        basic = response.status_code == 200 and payload.get("as_of_chapter") == effective
                        if case["expected"] == "refuse":
                            basic = basic and refused and not payload.get("claims") and not citations
                        else:
                            basic = basic and not refused and bool(payload.get("claims")) and bool(citations)
                        report["cases"].append({
                            **case, "seconds": round(time.perf_counter() - start, 3),
                            "http_status": response.status_code, "basic_check": basic,
                            "effective_bookmark": effective,
                            "candidate_source_boundary_and_normalized_excerpt": candidate_checks["boundary_and_normalized_excerpt"],
                            "candidate_source_check_count": candidate_checks["check_count"],
                            "selected_source_boundary_and_normalized_excerpt": selected_checks["boundary_and_normalized_excerpt"],
                            "selected_source_check_count": selected_checks["check_count"],
                            "selected_sources_subset_of_candidates": selected_subset_check(
                                evidence["candidate_sources"], evidence["selected_sources"]),
                            "source_boundary_and_normalized_excerpt": selected_checks["boundary_and_normalized_excerpt"],
                            "source_check_count": selected_checks["check_count"],
                            "citation_identity": citation_checks,
                            "citation_check_count": len(citations), "response": payload,
                            **evidence,
                        })
                        # A failure requiring external state must not start a paid retry loop.
                        if response.status_code in (401, 403, 429, 503):
                            break
                report["status"] = "ran-needs-manual-review"
            finally:
                report["cost"] = cost_snapshot(app.state.catalog, args.book_id,
                                               start=old_ledger_count, historical_usd=baseline)
                try:
                    report["runtime"] = response_data(client.get("/api/health/ai"))
                except Exception as exc:
                    report["runtime"] = {"failure": failure_details(exc)}
                report["source_snapshot"] = {
                    "chapters": len(atoms),
                    "manifest_sha256": hashlib.sha256((book_dir / "atoms.json").read_bytes()).hexdigest(),
                    "extraction_rebuilt": False,
                }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--book-id", required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", choices=["gpt-6-luna", "gpt-6.1-sol"], default="gpt-6-luna")
    parser.add_argument("--budget-usd", type=float, default=0.25)
    args = parser.parse_args()
    if not math.isfinite(args.budget_usd) or not 0 < args.budget_usd <= 1:
        parser.error("budget must be greater than zero and at most $1")
    document = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = document["cases"] if isinstance(document, dict) else document
    if not cases or len(cases) > 24:
        parser.error("supply between one and 24 pre-authored cases")
    settings = Settings()
    report = {"status": "pending", "live_calls_made": False, "model": args.model,
              "incremental_ceiling_usd": args.budget_usd, "book_id": args.book_id,
              "gold_cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
              "scope": "Real imported text. Disposable snapshot; original book and ledger unchanged. "
                       "Case content contains spoilers up to each explicitly labeled frontier. "
                       "A small targeted sample is not a general reliability estimate.", "cases": []}
    if not settings.openai_api_key:
        report["pending_reason"] = "OpenAI credentials are not configured."
    elif not args.run:
        report["pending_reason"] = "Pass --run for explicitly authorized capped provider testing."
    elif not pricing_known(args.model):
        report["pending_reason"] = "Explicit model pricing is required before a paid run."
    else:
        report["live_calls_made"] = True
        try:
            run(settings, args, report, cases)
        except Exception as exc:
            report.update(status="failed", failure_type=type(exc).__name__, failure=failure_details(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "live_calls_made", "model")}))
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
