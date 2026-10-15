"""Real local routes with synthetic books, no network, and deterministic inline ingestion."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _epub import three_chapter_book
from app.config import Settings
from app.llm.versioning import EMBED_CANARY_TEXT
from app.main import create_app
from test_ingest import InlineExecutor


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(
        _env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"),
        openai_api_key=None, anthropic_api_key=None, openai_base_url=None,
        embed_provider=None, embed_base_url=None, embed_api_key=None, embed_model=None,
        llm_cheap_model=None, llm_large_model=None,
    )
    app = create_app(settings, ingest_executor=InlineExecutor())
    with TestClient(app) as api:
        imported = api.post("/api/books", files={
            "file": ("audit.epub", three_chapter_book(), "application/epub+zip"),
        })
        assert imported.status_code == 201, imported.text
        book_id = imported.json()["book_id"]
        manifest = json.loads((Path(settings.data_dir) / "books" / book_id / "atoms.json").read_text())
        lengths = [atom["char_len"] for atom in manifest["atoms"]]
        assert api.put(f"/api/books/{book_id}/position", json={
            "cfi": "audit-1", "offset": lengths[0] + 2,
        }).status_code == 200
        yield api, app, book_id, lengths


@pytest.mark.parametrize("component", ["embedding", "extractor"])
def test_changed_model_cannot_extend_memory_or_spend_before_the_guard(runtime, component):
    api, app, book_id, lengths = runtime
    client = app.state.client
    before_costs = app.state.catalog.get_costs(book_id)
    if component == "embedding":
        client.embed_identity = lambda: "stub:other-same-dimension-space"
    else:
        client.cheap_model = "other-extractor"

    def forbidden_io(*_args, **_kwargs):
        pytest.fail("known configuration mismatch must fail before provider IO")

    client.embed = forbidden_io
    client.complete = forbidden_io
    position = api.put(f"/api/books/{book_id}/position", json={
        "cfi": "audit-2", "offset": sum(lengths[:2]) + 2,
    })
    assert position.status_code == 200
    status = api.get(f"/api/books/{book_id}/ingest").json()
    assert status["ingest_progress"] == 1
    assert status["status"] == "error" and "ModelMigrationRequired" in status["error"]
    assert app.state.catalog.get_costs(book_id) == before_costs
    with app.state.store.book(book_id) as memory:
        assert len(memory._audit_all("chunks")) == 1
        assert len(memory._audit_all("ingested_chapters")) == 1
    for path in ("graph", "notes", "timeline", "manifest", "epub"):
        assert api.get(f"/api/books/{book_id}/{path}").status_code == 200


def test_changed_embedder_rejects_query_before_any_spend(runtime):
    api, app, book_id, _lengths = runtime
    before = app.state.catalog.get_costs(book_id)
    app.state.client.embed_identity = lambda: "stub:other-space"
    app.state.client.embed = lambda _texts: pytest.fail("mismatch query must not reach provider")
    responses = [
        api.get(f"/api/books/{book_id}/search", params={"q": "Aldric"}),
        api.post(f"/api/books/{book_id}/ask", json={"question": "Aldric"}),
    ]
    for response in responses:
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "model_migration_required"
    assert app.state.catalog.get_costs(book_id) == before


def test_same_name_space_drift_cannot_rank_old_vectors(runtime):
    api, app, book_id, _lengths = runtime
    client, batches = app.state.client, []
    original_embed = client.embed

    def rotated(texts):
        assert not app.state.store._lock_for(book_id).locked()
        batches.append(list(texts))
        vectors, usage = original_embed(texts)
        return [vector[1:] + vector[:1] for vector in vectors], usage

    client.embed = rotated
    responses = [
        api.get(f"/api/books/{book_id}/search", params={"q": "Aldric"}),
        api.post(f"/api/books/{book_id}/ask", json={"question": "Aldric"}),
    ]
    for response in responses:
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "model_migration_required"
    assert batches == [["Aldric", EMBED_CANARY_TEXT]] * 2, "each query and canary share one billed request"


def test_changed_synthesis_and_extractor_do_not_block_compatible_read_search(runtime):
    api, app, book_id, _lengths = runtime
    app.state.client.cheap_model = "future-extractor"
    app.state.client.large_model = "future-synthesis"
    response = api.get(f"/api/books/{book_id}/search", params={"q": "Aldric"})
    assert response.status_code == 200
    assert response.json()["hits"]


def test_same_name_canary_drift_stops_before_extraction(runtime):
    api, app, book_id, lengths = runtime
    client, batches = app.state.client, []
    original_embed = client.embed
    def rotated(texts):
        batches.append(list(texts))
        vectors, usage = original_embed(texts)
        return [vector[1:] + vector[:1] for vector in vectors], usage
    client.embed = rotated
    client.complete = lambda *_args, **_kwargs: pytest.fail("space drift must stop before extraction")
    assert api.put(f"/api/books/{book_id}/position", json={
        "cfi": "audit-2", "offset": sum(lengths[:2]) + 2,
    }).status_code == 200
    status = api.get(f"/api/books/{book_id}/ingest").json()
    assert status["status"] == "error" and status["ingest_progress"] == 1
    assert "ModelMigrationRequired" in status["error"]
    assert batches == [[EMBED_CANARY_TEXT]]
