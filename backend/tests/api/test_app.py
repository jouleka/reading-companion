"""Module E / the FastAPI service skeleton (ADR 0007 D-A7/D-A11): app factory + config + lifespan.

Every test runs against a REAL Store/Catalog under a tmp data dir with the offline stub LLM
(ALLOW_STUB) — no network. The D-A7 fail-loud predicate is pinned: a deploy that silently resolves to
the stub must refuse to start unless ALLOW_STUB is explicitly set.
"""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _settings(tmp_path, **kw):
    kw.setdefault("allow_stub", True)
    kw.setdefault("data_dir", str(tmp_path / "data"))
    # _env_file=None: unit tests must not inherit the developer's repo-root .env (a real key there
    # would silently flip the provider away from the stub)
    return Settings(_env_file=None, **kw)


@pytest.fixture
def client(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:                     # context manager runs the lifespan
        yield c


def test_app_boots_and_serves_an_empty_shelf(client):
    r = client.get("/api/books")
    assert r.status_code == 200
    assert r.json() == []


def test_api_docs_are_disabled_by_default(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_browser_security_headers_cover_api_and_block_untrusted_hosts(client):
    response = client.get("/api/books")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "script-src-attr 'none'" in policy
    assert "frame-ancestors 'none'" in policy

    rejected = client.get("/api/books", headers={"host": "attacker.example"})
    assert rejected.status_code == 400
    assert rejected.headers["content-security-policy"] == policy


def test_built_frontend_can_be_served_by_the_api_process(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>Reading Companion</h1>", encoding="utf-8")
    (dist / "asset.txt").write_text("built asset", encoding="utf-8")
    app = create_app(_settings(tmp_path, frontend_dist_dir=str(dist)))

    with TestClient(app) as production_client:
        home = production_client.get("/")
        assert home.text == "<h1>Reading Companion</h1>"
        assert "script-src 'self'" in home.headers["content-security-policy"]
        assert production_client.get("/asset.txt").text == "built asset"
        assert production_client.get("/api/books").json() == []


def test_stub_without_allow_stub_fails_loud(tmp_path):
    """D-A7 default-deny: no real provider resolves and ALLOW_STUB is not set -> the app HARD-FAILS at
    startup (never a silent stub deploy)."""
    app = create_app(_settings(tmp_path, allow_stub=False))
    with pytest.raises(RuntimeError):
        with TestClient(app):
            pass


def test_env_file_is_the_absolute_repo_root_dotenv():
    """D-A7: the settings env_file is the ABSOLUTE, __file__-derived repo-root .env (never a CWD
    lookup)."""
    from pathlib import Path

    from app.config import ENV_FILE
    assert Path(ENV_FILE).is_absolute()
    assert Path(ENV_FILE).parent.name == "reading-companion"
    assert Path(ENV_FILE).name == ".env"
