"""Process liveness and provider-aware readiness for the local production wrapper."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.deps import get_client

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("/live")
def live():
    return {"status": "ok"}


@router.get("/ready")
def ready(client=Depends(get_client)):
    provider = client.provider_status()
    degraded = any(item["status"] == "degraded" for item in provider.values())
    payload = {"status": "degraded" if degraded else "ready", "provider": provider}
    return JSONResponse(payload, status_code=503 if degraded else 200)


@router.get("/ai")
def ai_runtime(client=Depends(get_client)):
    """Describe local AI truthfully without probing a provider or exposing keys/endpoints.

    Readiness means that the process can serve requests. A deliberately enabled test stub can be
    ready too, so the UI needs this separate distinction before presenting it as working AI.
    """
    health = client.provider_status()
    return {
        "mode": "demo" if client.provider == "stub" else "live",
        "provider": client.provider,
        "models": {"extraction": client.version["cheap"], "synthesis": client.version["large"]},
        "completion_status": health["completion"]["status"],
        "embedding_mode": "demo" if client.embed_identity().startswith("stub:") else "live",
    }
