"""Acceptance-harness bookkeeping and cleanup regressions; all providers remain explicitly offline."""
from contextlib import closing
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from app.catalog.catalog import Catalog
from app.config import Settings
from app.cost import budgeted_completion, budgeted_embedding
from app.ingest.extraction.validation import ExtractionSafetyError
from app.llm.client import ProviderUnavailable
from app.main import create_app
from scripts import live_book_audit as book_audit
from scripts import live_quality_audit as quality
from tests.api._epub import three_chapter_book


def _settings(tmp_path):
    return Settings(
        _env_file=None, data_dir=str(tmp_path), allow_stub=True,
        openai_api_key=None, anthropic_api_key=None, openai_base_url=None,
        embed_provider="stub", embed_api_key=None, embed_base_url=None,
        embed_model="lexical-stub-256", llm_cheap_model="gpt-6-luna", llm_large_model="gpt-6-luna",
    )


def test_classic_audit_never_counts_fast_errors_as_answers_or_spoiler_refusals():
    from scripts.classic_books_audit import summarize
    report = {'books': [{'usd': .01, 'checkpoints': [{'frontier': 1,
        'ingest': {'ingest_progress': 1}, 'probes': [
            {'expected': 'answer', 'status': 429, 'seconds': .01, 'value': {}},
            {'expected': 'answer', 'status': 200, 'seconds': 4, 'valid_shape': True,
             'value': {'cached': True}},
            {'expected': 'refuse', 'status': 502, 'seconds': .02, 'value': {}},
        ]}]}], 'provider_calls': []}
    result = summarize(report)
    assert result['answered'] == result['prepared_reuses'] == 1
    assert result['answer_latency']['median'] == 4
    assert result['answer_latency']['under_1_5'] == 0
    assert result['future_refusals'] == 0 and result['future_questions'] == 1


def test_snapshot_closes_both_sqlite_handles_before_windows_cleanup(tmp_path, monkeypatch):
    source, target = tmp_path / "original.db", tmp_path / "copy.db"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE facts(value)")
        connection.execute("INSERT INTO facts VALUES (1)")
        connection.commit()
    original_connect, handles = sqlite3.connect, []

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    def tracked(*args, **kwargs):
        connection = original_connect(*args, **kwargs, factory=TrackedConnection)
        handles.append(connection)
        return connection

    monkeypatch.setattr(book_audit.sqlite3, "connect", tracked)
    book_audit.snapshot_db(source, target)
    assert len(handles) == 2 and all(handle.closed for handle in handles)
    target.unlink()  # Windows rejects this if the copy's SQLite connection still holds a file handle.
    assert source.exists()


def test_completion_and_embedding_failures_are_traced_once_with_safe_types_and_paid_reservations(tmp_path):
    catalog = Catalog(str(tmp_path / "catalog.db"))
    catalog.add_book("b", title="Synthetic audit")
    report, caps = {}, []

    def complete(system, user, tier, schema, cap):
        caps.append(cap)
        raise ProviderUnavailable("authentication", service="completion")

    def embed(texts):
        raise ProviderUnavailable("network", service="embedding")

    provider = SimpleNamespace(
        provider="openai-compatible", _is_native_openai=True, complete=complete, embed=embed,
        _model_for=lambda tier: "gpt-6-luna", embed_identity=lambda: "text-embedding-3-small",
        api_key="private-canary-must-not-appear",
    )
    quality.install_call_recording(provider, report, 2048)
    settings = _settings(tmp_path)
    settings.cost_max_output_tokens_per_call = 2048
    with pytest.raises(ProviderUnavailable):
        budgeted_completion(catalog, settings, provider, "b", phase="synthesis",
                            system="Only source.", user="Evidence.", tier="large")
    with pytest.raises(ProviderUnavailable):
        budgeted_embedding(catalog, settings, provider, "b", phase="search-embedding", texts=["Question."])
    assert caps == [2048]
    [completion], [embedding] = report["model_calls"], report["embedding_calls"]
    assert completion["status"] == embedding["status"] == "failed"
    assert completion["usage"] is None and embedding["usage"] is None
    assert completion["failure"]["provider_kind"] == "authentication"
    assert completion["configured_reasoning_effort"] == "medium"
    assert completion["configured_service_tier"] == "default"
    assert len(completion["request_sha256"]) == 64
    assert embedding["failure"]["provider_service"] == "embedding"
    assert len(catalog.get_costs("b")) == 2
    assert catalog.get_cost_reservations("b") == []
    assert "private-canary" not in json.dumps(report)
    catalog.close()


