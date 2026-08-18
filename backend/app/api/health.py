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
