"""The FastAPI app factory + lifespan (ADR 0007 D-A11). ``create_app(settings)`` builds the service
around the per-process singletons; the lifespan owns their startup/shutdown (Store.close serializes
against in-flight sessions; the D-A7 fail-loud predicate fires during startup, refusing a stub deploy
unless ALLOW_STUB is explicitly set)."""
import logging
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles

from app import deps
from app.api import books, health, ingest, reading, views
from app.config import Settings
from app.ingest.worker import IngestWorker
from app.hosted.auth import api as auth_api
from app.hosted.auth.oidc import OIDCClient
from app.hosted.auth.repository import PostgresAuthRepository
from app.hosted.credentials import build_credential_cipher
from app.hosted.provider_settings import ProviderValidator, allowed_origins
from app.hosted.runtime import HostedRuntimeRegistry
from app.hosted.storage import build_object_storage
from app.hosted.tenant import api as tenant_api
from app.hosted.tenant.repository import PostgresTenantRepository
from app.lifecycle.archive import DataDirLock
from app.lifecycle_cache import RecapRegistry
from app.llm.client import ProviderUnavailable

_MULTIPART_OVERHEAD_BYTES = 64 * 1024
_CREDENTIAL_BODY_BYTES = 32 * 1024
_LOG = logging.getLogger(__name__)
_CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    "base-uri 'none'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "frame-src 'self' blob:",
    "form-action 'self'",
    "script-src 'self'",
    "script-src-attr 'none'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data: blob:",
    "media-src 'self' data: blob:",
    "connect-src 'self'",
    "worker-src 'self' blob:",
))


class _SecurityHeaders:
    """Apply browser defenses to API, static, and error responses.

    The CSP is a security boundary for Foliate's same-origin blob iframes: EPUB markup is untrusted
    and must not be allowed to execute inline or blob-hosted scripts in the application origin.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def add_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["content-security-policy"] = _CONTENT_SECURITY_POLICY
                headers["x-content-type-options"] = "nosniff"
                headers["referrer-policy"] = "no-referrer"
                headers["x-frame-options"] = "DENY"
                headers["permissions-policy"] = "camera=(), microphone=(), geolocation=()"
            await send(message)

        await self.app(scope, receive, add_headers)


class _EpubUploadBodyLimit:
    """Bound multipart ingress before Starlette can spool an arbitrarily large upload to disk."""

    def __init__(self, app, *, max_file_bytes):
        self.app = app
        self.max_body_bytes = max_file_bytes + _MULTIPART_OVERHEAD_BYTES

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope.get("method") != "POST"
                or scope.get("path", "").rstrip("/") != "/api/books"):
            await self.app(scope, receive, send)
            return
        for key, value in scope.get("headers", ()):
            if key == b"content-length":
                try:
                    if int(value) > self.max_body_bytes:
                        await self._reject(scope, receive, send)
                        return
                except ValueError:
                    pass
                break
        # Buffer at most the policy limit before invoking Starlette's multipart parser. Most requests
        # stay in a small memory spool; large valid requests spill to a bounded temporary file. This
        # handles chunked bodies without relying on Content-Length or leaking parser-owned temp files.
        spool_memory = min(self.max_body_bytes, 1024 * 1024)
        with tempfile.SpooledTemporaryFile(max_size=spool_memory, mode="w+b") as body:
            received = 0
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    first = True

                    async def replay_disconnect():
                        nonlocal first
                        if first:
                            first = False
                            return message
                        return await receive()

                    await self.app(scope, replay_disconnect, send)
                    return
                chunk = message.get("body", b"")
                received += len(chunk)
                if received > self.max_body_bytes:
                    await self._reject(scope, receive, send)
                    return
                body.write(chunk)
                if not message.get("more_body", False):
                    break
            body.seek(0)

            async def replay_receive():
                chunk = body.read(64 * 1024)
                return {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": body.tell() < received,
                }

            await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope, receive, send):
        response = JSONResponse(
            {"detail": "EPUB upload exceeds the configured size limit"},
            status_code=413,
        )
        await response(scope, receive, send)


class _HostedNoStore:
    """Keep authenticated payloads and even identifier-specific failures out of shared caches."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return

        async def no_store_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["cache-control"] = "private, no-store"
                headers["pragma"] = "no-cache"
            await send(message)

        await self.app(scope, receive, no_store_send)