def test_success_trace_honors_explicit_cap_and_logs_usage_without_changing_results():
    seen, report = [], {}

    def complete(system, user, tier, schema, cap):
        seen.append(cap)
        return {"accepted": True}, {"in": 10, "out": 3}

    provider = SimpleNamespace(complete=complete, embed=lambda texts: ([[1.0]], {"in": 4}),
                               _model_for=lambda tier: "gpt-6-luna", embed_identity=lambda: "embedding")
    quality.install_call_recording(provider, report, 2048)
    result = provider.complete("Source.", "Evidence.", max_output_tokens=4096)
    assert seen == [2048] and result == ({"accepted": True}, {"in": 10, "out": 3})
    assert report["model_calls"][0]["status"] == "succeeded"
    assert report["model_calls"][0]["input_estimate"] > 0
    assert "configured_reasoning_effort" not in report["model_calls"][0]


def test_wrapped_review_failure_preserves_safe_provider_classification_without_messages():
    try:
        try:
            raise ProviderUnavailable("authentication", service="completion")
        except ProviderUnavailable as cause:
            raise ExtractionSafetyError("private-canary raw validation material") from cause
    except ExtractionSafetyError as error:
        details = quality.failure_details(error)
    assert details["type"] == "ExtractionSafetyError"
    assert details["provider_kind"] == "authentication"
    assert details["cause_types"] == ["ExtractionSafetyError", "ProviderUnavailable"]
    assert "private-canary" not in json.dumps(details)


def test_cost_snapshot_keeps_incremental_settled_and_pending_exposure(tmp_path):
    catalog = Catalog(str(tmp_path / "catalog.db"))
    catalog.add_book("b", title="Synthetic audit")
    catalog.record_cost("b", phase="old", model="m", usd=0.1)
    catalog.record_cost("b", phase="new", model="m", usd=0.002)
    catalog.reserve_cost("b", phase="pending", model="m", input_tokens=20, output_tokens=10,
                         usd=0.01, max_input_tokens=1000, max_output_tokens=1000, max_usd=1)
    snapshot = quality.cost_snapshot(catalog, "b", start=1, historical_usd=0.1)
    assert snapshot["usd"] == pytest.approx(0.002)
    assert snapshot["pending_reserved_usd"] == pytest.approx(0.01)
    assert snapshot["recorded_exposure_usd"] == pytest.approx(0.012)
    assert snapshot["historical_usd_preserved"] == 0.1
    assert len(snapshot["ledger"]) == len(snapshot["pending_reservations"]) == 1
    catalog.close()


class OfflineHarnessClient(TestClient):
    def __enter__(self):
        super().__enter__()
        # Satisfy the harness's SDK retry configuration without constructing/using a real SDK.
        configured = SimpleNamespace(with_options=lambda **kwargs: None)
        self.app.state.client._oai = configured
        self.app.state.client._embed_oai = configured
        return self


def _offline_app(settings, **kwargs):
    safe = settings.model_copy(update={
        "allow_stub": True, "openai_api_key": None, "anthropic_api_key": None,
        "openai_base_url": None, "embed_api_key": None, "embed_base_url": None,
        "embed_provider": "stub", "embed_model": "lexical-stub-256",
    })
    return create_app(safe, **kwargs)


