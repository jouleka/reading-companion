"""Local browser-origin boundary: CLI remains available, hostile browser requests do no work."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from _epub import three_chapter_book
from app.config import Settings
from app.main import _LocalOriginGuard, create_app


@pytest.fixture
def api(tmp_path):
    settings = Settings(
        _env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"),
        openai_api_key=None, anthropic_api_key=None, openai_base_url=None,
        embed_provider=None, embed_base_url=None, embed_api_key=None, embed_model=None,
    )
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.mark.parametrize("origin", [None, "http://testserver", "http://testserver:80"])
def test_local_cli_and_own_origin_can_import(api, origin):
    headers = {} if origin is None else {"Origin": origin}
    response = api.post("/api/books", headers=headers, files={
        "file": ("synthetic.epub", three_chapter_book(), "application/epub+zip"),
    })
    assert response.status_code == 201
    assert len(api.get("/api/books").json()) == 1


@pytest.mark.parametrize("origin", [
    "https://foreign.invalid", "null", "http://testserver:1234", "http://testserver:0", "https://testserver",
    "http://testserver/path", "http://testserver@foreign.invalid", "not an origin",
])
def test_foreign_opaque_and_malformed_origins_cannot_import(api, origin):
    response = api.post("/api/books", headers={"Origin": origin}, files={
        "file": ("synthetic.epub", three_chapter_book(), "application/epub+zip"),
    })
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "local_origin_required"
    assert api.get("/api/books").json() == []
    assert "foreign.invalid" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("headers", [
    {"Origin": "https://foreign.invalid"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"},
])
def test_cost_bearing_gets_are_guarded_before_lookup_or_provider_io(api, headers):
    response = api.get("/api/books/unknown/catch-me-up", headers=headers)
    assert response.status_code == 403


def test_rejected_upload_does_not_even_receive_the_body():
    messages = []
    async def forbidden(*_args):
        pytest.fail("foreign request reached body parsing or application work")
    async def send(message):
        messages.append(message)
    scope = {"type": "http", "method": "POST", "path": "/api/books", "scheme": "http",
             "headers": [(b"host", b"localhost:8000"), (b"origin", b"https://foreign.invalid")]}
    asyncio.run(_LocalOriginGuard(forbidden)(scope, forbidden, send))
    assert messages[0]["status"] == 403
    assert json.loads(messages[1]["body"])["detail"]["code"] == "local_origin_required"


def test_proxy_preserved_browser_host_is_valid_and_duplicate_origin_is_rejected():
    called, responses = [], []
    async def app(_scope, _receive, _send):
        called.append(True)
    async def receive():
        pytest.fail("origin guard should not consume a request body")
    async def send(message):
        responses.append(message)
    scope = {"type": "http", "method": "PUT", "path": "/api/books/book/position",
             "scheme": "http", "headers": [(b"host", b"127.0.0.1:5174"),
                                             (b"origin", b"http://127.0.0.1:5174")]}
    guard = _LocalOriginGuard(app)
    asyncio.run(guard(scope, receive, send))
    assert called == [True]
    scope["headers"].append((b"origin", b"https://foreign.invalid"))
    asyncio.run(guard(scope, receive, send))
    assert called == [True] and responses[0]["status"] == 403