class _CredentialBodyLimit:
    """Bound secret-bearing JSON before parsing and before the application receives any bytes."""

    def __init__(self, app, *, max_body_bytes: int = _CREDENTIAL_BODY_BYTES):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            (scope.get("method") == "POST" and path.rstrip("/") == "/api/credentials")
            or (scope.get("method") == "PUT" and path.startswith("/api/credentials/"))
        ):
            await self.app(scope, receive, send)
            return
        for key, value in scope.get("headers", ()):
            if key == b"content-length":
                try:
                    if int(value) > self.max_body_bytes:
                        await self._reject(scope, receive, send)
                        return
                except ValueError:
                    pass
                break
        chunks: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                first = True

                async def replay_disconnect():
                    nonlocal first
                    if first:
                        first = False
                        return message
                    return await receive()

                await self.app(scope, replay_disconnect, send)
                return
            chunk = message.get("body", b"")
            received += len(chunk)
            if received > self.max_body_bytes:
                await self._reject(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        position = 0

        async def replay_receive():
            nonlocal position
            if position >= len(chunks):
                return {"type": "http.request", "body": b"", "more_body": False}
            chunk = chunks[position]
            position += 1
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": position < len(chunks),
            }

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope, receive, send):
        response = JSONResponse(
            {"detail": "credential request exceeds the configured size limit"}, status_code=413
        )
        await response(scope, receive, send)