def test_synthetic_harness_finally_preserves_spend_after_post_call_exception_and_cleans_temp(tmp_path, monkeypatch):
    directories = []

    def offline_app(settings, **kwargs):
        directories.append(settings.data_dir)
        return _offline_app(settings, **kwargs)

    def interrupted(app, settings, book_id, report):
        app.state.catalog.record_cost(book_id, phase="completed-fixture-call", model="m", usd=0.003)
        app.state.catalog.reserve_cost(book_id, phase="uncertain-fixture-call", model="m",
                                       input_tokens=20, output_tokens=10, usd=0.007,
                                       max_input_tokens=1000, max_output_tokens=1000, max_usd=1)
        report["cases"].append({"id": "already-retained"})
        raise RuntimeError("private-canary after an already-accounted call")

    monkeypatch.setattr(quality, "create_app", offline_app)
    monkeypatch.setattr(quality, "TestClient", OfflineHarnessClient)
    monkeypatch.setattr(quality, "grounding_challenges", interrupted)
    report = {"per_book_ceiling_usd": 0.25, "cases": []}
    with pytest.raises(RuntimeError):
        quality.run(_settings(tmp_path), report)
    assert report["cases"] == [{"id": "already-retained"}]
    assert report["cost"]["usd"] == pytest.approx(0.003)
    assert report["cost"]["recorded_exposure_usd"] == pytest.approx(0.01)
    assert report["runtime"]["mode"] == "demo"
    assert all(not Path(path).exists() for path in directories)


@pytest.mark.parametrize("requested,expected", [(1, 1), (99, 2)])
def test_book_harness_checks_effective_frontier_and_preserves_original_state(tmp_path, monkeypatch, requested, expected):
    source = tmp_path / "original"
    settings = _settings(source)
    app = create_app(settings, ingest_executor=quality.InlineExecutor())
    with TestClient(app) as api:
        imported = api.post("/api/books", files={
            "file": ("fixture.epub", three_chapter_book(), "application/epub+zip"),
        })
        book_id = imported.json()["book_id"]
        atoms = json.loads((source / "books" / book_id / "atoms.json").read_text())["atoms"]
        advanced = api.put(f"/api/books/{book_id}/position", json={
            "cfi": "original-position", "offset": sum(atom["char_len"] for atom in atoms[:2]) + 2,
        })
        assert advanced.status_code == 200
        state_before = app.state.catalog.get_state(book_id)
        costs_before = app.state.catalog.get_costs(book_id)
        with app.state.store.book(book_id) as memory:
            pin_before = memory.pinned_identity()
    monkeypatch.setattr(book_audit, "create_app", _offline_app)
    monkeypatch.setattr(book_audit, "TestClient", OfflineHarnessClient)
    args = SimpleNamespace(data_dir=source, book_id=book_id, model="gpt-6-luna", budget_usd=0.25)
    report = {"cases": []}
    book_audit.run(settings, args, report, [{
        "id": "effective", "bookmark": 2, "requested_bookmark": requested,
        "question": "Where did Aldric work?", "expected": "refuse",
    }])
    [case] = report["cases"]
    assert case["effective_bookmark"] == expected and case["basic_check"] is True
    assert case["source_check_count"] > 0
    assert case["source_boundary_and_normalized_excerpt"] is True
    assert all(row["ordinal"] <= expected for row in case["retrieved_sources"])
    assert case["citation_check_count"] == 0 and case["citation_identity"] is None
    assert report["cost"]["pending_reservations"] == []
    with TestClient(create_app(settings, ingest_executor=quality.InlineExecutor())) as restored:
        assert restored.app.state.catalog.get_state(book_id) == state_before
        assert restored.app.state.catalog.get_costs(book_id) == costs_before
        with restored.app.state.store.book(book_id) as memory:
            assert memory.pinned_identity() == pin_before
