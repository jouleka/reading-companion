"""Real-PostgreSQL, signed-OIDC acceptance tests for LIT-40."""

from __future__ import annotations

import base64
import os
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey
from psycopg import conninfo, sql

from app.config import Settings
from app.hosted.auth.api import CSRF_COOKIE, CSRF_HEADER, OIDC_COOKIE, SESSION_COOKIE
from app.hosted.auth.repository import AuthConfigurationError, PostgresAuthRepository
from app.hosted.auth.tokens import pkce_challenge
from app.hosted.migrations import apply_migrations
from app.main import create_app

pytestmark = pytest.mark.postgres

ISSUER = "https://idp.example"
CLIENT_ID = "litlet-client"
CLIENT_SECRET = "test-client-secret"
REDIRECT_URI = "https://reader.example/api/auth/callback"


@pytest.fixture(scope="module")
def admin_dsn() -> str:
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for the real PostgreSQL suite")
    return dsn


@pytest.fixture()
def database(admin_dsn: str):
    database_name = f"lit40_{uuid.uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    dsn = conninfo.make_conninfo(admin_dsn, dbname=database_name)
    apply_migrations(dsn)
    try:
        yield dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))


@pytest.fixture()
def auth_dsn(database: str, admin_dsn: str):
    role = f"lit40_auth_{uuid.uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOINHERIT BYPASSRLS").format(
                sql.Identifier(role)
            )
        )
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        conn.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON users TO {}").format(
                sql.Identifier(role)
            )
        )
        conn.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE ON external_identities, sessions TO {}").format(
                sql.Identifier(role)
            )
        )
        conn.execute(
            sql.SQL("GRANT SELECT, INSERT, DELETE ON oidc_login_attempts TO {}").format(
                sql.Identifier(role)
            )
        )
        conn.execute(sql.SQL("GRANT INSERT ON audit_events TO {}").format(sql.Identifier(role)))
    runtime_dsn = conninfo.make_conninfo(database, user=role)
    try:
        yield runtime_dsn
    finally:
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


class UnusedTenantRepository:
    async def check_runtime_role(self) -> None:
        return None


class UnusedObjectStorage:
    def close(self) -> None:
        return None


class SignedOIDCProvider:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.key = RSAKey.generate_key(auto_kid=True)
        self.nonce: str | None = None
        self.challenge: str | None = None
        self.subject = "provider-subject-1"
        self.email = "reader@example.test"
        self.claim_overrides: dict = {}
        self.tamper_signature = False
        self.token_exchanges = 0

    def observe_authorization_url(self, url: str) -> dict[str, str]:
        query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
        assert query["response_type"] == "code"
        assert query["client_id"] == CLIENT_ID
        assert query["redirect_uri"] == REDIRECT_URI
        assert query["scope"].split()[0] == "openid"
        assert query["code_challenge_method"] == "S256"
        self.nonce = query["nonce"]
        self.challenge = query["code_challenge"]
        return query

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": ISSUER + "/authorize",
                    "token_endpoint": ISSUER + "/token",
                    "jwks_uri": ISSUER + "/jwks",
                    "response_types_supported": ["code"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [self.key.as_dict()]})
        if request.url.path == "/token":
            self.token_exchanges += 1
            expected_basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            assert request.headers["authorization"] == "Basic " + expected_basic
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["authorization_code"]
            assert form["redirect_uri"] == [REDIRECT_URI]
            assert self.challenge == pkce_challenge(form["code_verifier"][0])
            now = int(self.clock().timestamp())
            claims = {
                "iss": ISSUER,
                "sub": self.subject,
                "aud": CLIENT_ID,
                "exp": now + 300,
                "iat": now,
                "nonce": self.nonce,
                "name": "Test Reader",
                "email": self.email,
                "email_verified": True,
            }
            claims.update(self.claim_overrides)
            id_token = jwt.encode({"alg": "RS256", "kid": self.key.kid}, claims, self.key)
            if self.tamper_signature:
                id_token = id_token[:-1] + ("A" if id_token[-1] != "A" else "B")
            return httpx.Response(200, json={"id_token": id_token, "token_type": "Bearer"})
        raise AssertionError(f"unexpected OIDC request: {request.method} {request.url}")


