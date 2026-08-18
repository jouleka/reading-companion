"""Production liveness/readiness surfaces never expose credentials or take stored reading data offline."""

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from test_ingest import InlineExecutor


def test_health_is_live_while_provider_readiness_is_degraded(tmp_path):
    settings = Settings(_env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"))
    app = create_app(settings, ingest_executor=InlineExecutor())
    with TestClient(app) as client:
        app.state.client._record_provider_failure("completion", "authentication")

        live = client.get("/api/health/live")
        ready = client.get("/api/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 503
    assert ready.json() == {
        "status": "degraded",
        "provider": {
            "completion": {"status": "degraded", "reason": "authentication"},
            "embedding": {"status": "ready", "reason": None},
        },
    }