def create_app(
    settings: Settings | None = None,
    ingest_executor=None,
    *,
    auth_repository=None,
    tenant_repository=None,
    oidc_client=None,
    oidc_http_transport=None,
    auth_clock=None,
    hosted_runtime=None,
    object_storage=None,
    credential_cipher=None,
    provider_validator=None,
) -> FastAPI:
    """``ingest_executor`` lets tests inject a deterministic inline executor; production uses the
    worker's own threadpool."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.deployment_mode == "hosted":
            repository = auth_repository or PostgresAuthRepository(
                settings.hosted_auth_dsn.get_secret_value()
            )
            tenant = tenant_repository or PostgresTenantRepository(
                settings.hosted_tenant_dsn.get_secret_value()
            )
            storage = None
            client = None
            runtime = None
            try:
                storage = object_storage or build_object_storage(settings)
                client = oidc_client or OIDCClient(
                    issuer=settings.oidc_issuer,
                    client_id=settings.oidc_client_id,
                    client_secret=settings.oidc_client_secret.get_secret_value(),
                    redirect_uri=settings.oidc_redirect_uri,
                    scopes=settings.oidc_scopes,
                    signing_algorithms=settings.oidc_signing_algorithm_values(),
                    clock_skew_seconds=settings.oidc_clock_skew_seconds,
                    timeout_seconds=settings.oidc_request_timeout_seconds,
                    transport=oidc_http_transport,
                )
                runtime = hosted_runtime or HostedRuntimeRegistry(
                    cache_max_entries=settings.hosted_runtime_cache_max_entries,
                    lock_max_entries=settings.hosted_runtime_lock_max_entries,
                )
                await repository.check_runtime_role()
                await tenant.check_runtime_role()
                cipher = credential_cipher or build_credential_cipher(settings)
                origins = allowed_origins(settings.hosted_provider_allowed_origins)
                validator = provider_validator or ProviderValidator(
                    timeout_seconds=settings.hosted_provider_validation_timeout_seconds
                )
                app.state.settings = settings
                app.state.auth_repository = repository
                app.state.tenant_repository = tenant
                app.state.oidc_client = client
                app.state.auth_clock = auth_clock or (lambda: datetime.now(UTC))
                app.state.hosted_runtime = runtime
                app.state.object_storage = storage
                app.state.credential_cipher = cipher
                app.state.provider_origins = origins
                app.state.provider_validator = validator
                yield
            finally:
                drained = True
                if runtime is not None:
                    drained = await runtime.close(
                        timeout=settings.hosted_runtime_shutdown_timeout_seconds
                    )
                    if not drained:
                        stats = await runtime.stats()
                        _LOG.warning(
                            "hosted runtime shutdown timed out with %d active operations",
                            stats["active_lock_leases"],
                        )
                if storage is not None and drained:
                    storage.close()
                if client is not None and oidc_client is None:
                    await client.close()
            return

        # LIT-24: restore swaps the whole data directory and therefore must never race a running app.
        # The lock file lives beside data_dir, so the protected inode is stable across a later swap.
        data_lock = DataDirLock(settings.data_dir)
        data_lock.acquire()
        try:
            state = deps.build_state(settings)         # raises -> startup refused (D-A7)
            # Zero-token credential preflight. A failure marks readiness degraded while preserving
            # access to already-derived Codex/reading data; no credential or provider detail is logged.
            state["client"].probe()
            for k, v in state.items():
                setattr(app.state, k, v)
            app.state.worker = IngestWorker(state["store"], state["catalog"], state["client"],
                                            settings, executor=ingest_executor)
            # asyncio.Lock instances are app-lifespan / single-event-loop scoped. Keeping the registry
            # here (rather than in the thread-safe worker) also gives every TestClient lifespan fresh locks.
            app.state.book_request_locks: dict[str, deps._BookRequestLockEntry] = {}
            app.state.recaps = RecapRegistry(
                max_entries=settings.recap_cache_max_entries,
                max_failures=settings.recap_failure_max_entries,
                max_flights=settings.recap_max_inflight,
            )
            try:
                yield
            finally:
                app.state.worker.shutdown()
                app.state.store.close()
                app.state.catalog.close()
        finally:
            data_lock.release()

    docs_url = "/docs" if settings.expose_api_docs else None
    openapi_url = "/openapi.json" if settings.expose_api_docs else None
    app = FastAPI(
        title="reading-companion",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=None,
        openapi_url=openapi_url,
    )

    @app.exception_handler(ProviderUnavailable)
    async def provider_unavailable(_request, exc: ProviderUnavailable):
        return JSONResponse({"detail": exc.public_detail()}, status_code=503)

    if settings.deployment_mode == "hosted":
        # Hosted mode is intentionally partial: LIT-41 opens only the inventoried owner-scoped
        # read/reset routes. Storage, workers, and lifecycle operations remain fail-closed.
        @app.get("/api/health/live", tags=["health"])
        def hosted_live():
            return {"status": "ok"}

        app.include_router(auth_api.router)
        app.include_router(tenant_api.router)
        app.add_middleware(_EpubUploadBodyLimit, max_file_bytes=settings.epub_max_upload_bytes)
        app.add_middleware(_CredentialBodyLimit)
        app.add_middleware(_HostedNoStore)
    else:
        app.add_middleware(_EpubUploadBodyLimit, max_file_bytes=settings.epub_max_upload_bytes)
        app.include_router(health.router)
        app.include_router(books.router)
        app.include_router(reading.router)
        app.include_router(ingest.router)
        app.include_router(views.router)
    if settings.frontend_dist_dir and settings.deployment_mode == "local":
        # Mount last so every /api route keeps priority. StaticFiles validates the build directory
        # immediately, making a missing production build a startup/configuration error.
        app.mount("/", StaticFiles(directory=settings.frontend_dist_dir, html=True), name="frontend")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_host_values()))
    app.add_middleware(_SecurityHeaders)
    return app