def _settings(auth_dsn: str, **overrides) -> Settings:
    values = {
        "deployment_mode": "hosted",
        "hosted_auth_dsn": auth_dsn,
        "hosted_tenant_dsn": "postgresql://unused.invalid/litlet",
        "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": CLIENT_SECRET,
        "oidc_redirect_uri": REDIRECT_URI,
        "trusted_hosts": "reader.example",
        "hosted_credential_master_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "hosted_credential_key_version": "test-v1",
        "session_absolute_ttl_seconds": 3600,
        "session_idle_ttl_seconds": 300,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture()
def auth_app(auth_dsn: str):
    clock = Clock()
    provider = SignedOIDCProvider(clock)
    app = create_app(
        _settings(auth_dsn),
        oidc_http_transport=httpx.MockTransport(provider.handle),
        auth_clock=clock,
        tenant_repository=UnusedTenantRepository(),
        object_storage=UnusedObjectStorage(),
    )
    with TestClient(app, base_url="https://reader.example") as client:
        yield client, provider, clock


def _login(client: TestClient, provider: SignedOIDCProvider, *, return_to: str = "/"):
    response = client.get(
        "/api/auth/login", params={"return_to": return_to}, follow_redirects=False
    )
    assert response.status_code == 302
    query = provider.observe_authorization_url(response.headers["location"])
    assert client.cookies.get(OIDC_COOKIE)
    callback = client.get(
        "/api/auth/callback",
        params={"state": query["state"], "code": "one-time-code"},
        follow_redirects=False,
    )
    return query, callback


def test_auth_role_is_narrow_and_identity_is_stable(auth_dsn: str, database: str) -> None:
    import asyncio

    repository = PostgresAuthRepository(auth_dsn)
    asyncio.run(repository.check_runtime_role())
    with psycopg.connect(database) as conn:
        role = conninfo.conninfo_to_dict(auth_dsn)["user"]
        assert conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone() == (False, True)
        assert not conn.execute(
            "SELECT has_table_privilege(%s, 'books', 'SELECT')", (role,)
        ).fetchone()[0]


def test_auth_role_startup_rejects_a_missing_required_grant(
    auth_dsn: str, database: str
) -> None:
    import asyncio

    role = conninfo.conninfo_to_dict(auth_dsn)["user"]
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute(sql.SQL("REVOKE DELETE ON users FROM {}").format(sql.Identifier(role)))
    try:
        with pytest.raises(AuthConfigurationError, match="restricted"):
            asyncio.run(PostgresAuthRepository(auth_dsn).check_runtime_role())
    finally:
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL("GRANT DELETE ON users TO {}").format(sql.Identifier(role)))


def test_signed_oidc_login_creates_opaque_secure_session_and_csrf(auth_app, database: str) -> None:
    client, provider, _clock = auth_app
    query, response = _login(client, provider, return_to="/library?sort=recent")

    assert response.status_code == 303
    assert response.headers["location"] == "/library?sort=recent"
    cookie_headers = response.headers.get_list("set-cookie")
    session_header = next(item for item in cookie_headers if SESSION_COOKIE in item)
    csrf_header = next(item for item in cookie_headers if CSRF_COOKIE in item)
    assert "HttpOnly" in session_header and "Secure" in session_header
    assert "SameSite=lax" in session_header and "Path=/" in session_header
    assert "HttpOnly" not in csrf_header and "Secure" in csrf_header
    assert "SameSite=lax" in csrf_header

    session = client.get("/api/auth/session")
    assert session.status_code == 200
    first_owner = session.json()["user"]["id"]
    assert session.json()["user"]["email"] == "reader@example.test"
    raw_session = client.cookies.get(SESSION_COOKIE)
    raw_csrf = client.cookies.get(CSRF_COOKIE)
    with psycopg.connect(database) as conn:
        stored = conn.execute(
            "SELECT session_digest, csrf_digest FROM sessions WHERE revoked_at IS NULL"
        ).fetchone()
        assert raw_session.encode() not in stored[0]
        assert raw_csrf.encode() not in stored[1]

    assert client.post("/api/auth/logout").status_code == 403
    assert client.post("/api/auth/logout", headers={CSRF_HEADER: "x" * 43}).status_code == 403
    logout = client.post("/api/auth/logout", headers={CSRF_HEADER: raw_csrf})
    assert logout.status_code == 204
    assert client.get("/api/auth/session").status_code == 401

    # State is one-time even though the provider code was not modeled as consumed.
    replay = client.get(
        "/api/auth/callback", params={"state": query["state"], "code": "one-time-code"}
    )
    assert replay.status_code == 400

    _, second = _login(client, provider)
    assert second.status_code == 303
    assert client.get("/api/auth/session").json()["user"]["id"] == first_owner
    with psycopg.connect(database) as conn:
        events = conn.execute(
            "SELECT actor_kind,action,target_kind,result,occurred_at FROM audit_events "
            "WHERE owner_id=%s ORDER BY occurred_at,id",
            (first_owner,),
        ).fetchall()
    assert [row[1] for row in events] == ["session.login", "session.logout", "session.login"]
    assert all(row[0:1] + row[2:4] == ("owner", "session", "succeeded") for row in events)
    assert all(row[4].tzinfo is not None for row in events)


