"""Cooperative local-worker shutdown never drains an entire paid target or leaves stale spend."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi.testclient import TestClient

from tests.api._epub import three_chapter_book
from app.config import Settings
from app.cost.limits import estimate_input_tokens
from app.ingest.extraction.prompts import extract_system_for, extract_user_prompt
from app.ingest.extraction.schema import Extraction
from app.ingest.worker import _IngestStopping
from app.main import create_app


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(
        _env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"),
        openai_api_key=None, anthropic_api_key=None, openai_base_url=None,
        embed_provider=None, embed_base_url=None, embed_api_key=None, embed_model=None,
    )
    app = create_app(settings)
    with TestClient(app) as api:
        imported = api.post("/api/books", files={
            "file": ("synthetic.epub", three_chapter_book(), "application/epub+zip"),
        })
        assert imported.status_code == 201
        yield api, app, imported.json()["book_id"]


def test_shutdown_waits_for_active_call_then_cancels_remaining_target(runtime):
    api, app, book_id = runtime
    worker, client = app.state.worker, app.state.client
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = client.complete

    def blocked(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5), "test did not release the synthetic provider"
        return original(*args, **kwargs)

    client.complete = blocked
    worker.enqueue(book_id, 3)
    assert entered.wait(5), "worker did not start the synthetic provider"
    with ThreadPoolExecutor(max_workers=1) as executor:
        shutdown = executor.submit(worker.shutdown)
        assert worker._stopping.wait(2)
        assert not shutdown.done(), "active provider work must finish before resources close"
        assert worker.enqueue(book_id, 3) is False
        release.set()
        shutdown.result(timeout=5)
    assert calls == [1], "shutdown must not start another chunk, review, or chapter call"
    assert worker.status(book_id)["status"] == "idle"
    assert app.state.catalog.get_cost_reservations(book_id) == []
    with app.state.store.book(book_id) as memory:
        assert memory._audit_all("ingested_chapters") == []
    assert api.get(f"/api/books/{book_id}/epub").status_code == 200


def test_shutdown_between_chunk_calls_settles_completed_partial_spend(runtime):
    _api, app, book_id = runtime
    worker, client = app.state.worker, app.state.client
    original, calls = client.complete, []
    def stop_after_first(*args, **kwargs):
        calls.append(1)
        value = original(*args, **kwargs)
        worker._stopping.set()
        return value
    client.complete = stop_after_first
    system = extract_system_for("novel", "und")
    overhead = estimate_input_tokens(system, extract_user_prompt("", [], ""), Extraction)
    app.state.settings.cost_max_input_tokens_per_call = overhead + 2000
    with pytest.raises(_IngestStopping):
        worker._complete_extraction(book_id, 1, "", [], "Aldric arrived at the forge. " * 1000)
    assert calls == [1]
    assert app.state.catalog.get_cost_reservations(book_id) == []
    assert app.state.catalog.get_costs(book_id), "completed work remains accounted, never free"