def test_email_does_not_link_distinct_oidc_subjects(auth_app, database: str) -> None:
    client, provider, _clock = auth_app
    _login(client, provider)
    first = client.get("/api/auth/session").json()["user"]["id"]
    provider.subject = "provider-subject-2"
    _login(client, provider)
    second = client.get("/api/auth/session").json()["user"]["id"]
    assert second != first
    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT count(*) FROM users WHERE email = 'reader@example.test'"
        ).fetchone()[0] == 2


def test_oidc_state_is_bound_to_the_browser_cookie(auth_app) -> None:
    client, provider, _clock = auth_app
    login = client.get("/api/auth/login", follow_redirects=False)
    query = provider.observe_authorization_url(login.headers["location"])
    attacker = TestClient(client.app, base_url="https://reader.example")
    assert attacker.get(
        "/api/auth/callback", params={"state": query["state"], "code": "stolen-code"}
    ).status_code == 400
    assert provider.token_exchanges == 0

    legitimate = client.get(
        "/api/auth/callback", params={"state": query["state"], "code": "legitimate-code"},
        follow_redirects=False,
    )
    assert legitimate.status_code == 303
    assert provider.token_exchanges == 1


def test_nonce_mismatch_is_rejected_and_attempt_cannot_be_replayed(auth_app) -> None:
    client, provider, _clock = auth_app
    login = client.get("/api/auth/login", follow_redirects=False)
    query = provider.observe_authorization_url(login.headers["location"])
    provider.claim_overrides = {"nonce": "attacker-nonce"}
    response = client.get(
        "/api/auth/callback", params={"state": query["state"], "code": "injected-code"}
    )
    assert response.status_code == 400
    assert client.get("/api/auth/session").status_code == 401
    assert client.get(
        "/api/auth/callback", params={"state": query["state"], "code": "injected-code"}
    ).status_code == 400


@pytest.mark.parametrize(
    "claim_overrides",
    [
        {"iss": "https://attacker.example"},
        {"aud": "another-client"},
        {"exp": 0},
        {"iat": 9_999_999_999},
        {"aud": [CLIENT_ID, "another-client"]},  # multi-audience tokens require matching azp
    ],
)
def test_invalid_id_token_claims_are_rejected(auth_app, claim_overrides: dict) -> None:
    client, provider, _clock = auth_app
    provider.claim_overrides = claim_overrides
    _query, response = _login(client, provider)
    assert response.status_code == 400
    assert client.get("/api/auth/session").status_code == 401


def test_invalid_id_token_signature_is_rejected(auth_app) -> None:
    client, provider, _clock = auth_app
    provider.tamper_signature = True
    _query, response = _login(client, provider)
    assert response.status_code == 400
    assert client.get("/api/auth/session").status_code == 401


def test_session_rotation_and_idle_expiry_revoke_access(auth_app) -> None:
    client, provider, clock = auth_app
    _login(client, provider)
    old_session = client.cookies.get(SESSION_COOKIE)
    _login(client, provider)
    old_client = TestClient(client.app, base_url="https://reader.example")
    old_client.cookies.set(SESSION_COOKIE, old_session, domain="reader.example", path="/")
    assert old_client.get("/api/auth/session").status_code == 401

    clock.value += timedelta(seconds=301)
    assert client.get("/api/auth/session").status_code == 401


def test_absolute_expiry_wins_even_when_idle_activity_is_fresh(auth_app) -> None:
    client, provider, clock = auth_app
    _login(client, provider)
    for _ in range(12):
        clock.value += timedelta(seconds=299)
        assert client.get("/api/auth/session").status_code == 200
    clock.value += timedelta(seconds=13)
    assert client.get("/api/auth/session").status_code == 401


def test_hosted_tenant_routes_require_authentication(auth_app) -> None:
    client, _provider, _clock = auth_app
    assert client.get("/api/health/live").json() == {"status": "ok"}
    assert client.get("/api/books").status_code == 401
    assert client.post("/api/books").status_code == 401
