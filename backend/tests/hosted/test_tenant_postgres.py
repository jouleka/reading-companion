"""Real-PostgreSQL adversarial owner-isolation release gate for LIT-48."""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
import httpx
from fastapi.testclient import TestClient
from psycopg import conninfo, sql

from app.config import Settings
from app.hosted.audit import inspect_events, purge_events
from app.hosted.auth.api import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from app.hosted.auth.models import Principal
from app.hosted.auth.repository import AuthConfigurationError
from app.hosted.auth.tokens import digest_token, new_token
from app.hosted.migrations import apply_migrations
from app.hosted.limits import LimitExceededError, inspect_limits, update_limits
from app.hosted.provider_settings import ProviderValidator
from app.hosted.storage import EPUB_MEDIA_TYPE
from app.hosted.tenant.models import MissingTenantResourceError, OwnerId
from app.hosted.tenant.repository import PostgresTenantRepository
from app.main import create_app
from tests.api._epub import epub_ncx

pytestmark = pytest.mark.postgres

INVENTORY = (
    Path(__file__).parents[2] / "app" / "hosted" / "tenant" / "endpoints.json"
)


@pytest.fixture(scope="module")
def admin_dsn() -> str:
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for the real PostgreSQL suite")
    return dsn


@pytest.fixture()
def database(admin_dsn: str):
    database_name = f"lit41_{uuid.uuid4().hex}"
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
def tenant_dsn(database: str, admin_dsn: str):
    role = f"lit41_tenant_{uuid.uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOINHERIT NOBYPASSRLS").format(
                sql.Identifier(role)
            )
        )
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        conn.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE ON books, source_objects, reading_state TO {}; "
                "GRANT SELECT, INSERT, UPDATE ON reader_preferences TO {}; "
                "GRANT SELECT, INSERT, DELETE ON book_search_documents TO {}; "
                "GRANT SELECT, INSERT, UPDATE ON highlights TO {}; "
                "GRANT SELECT, INSERT, UPDATE ON annotations TO {}; "
                "GRANT SELECT, INSERT, UPDATE ON bookmarks TO {}; "
                "GRANT SELECT, INSERT, UPDATE ON jobs TO {}; "
                "GRANT SELECT, INSERT, UPDATE ON provider_credentials TO {}; "
                "GRANT SELECT, INSERT, UPDATE ON provider_model_settings TO {}; "
                "GRANT SELECT ON owner_limits TO {}; "
                "GRANT SELECT, INSERT, UPDATE ON owner_request_windows TO {}; "
                "GRANT SELECT, INSERT, UPDATE ON cost_reservations TO {}; "
                "GRANT SELECT ON chapters, ingested_chapters, chapter_summaries, "
                "events, themes TO {}; "
                "GRANT SELECT, INSERT, UPDATE ON entities TO {}; "
                "GRANT SELECT, INSERT ON aliases, edges, event_participants, entity_state, "
                "entity_corrections TO {}; "
                "GRANT SELECT, INSERT ON cost_ledger TO {}"
            ).format(*(sql.Identifier(role) for _ in range(16)))
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


@pytest.fixture()
def corpus(database: str) -> dict:
    owner_a = uuid.uuid4()
    owner_b = uuid.uuid4()
    book_a = uuid.uuid4()
    book_b = uuid.uuid4()
    incarnation_a = uuid.uuid4()
    incarnation_b = uuid.uuid4()
    chapter_1 = uuid.uuid4()
    chapter_2 = uuid.uuid4()
    entity_1 = uuid.uuid4()
    entity_2 = uuid.uuid4()
    future_entity = uuid.uuid4()
    with psycopg.connect(database) as conn:
        conn.cursor().executemany(
            "INSERT INTO users (id,display_name) VALUES (%s,%s)",
            [(owner_a, "Owner A"), (owner_b, "Owner B")],
        )
        conn.cursor().executemany(
            """
            INSERT INTO books (owner_id,id,incarnation,title,author,schema_version)
            VALUES (%s,%s,%s,%s,%s,1)
            """,
            [
                (owner_a, book_a, incarnation_a, "A private book", "A author"),
                (owner_b, book_b, incarnation_b, "B private book", "B author"),
            ],
        )
        conn.cursor().executemany(
            """
            INSERT INTO reading_state
              (owner_id,book_id,book_incarnation,bookmark,current_cfi,high_water_cfi,position_epoch)
            VALUES (%s,%s,%s,1,%s,%s,0)
            """,
            [
                (owner_a, book_a, incarnation_a, "epubcfi(/6/2)", "epubcfi(/6/2)"),
                (owner_b, book_b, incarnation_b, "epubcfi(/6/4)", "epubcfi(/6/4)"),
            ],
        )
        conn.cursor().executemany(
            """INSERT INTO book_search_documents
                 (owner_id,book_id,book_incarnation,ordinal,href,title,part_label,content,
                  char_start,char_end)
               VALUES (%s,%s,%s,%s,%s,%s,'',%s,%s,%s)""",
            [
                (
                    owner_a, book_a, incarnation_a, 1, "a-1.xhtml", "Chapter 1",
                    "A visible lantern stood beside the private window.", 0, 100,
                ),
                (
                    owner_a, book_a, incarnation_a, 2, "a-2.xhtml", "Chapter 2",
                    "A future comet disclosed the hidden ending.", 100, 200,
                ),
                (
                    owner_b, book_b, incarnation_b, 1, "b-1.xhtml", "Chapter 1",
                    "B kept an owner private compass in the study.", 0, 100,
                ),
            ],
        )
        for ordinal, chapter_id, marker in ((1, chapter_1, "a"), (2, chapter_2, "b")):
            content_hash = marker * 64
            conn.execute(
                """
                INSERT INTO chapters
                  (owner_id,book_id,book_incarnation,id,chapter_key,revealed_at,title,content_hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    owner_a,
                    book_a,
                    incarnation_a,
                    chapter_id,
                    f"ch{ordinal}",
                    ordinal,
                    f"Chapter {ordinal}",
                    content_hash,
                ),
            )
            conn.execute(
                """
                INSERT INTO ingested_chapters
                  (owner_id,book_id,book_incarnation,chapter_id,content_hash,completed_at)
                VALUES (%s,%s,%s,%s,%s,now())
                """,
                (owner_a, book_a, incarnation_a, chapter_id, content_hash),
            )
        for entity_id, chapter_id, name, reveal in (
            (entity_1, chapter_1, "A Visible One", 1),
            (entity_2, chapter_1, "A Visible Two", 1),
            (future_entity, chapter_2, "A Future Secret", 2),
        ):
            conn.execute(
                """
                INSERT INTO entities
                  (owner_id,book_id,book_incarnation,id,source_chapter_id,canonical_name,
                   entity_type,revealed_at)
                VALUES (%s,%s,%s,%s,%s,%s,'character',%s)
                """,
                (owner_a, book_a, incarnation_a, entity_id, chapter_id, name, reveal),
            )
        conn.execute(
            """
            INSERT INTO edges
              (owner_id,book_id,book_incarnation,id,source_chapter_id,src_entity_id,
               dst_entity_id,relationship_type,label,revealed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'knows','A private relationship',1)
            """,
            (owner_a, book_a, incarnation_a, uuid.uuid4(), chapter_1, entity_1, entity_2),
        )
        conn.execute(
            """
            INSERT INTO events
              (owner_id,book_id,book_incarnation,id,source_chapter_id,order_idx,summary,kind,revealed_at)
            VALUES (%s,%s,%s,%s,%s,0,'A private event','meeting',1)
            """,
            (owner_a, book_a, incarnation_a, uuid.uuid4(), chapter_1),
        )
        conn.execute(
            """
            INSERT INTO themes
              (owner_id,book_id,book_incarnation,id,source_chapter_id,name,description,revealed_at)
            VALUES (%s,%s,%s,%s,%s,'A private theme','Only A may read this',1)
            """,
            (owner_a, book_a, incarnation_a, uuid.uuid4(), chapter_1),
        )
        conn.execute(
            """
            INSERT INTO chapter_summaries
              (owner_id,book_id,book_incarnation,id,source_chapter_id,kind,summary,revealed_at)
            VALUES (%s,%s,%s,%s,%s,'chapter','A private summary',1)
            """,
            (owner_a, book_a, incarnation_a, uuid.uuid4(), chapter_1),
        )
        conn.cursor().executemany(
            """
            INSERT INTO cost_ledger
              (owner_id,id,book_id,book_incarnation,phase,provider,model,input_tokens,
               output_tokens,usd,idempotency_key)
            VALUES (%s,%s,%s,%s,'extraction',%s,'model',10,2,0.01,%s)
            """,
            [
                (owner_a, uuid.uuid4(), book_a, incarnation_a, "provider-a", "cost-a"),
                (owner_b, uuid.uuid4(), book_b, incarnation_b, "provider-b", "cost-b"),
            ],
        )
    return {
        "owner_a": owner_a,
        "owner_b": owner_b,
        "book_a": book_a,
        "book_b": book_b,
        "incarnation_a": incarnation_a,
        "incarnation_b": incarnation_b,
        "entity_1": entity_1,
        "entity_2": entity_2,
        "future_entity": future_entity,
    }


class SessionRepository:
    def __init__(self, sessions: dict[bytes, Principal]) -> None:
        self.sessions = sessions

    async def check_runtime_role(self) -> None:
        return None

    async def authenticate_session(self, *, session_digest, now, idle_ttl):
        return self.sessions.get(session_digest)


class UnusedOIDCClient:
    pass


@pytest.fixture()
def tenant_app(tenant_dsn: str, corpus: dict, tmp_path):
    tokens = {}
    sessions = {}
    expires = datetime(2026, 7, 17, tzinfo=UTC)
    for label in ("a", "b"):
        session_token = new_token()
        csrf_token = new_token()
        tokens[label] = (session_token, csrf_token)
        sessions[digest_token(session_token)] = Principal(
            owner_id=corpus[f"owner_{label}"],
            session_id=uuid.uuid4(),
            display_name=f"Owner {label.upper()}",
            email=None,
            csrf_digest=digest_token(csrf_token),
            expires_at=expires,
        )
    settings = Settings(
        _env_file=None,
        deployment_mode="hosted",
        hosted_auth_dsn="postgresql://unused.invalid/litlet",
        hosted_tenant_dsn=tenant_dsn,
        oidc_issuer="https://idp.example",
        oidc_client_id="litlet",
        oidc_client_secret="not-a-real-secret",
        oidc_redirect_uri="https://reader.example/api/auth/callback",
        trusted_hosts="reader.example",
        hosted_storage_backend="filesystem",
        hosted_storage_filesystem_root=str(tmp_path / "objects"),
        hosted_storage_filesystem_key=base64.b64encode(b"t" * 32).decode("ascii"),
        hosted_credential_master_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        hosted_credential_key_version="test-v1",
        hosted_provider_allowed_origins=(
            "https://api.openai.com,https://api.anthropic.com,https://network.example"
        ),
    )

    def provider_response(request: httpx.Request) -> httpx.Response:
        if request.url.host == "network.example":
            raise httpx.ConnectError("synthetic network failure", request=request)
        authorization = request.headers.get("authorization")
        if authorization == "Bearer invalid-owner-key":
            return httpx.Response(401, json={"error": "private provider detail"})
        return httpx.Response(
            200,
            json={"data": [{"id": "available-model"}, {"id": "text-embedding-3-small"}]},
        )

    app = create_app(
        settings,
        auth_repository=SessionRepository(sessions),
        oidc_client=UnusedOIDCClient(),
        auth_clock=lambda: datetime(2026, 7, 16, 12, tzinfo=UTC),
        provider_validator=ProviderValidator(transport=httpx.MockTransport(provider_response)),
    )
    with TestClient(app, base_url="https://reader.example") as client:
        yield client, tokens, app


def _become(client: TestClient, tokens: dict, label: str) -> str:
    client.cookies.clear()
    session_token, csrf_token = tokens[label]
    client.cookies.set(SESSION_COOKIE, session_token, domain="reader.example", path="/")
    client.cookies.set(CSRF_COOKIE, csrf_token, domain="reader.example", path="/")
    return csrf_token


def test_inventory_exactly_matches_every_enabled_tenant_route(tenant_app) -> None:
    _client, _tokens, app = tenant_app
    schema = app.openapi()
    expected = {
        (item["method"], item["path"])
        for item in json.loads(INVENTORY.read_text(encoding="utf-8"))["enabled"]
    }
    actual = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        if "hosted-library" in operation.get("tags", [])
    }
    assert actual == expected
    for operations in schema["paths"].values():
        for operation in operations.values():
            if "hosted-library" not in operation.get("tags", []):
                continue
            parameter_names = {
                item["name"].casefold().replace("_", "").replace("-", "")
                for item in operation.get("parameters", [])
            }
            assert not (
                {
                    "owner",
                    "ownerid",
                    "userid",
                    "objectid",
                    "objectkey",
                    "storagekey",
                }
                & parameter_names
            )


def test_every_enabled_tenant_endpoint_requires_a_session(tenant_app, corpus: dict) -> None:
    client, _tokens, _app = tenant_app
    book = corpus["book_a"]
    requests = [
        ("GET", "/api/limits", None),
        ("GET", "/api/provider-settings", None),
        ("GET", f"/api/books/{book}/preferences", None),
        ("GET", f"/api/books/{book}/manifest", None),
        ("GET", f"/api/books/{book}/search?q=lantern", None),
        ("GET", f"/api/books/{book}/marks", None),
        ("GET", f"/api/books/{book}/marks/export", None),
        (
            "POST", f"/api/books/{book}/highlights",
            {"anchor": {"cfi": "epubcfi(/6/2)", "atom": 1}, "selected_text": "text"},
        ),
        (
            "POST", f"/api/books/{book}/annotations",
            {"anchor": {"cfi": "epubcfi(/6/2)", "atom": 1}, "body": "note"},
        ),
        (
            "POST", f"/api/books/{book}/bookmarks",
            {"anchor": {"cfi": "epubcfi(/6/2)", "atom": 1}, "label": "place"},
        ),
        ("PATCH", f"/api/books/{book}/highlights/{uuid.uuid4()}", {"color": "blue"}),
        ("PATCH", f"/api/books/{book}/annotations/{uuid.uuid4()}", {"body": "note"}),
        ("PATCH", f"/api/books/{book}/bookmarks/{uuid.uuid4()}", {"label": "place"}),
        ("DELETE", f"/api/books/{book}/highlights/{uuid.uuid4()}", None),
        ("DELETE", f"/api/books/{book}/annotations/{uuid.uuid4()}", None),
        ("DELETE", f"/api/books/{book}/bookmarks/{uuid.uuid4()}", None),
        (
            "PUT",
            f"/api/books/{book}/preferences",
            {
                "font_size": "book",
                "line_height": "comfortable",
                "measure": "balanced",
                "theme": "paper",
                "margins": "balanced",
                "typeface": "publisher",
            },
        ),
        ("PUT", "/api/provider-settings/extraction", {"provider": "offline", "model": "offline"}),
        ("POST", "/api/provider-settings/extraction/validate", None),
        ("POST", "/api/credentials", {"provider": "anthropic", "secret": "secret"}),
        ("GET", "/api/credentials", None),
        ("PUT", f"/api/credentials/{uuid.uuid4()}", {"secret": "replacement"}),
        ("DELETE", f"/api/credentials/{uuid.uuid4()}", None),
        ("GET", "/api/books", None),
        ("POST", "/api/books", None),
        ("GET", f"/api/books/{book}", None),
        ("GET", "/api/jobs", None),
        ("GET", f"/api/jobs/{uuid.uuid4()}", None),
        ("POST", f"/api/jobs/{uuid.uuid4()}/cancel", None),
        ("GET", f"/api/books/{book}/epub", None),
        ("DELETE", f"/api/books/{book}", None),
        ("GET", f"/api/books/{book}/position", None),
        (
            "PUT",
            f"/api/books/{book}/position",
            {
                "cfi": "epubcfi(/6/2)",
                "offset": 1,
                "completed_chapter": 0,
                "position_epoch": 0,
                "base_version": 0,
                "client_id": str(uuid.uuid4()),
                "client_sequence": 1,
            },
        ),
        ("POST", f"/api/books/{book}/position/reset", {"position_epoch": 0}),
        ("GET", f"/api/books/{book}/memory", None),
        ("GET", f"/api/books/{book}/memory-corrections", None),
        (
            "POST", f"/api/books/{book}/memory-corrections",
            {
                "source_entity_id": str(uuid.uuid4()),
                "canonical_name": "Corrected name",
                "reason": "Reader correction",
                "bookmark": 1,
            },
        ),
        ("GET", "/api/costs", None),
    ]
    for method, path, body in requests:
        response = client.request(method, path, json=body)
        assert response.status_code == 401, (method, path, response.text)


def test_foreign_book_is_hidden_from_every_identifier_endpoint(
    tenant_app, corpus: dict, database: str
) -> None:
    client, tokens, _app = tenant_app
    csrf = _become(client, tokens, "b")
    foreign = corpus["book_a"]
    missing = uuid.uuid4()
    for suffix in (
        "", "/position", "/memory", "/memory-corrections", "/epub", "/manifest", "/search?q=lantern",
        "/marks", "/marks/export",
    ):
        hidden = client.get(f"/api/books/{foreign}{suffix}")
        absent = client.get(f"/api/books/{missing}{suffix}")
        assert (hidden.status_code, hidden.json()) == (absent.status_code, absent.json())
        assert hidden.status_code == 404
        assert hidden.headers["cache-control"] == "private, no-store"

    hidden_reset = client.post(
        f"/api/books/{foreign}/position/reset",
        json={"position_epoch": 0},
        headers={CSRF_HEADER: csrf},
    )
    absent_reset = client.post(
        f"/api/books/{missing}/position/reset",
        json={"position_epoch": 0},
        headers={CSRF_HEADER: csrf},
    )
    assert (hidden_reset.status_code, hidden_reset.json()) == (
        absent_reset.status_code,
        absent_reset.json(),
    )
    assert hidden_reset.status_code == 404
    update_body = {
        "cfi": "epubcfi(/6/2)",
        "offset": 10,
        "completed_chapter": 1,
        "position_epoch": 0,
        "base_version": 0,
        "client_id": str(uuid.uuid4()),
        "client_sequence": 1,
    }
    hidden_update = client.put(
        f"/api/books/{foreign}/position", json=update_body, headers={CSRF_HEADER: csrf}
    )
    absent_update = client.put(
        f"/api/books/{missing}/position", json=update_body, headers={CSRF_HEADER: csrf}
    )
    assert (hidden_update.status_code, hidden_update.json()) == (
        absent_update.status_code,
        absent_update.json(),
    )
    assert hidden_update.status_code == 404
    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT bookmark,position_epoch FROM reading_state WHERE owner_id=%s AND book_id=%s",
            (corpus["owner_a"], foreign),
        ).fetchone() == (1, 0)


def test_reader_search_is_owner_scoped_spoiler_bounded_and_deletion_aware(
    tenant_app, corpus: dict, database: str
) -> None:
    client, tokens, _app = tenant_app
    _become(client, tokens, "a")
    path = f"/api/books/{corpus['book_a']}/search"
    visible = client.get(path, params={"q": "visible lantern"})
    assert visible.status_code == 200
    assert visible.headers["cache-control"] == "private, no-store"
    assert visible.json()["as_of_chapter"] == 1
    assert len(visible.json()["hits"]) == 1
    assert "lantern" in visible.json()["hits"][0]["snippet"].casefold()
    assert client.get(path, params={"q": "future comet"}).json()["hits"] == []
    manifest = client.get(f"/api/books/{corpus['book_a']}/manifest").json()
    assert [atom["title"] for atom in manifest["atoms"]] == ["Chapter 1", "Chapter 2"]
    assert [atom["char_len"] for atom in manifest["atoms"]] == [100, 100]

    _become(client, tokens, "b")
    hidden = client.get(path, params={"q": "lantern"})
    missing = client.get(
        f"/api/books/{uuid.uuid4()}/search", params={"q": "lantern"}
    )
    assert (hidden.status_code, hidden.json()) == (missing.status_code, missing.json())
    assert hidden.status_code == 404
    own = client.get(
        f"/api/books/{corpus['book_b']}/search", params={"q": "private compass"}
    )
    assert len(own.json()["hits"]) == 1
    assert "lantern" not in own.text

    repository = PostgresTenantRepository(database)
    assert asyncio.run(
        repository.soft_delete_book(OwnerId(corpus["owner_a"]), corpus["book_a"])
    )
    assert asyncio.run(
        repository.search_book(
            OwnerId(corpus["owner_a"]), corpus["book_a"], "lantern", limit=20
        )
    ) is None
    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT count(*) FROM book_search_documents WHERE owner_id=%s AND book_id=%s",
            (corpus["owner_a"], corpus["book_a"]),
        ).fetchone()[0] == 0


def test_reader_marks_are_owner_scoped_frontier_bounded_and_exportable(
    tenant_app, corpus: dict, database: str
) -> None:
    client, tokens, _app = tenant_app
    book = corpus["book_a"]
    csrf_a = _become(client, tokens, "a")
    anchor = {
        "cfi": "epubcfi(/6/2!/4/2,/1:0,/1:8)",
        "atom": 1,
        "quote": {"exact": "lantern", "prefix": "visible ", "suffix": " stood"},
    }
    highlight = client.post(
        f"/api/books/{book}/highlights",
        json={"anchor": anchor, "color": "yellow", "selected_text": "lantern"},
        headers={CSRF_HEADER: csrf_a},
    )
    assert highlight.status_code == 201
    highlight_id = highlight.json()["id"]
    note = client.post(
        f"/api/books/{book}/annotations",
        json={"anchor": anchor, "body": "Remember this image.", "highlight_id": highlight_id},
        headers={CSRF_HEADER: csrf_a},
    )
    assert note.status_code == 201
    note_id = note.json()["id"]
    bookmark = client.post(
        f"/api/books/{book}/bookmarks",
        json={
            "anchor": {"cfi": "epubcfi(/6/4)", "atom": 2},
            "label": "Current chapter",
        },
        headers={CSRF_HEADER: csrf_a},
    )
    assert bookmark.status_code == 201
    bookmark_id = bookmark.json()["id"]
    assert client.post(
        f"/api/books/{book}/bookmarks",
        json={"anchor": {"cfi": "epubcfi(/6/6)", "atom": 3}, "label": "future"},
        headers={CSRF_HEADER: csrf_a},
    ).status_code == 409

    changed = client.patch(
        f"/api/books/{book}/annotations/{note_id}",
        json={"body": "Updated note."},
        headers={CSRF_HEADER: csrf_a},
    )
    assert changed.status_code == 200
    assert changed.json()["body"] == "Updated note."
    assert changed.json()["version"] == 2
    marks = client.get(f"/api/books/{book}/marks")
    assert marks.status_code == 200
    assert marks.headers["cache-control"] == "private, no-store"
    assert {mark["kind"] for mark in marks.json()["marks"]} == {
        "highlight", "annotation", "bookmark"
    }
    exported = client.get(f"/api/books/{book}/marks/export")
    assert exported.status_code == 200
    assert exported.headers["content-disposition"].startswith("attachment;")
    assert exported.json()["format"] == "litlet-reader-marks"
    assert len(exported.json()["marks"]) == 3

    csrf_b = _become(client, tokens, "b")
    hidden = client.get(f"/api/books/{book}/marks")
    missing = client.get(f"/api/books/{uuid.uuid4()}/marks")
    assert (hidden.status_code, hidden.json()) == (missing.status_code, missing.json())
    foreign_update = client.patch(
        f"/api/books/{book}/bookmarks/{bookmark_id}",
        json={"label": "stolen"},
        headers={CSRF_HEADER: csrf_b},
    )
    assert foreign_update.status_code == 404

    csrf_a = _become(client, tokens, "a")
    reset = client.post(
        f"/api/books/{book}/position/reset",
        json={"position_epoch": 0},
        headers={CSRF_HEADER: csrf_a},
    )
    assert reset.status_code == 200
    after_reset = client.get(f"/api/books/{book}/marks").json()["marks"]
    assert {mark["kind"] for mark in after_reset} == {"highlight", "annotation"}
    assert client.delete(
        f"/api/books/{book}/highlights/{highlight_id}",
        headers={CSRF_HEADER: csrf_a},
    ).status_code == 204
    repository = PostgresTenantRepository(database)
    assert asyncio.run(repository.soft_delete_book(OwnerId(corpus["owner_a"]), book))
    with psycopg.connect(database) as conn:
        for table in ("highlights", "annotations", "bookmarks"):
            assert conn.execute(
                sql.SQL(
                    "SELECT count(*) FROM {} WHERE owner_id=%s AND book_id=%s "
                    "AND deleted_at IS NULL"
                ).format(sql.Identifier(table)),
                (corpus["owner_a"], book),
            ).fetchone()[0] == 0


def test_list_and_cost_surfaces_never_include_another_owner(tenant_app, corpus: dict) -> None:
    client, tokens, _app = tenant_app
    _become(client, tokens, "b")
    books = client.get("/api/books")
    assert books.status_code == 200
    assert [item["id"] for item in books.json()] == [str(corpus["book_b"])]
    costs = client.get("/api/costs")
    assert costs.status_code == 200
    assert [item["provider"] for item in costs.json()["items"]] == ["provider-b"]
    assert client.get("/api/costs", params={"book_id": str(corpus["book_a"])}).status_code == 404


def test_hosted_credentials_are_owner_scoped_and_metadata_only(
    tenant_app, corpus: dict, database: str
) -> None:
    client, tokens, _app = tenant_app
    original = "sk-owner-a-private-canary-7H3k"
    csrf_a = _become(client, tokens, "a")
    assert client.post(
        "/api/credentials",
        json={"provider": "anthropic", "secret": original},
    ).status_code == 403
    created = client.post(
        "/api/credentials",
        json={"provider": "Anthropic", "secret": original},
        headers={CSRF_HEADER: csrf_a},
    )
    assert created.status_code == 201
    metadata = created.json()
    credential_id = uuid.UUID(metadata["id"])
    assert metadata["provider"] == "anthropic"
    assert metadata["masked_label"].endswith("7H3k")
    assert not ({"ciphertext", "encrypted_data_key", "nonce", "secret"} & metadata.keys())
    assert original not in created.text

    listed = client.get("/api/credentials")
    assert listed.json() == [metadata]
    assert original not in listed.text
    with psycopg.connect(database) as conn:
        stored = conn.execute(
            """
            SELECT ciphertext,encrypted_data_key,nonce,key_version
            FROM provider_credentials WHERE owner_id=%s AND id=%s
            """,
            (corpus["owner_a"], credential_id),
        ).fetchone()
    assert original.encode() not in bytes(stored[0]) + bytes(stored[1]) + bytes(stored[2])
    assert len(stored[1]) == 60 and len(stored[2]) == 12 and stored[3] == "test-v1"

    csrf_b = _become(client, tokens, "b")
    assert client.get("/api/credentials").json() == []
    missing = uuid.uuid4()
    hidden_replace = client.put(
        f"/api/credentials/{credential_id}",
        json={"secret": "owner-b-attack"},
        headers={CSRF_HEADER: csrf_b},
    )
    absent_replace = client.put(
        f"/api/credentials/{missing}",
        json={"secret": "owner-b-attack"},
        headers={CSRF_HEADER: csrf_b},
    )
    assert (hidden_replace.status_code, hidden_replace.json()) == (
        absent_replace.status_code,
        absent_replace.json(),
    ) == (404, {"detail": "unknown credential"})
    hidden_delete = client.delete(
        f"/api/credentials/{credential_id}", headers={CSRF_HEADER: csrf_b}
    )
    absent_delete = client.delete(
        f"/api/credentials/{missing}", headers={CSRF_HEADER: csrf_b}
    )
    assert (hidden_delete.status_code, hidden_delete.json()) == (
        absent_delete.status_code,
        absent_delete.json(),
    ) == (404, {"detail": "unknown credential"})

    replacement = "sk-owner-a-replacement-9Zq2"
    csrf_a = _become(client, tokens, "a")
    rotated = client.put(
        f"/api/credentials/{credential_id}",
        json={"secret": replacement},
        headers={CSRF_HEADER: csrf_a},
    )
    assert rotated.status_code == 200
    assert rotated.json()["rotated_at"] is not None
    assert replacement not in rotated.text
    invalid = client.put(
        f"/api/credentials/{credential_id}",
        json={"secret": "leaked-on-error\n"},
        headers={CSRF_HEADER: csrf_a},
    )
    assert invalid.status_code == 422 and "leaked-on-error" not in invalid.text
    duplicate = client.put(
        f"/api/credentials/{credential_id}",
        content='{"secret":"duplicate-canary","secret":"second"}',
        headers={CSRF_HEADER: csrf_a, "Content-Type": "application/json"},
    )
    assert duplicate.status_code == 422 and "duplicate-canary" not in duplicate.text
    oversized = client.put(
        f"/api/credentials/{credential_id}",
        content=b"x" * (32 * 1024 + 1),
        headers={CSRF_HEADER: csrf_a, "Content-Type": "application/json"},
    )
    assert oversized.status_code == 413 and "x" * 100 not in oversized.text

    deleted = client.delete(
        f"/api/credentials/{credential_id}", headers={CSRF_HEADER: csrf_a}
    )
    assert deleted.status_code == 204
    assert client.get("/api/credentials").json() == []
    with psycopg.connect(database) as conn:
        destroyed = conn.execute(
            """
            SELECT ciphertext,encrypted_data_key,disabled_at,deleted_at
            FROM provider_credentials WHERE owner_id=%s AND id=%s
            """,
            (corpus["owner_a"], credential_id),
        ).fetchone()
    assert bytes(destroyed[0]) == b"\x00" and bytes(destroyed[1]) == b"\x00"
    assert destroyed[2] is not None and destroyed[3] is not None


def test_provider_settings_and_validation_are_owner_scoped(
    tenant_app, corpus: dict, database: str
) -> None:
    client, tokens, _app = tenant_app
    csrf_a = _become(client, tokens, "a")
    initial = client.get("/api/provider-settings")
    assert initial.status_code == 200
    assert initial.json()["items"] == []
    assert initial.json()["recommendations_persisted"] is False
    assert "offline" in initial.json()["offline_behavior"].casefold()
    assert "billed" in initial.json()["cost_ownership"].casefold()

    valid_secret = "valid-owner-key"
    invalid_secret = "invalid-owner-key"
    valid = client.post(
        "/api/credentials",
        json={"provider": "openai-compatible", "secret": valid_secret},
        headers={CSRF_HEADER: csrf_a},
    ).json()
    invalid = client.post(
        "/api/credentials",
        json={"provider": "openai-compatible", "secret": invalid_secret},
        headers={CSRF_HEADER: csrf_a},
    ).json()
    with psycopg.connect(database) as conn:
        waiting_job = uuid.uuid4()
        conn.execute(
            """
            INSERT INTO jobs
              (owner_id,id,book_id,book_incarnation,kind,state,idempotency_key,payload_metadata)
            VALUES (%s,%s,%s,%s,'ingest_book','waiting_configuration',%s,
                    '{"chapter_count":1}'::jsonb)
            """,
            (
                corpus["owner_a"],
                waiting_job,
                corpus["book_a"],
                corpus["incarnation_a"],
                f"ingest:{waiting_job.hex}",
            ),
        )

    extraction_payload = {
        "provider": "openai-compatible",
        "model": "available-model",
        "credential_id": valid["id"],
        "base_url": "https://api.openai.com/v1",
    }
    assert client.put(
        "/api/provider-settings/extraction", json=extraction_payload
    ).status_code == 403
    saved = client.put(
        "/api/provider-settings/extraction",
        json=extraction_payload,
        headers={CSRF_HEADER: csrf_a},
    )
    assert saved.status_code == 200 and saved.json()["validation_status"] == "unchecked"

    csrf_b = _become(client, tokens, "b")
    assert client.get("/api/provider-settings").json()["items"] == []
    assert client.post(
        "/api/provider-settings/extraction/validate", headers={CSRF_HEADER: csrf_b}
    ).status_code == 404
    foreign = client.put(
        "/api/provider-settings/extraction",
        json=extraction_payload,
        headers={CSRF_HEADER: csrf_b},
    )
    missing_payload = {**extraction_payload, "credential_id": str(uuid.uuid4())}
    absent = client.put(
        "/api/provider-settings/extraction",
        json=missing_payload,
        headers={CSRF_HEADER: csrf_b},
    )
    assert (foreign.status_code, foreign.json()) == (absent.status_code, absent.json()) == (
        422,
        {"detail": "selected credential is unavailable"},
    )

    csrf_a = _become(client, tokens, "a")
    ready = client.post(
        "/api/provider-settings/extraction/validate", headers={CSRF_HEADER: csrf_a}
    )
    assert ready.status_code == 200 and ready.json()["code"] == "ok"
    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT state,credential_id FROM jobs WHERE owner_id=%s AND id=%s",
            (corpus["owner_a"], waiting_job),
        ).fetchone() == ("pending", uuid.UUID(valid["id"]))

    for capability, credential_id, model, base_url, expected in (
        ("synthesis", invalid["id"], "available-model", "https://api.openai.com/v1", "invalid_credentials"),
        ("judge", valid["id"], "missing-model", "https://api.openai.com/v1", "unavailable_model"),
        ("embedding", valid["id"], "available-model", "https://network.example/v1", "network_error"),
    ):
        configured = client.put(
            f"/api/provider-settings/{capability}",
            json={
                "provider": "openai-compatible",
                "model": model,
                "credential_id": credential_id,
                "base_url": base_url,
            },
            headers={CSRF_HEADER: csrf_a},
        )
        assert configured.status_code == 200
        checked = client.post(
            f"/api/provider-settings/{capability}/validate", headers={CSRF_HEADER: csrf_a}
        )
        assert checked.status_code == 200 and checked.json()["code"] == expected
        assert valid_secret not in checked.text and invalid_secret not in checked.text

    offline = client.put(
        "/api/provider-settings/embedding",
        json={"provider": "offline", "model": "offline", "credential_id": None},
        headers={CSRF_HEADER: csrf_a},
    )
    assert offline.status_code == 200 and offline.json()["validation_status"] == "offline"
    offline_check = client.post(
        "/api/provider-settings/embedding/validate", headers={CSRF_HEADER: csrf_a}
    )
    assert offline_check.json()["code"] == "offline"

    final = client.get("/api/provider-settings").json()
    extraction = next(item for item in final["items"] if item["capability"] == "extraction")
    assert extraction["model"] == "available-model"
    assert extraction["credential_id"] == valid["id"]
    assert final["recommendations"]["extraction"]["model"] == "gpt-4o-mini"
    assert final["recommendations_persisted"] is False


def test_reader_preferences_are_owner_scoped_and_validated(
    tenant_app, corpus: dict, database: str
) -> None:
    client, tokens, _app = tenant_app
    csrf_a = _become(client, tokens, "a")
    defaults = {
        "font_size": "book",
        "line_height": "comfortable",
        "measure": "balanced",
        "theme": "paper",
        "margins": "balanced",
        "typeface": "publisher",
        "preference_version": 0,
    }
    initial = client.get(f"/api/books/{corpus['book_a']}/preferences")
    assert initial.status_code == 200
    assert initial.headers["cache-control"] == "private, no-store"
    assert initial.json() == defaults

    owner_a = {
        "font_size": "large",
        "line_height": "relaxed",
        "measure": "narrow",
        "theme": "night",
        "margins": "generous",
        "typeface": "sans",
    }
    path_a = f"/api/books/{corpus['book_a']}/preferences"
    assert client.put(path_a, json=owner_a).status_code == 403
    invalid = client.put(
        path_a,
        json={**owner_a, "theme": "low-contrast"},
        headers={CSRF_HEADER: csrf_a},
    )
    assert invalid.status_code == 422
    saved_a = client.put(
        path_a, json=owner_a, headers={CSRF_HEADER: csrf_a}
    )
    assert saved_a.status_code == 200
    assert saved_a.json() == {**owner_a, "preference_version": 1}

    csrf_b = _become(client, tokens, "b")
    hidden = client.get(path_a)
    missing = client.get(f"/api/books/{uuid.uuid4()}/preferences")
    assert (hidden.status_code, hidden.json()) == (missing.status_code, missing.json())
    assert hidden.status_code == 404
    hidden_write = client.put(path_a, json=owner_a, headers={CSRF_HEADER: csrf_b})
    missing_write = client.put(
        f"/api/books/{uuid.uuid4()}/preferences",
        json=owner_a,
        headers={CSRF_HEADER: csrf_b},
    )
    assert (hidden_write.status_code, hidden_write.json()) == (
        missing_write.status_code,
        missing_write.json(),
    )
    assert hidden_write.status_code == 404
    path_b = f"/api/books/{corpus['book_b']}/preferences"
    assert client.get(path_b).json() == defaults
    owner_b = {**owner_a, "font_size": "small", "theme": "sepia"}
    saved_b = client.put(
        path_b, json=owner_b, headers={CSRF_HEADER: csrf_b}
    )
    assert saved_b.json() == {**owner_b, "preference_version": 1}

    _become(client, tokens, "a")
    assert client.get(path_a).json() == {
        **owner_a,
        "preference_version": 1,
    }
    with psycopg.connect(database) as conn:
        rows = conn.execute(
            "SELECT owner_id,theme,font_size FROM reader_preferences ORDER BY owner_id"
        ).fetchall()
    assert set(rows) == {
        (corpus["owner_a"], "night", "large"),
        (corpus["owner_b"], "sepia", "small"),
    }


def test_owner_limits_are_visible_atomic_and_owner_scoped(
    tenant_app, corpus: dict, database: str
) -> None:
    client, tokens, _app = tenant_app
    _become(client, tokens, "a")
    visible = client.get("/api/limits")
    assert visible.status_code == 200
    policy = visible.json()
    assert policy["max_upload_bytes"] == 128 * 1024 * 1024
    assert policy["max_library_bytes"] == 5 * 1024 * 1024 * 1024
    assert policy["max_books"] == 100
    assert policy["max_active_jobs"] == 3
    assert policy["max_provider_concurrency"] == 2
    assert policy["max_spend_usd"] is None
    assert not ({"owner_id", "title", "credential_id"} & set(policy))

    updated = update_limits(database, corpus["owner_a"], {"requests_per_window": 2})
    assert updated is not None and updated["requests_per_window"] == 2
    with pytest.raises(psycopg.errors.CheckViolation):
        update_limits(database, corpus["owner_a"], {"max_books": 0})
    with psycopg.connect(database) as conn:
        conn.execute(
            "DELETE FROM owner_request_windows WHERE owner_id=%s", (corpus["owner_a"],)
        )
    assert client.get("/api/limits").status_code == 200
    assert client.get("/api/books").status_code == 200
    limited = client.get("/api/books")
    assert limited.status_code == 429
    assert limited.headers["retry-after"]
    assert limited.json()["detail"]["code"] == "request_rate_exceeded"
    assert limited.json()["detail"]["retry_after_seconds"] >= 1

    _become(client, tokens, "b")
    owner_b_policy = client.get("/api/limits")
    assert owner_b_policy.status_code == 200
    assert owner_b_policy.json()["requests_per_window"] == 600
    assert owner_b_policy.json()["max_upload_bytes"] == 128 * 1024 * 1024
    listed = inspect_limits(database, corpus["owner_a"])
    assert len(listed) == 1 and listed[0]["owner_id"] == str(corpus["owner_a"])
    assert not ({"display_name", "email", "title", "ciphertext"} & set(listed[0]))

    update_limits(
        database,
        corpus["owner_a"],
        {"requests_per_window": 600, "max_upload_bytes": 1},
    )
    with psycopg.connect(database) as conn:
        conn.execute(
            "DELETE FROM owner_request_windows WHERE owner_id=%s", (corpus["owner_a"],)
        )
    csrf_a = _become(client, tokens, "a")
    payload = epub_ncx(
        [("chapter.xhtml", "Chapter I", "Chapter I", "A bounded chapter.")],
        title="Too large for this owner",
    )
    rejected = client.post(
        "/api/books",
        files={"file": ("bounded.epub", payload, EPUB_MEDIA_TYPE)},
        headers={CSRF_HEADER: csrf_a},
    )
    assert rejected.status_code == 413
    assert rejected.json()["detail"]["code"] == "upload_size_exceeded"
    assert "smaller EPUB" in rejected.json()["detail"]["action"]
    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT count(*) FROM source_objects WHERE owner_id=%s",
            (corpus["owner_a"],),
        ).fetchone()[0] == 0

    # The policy row lock serializes two application instances: with one existing live book and a
    # max of two, exactly one of these simultaneous creates may commit.
    update_limits(
        database,
        corpus["owner_a"],
        {
            "requests_per_window": 600,
            "max_books": 2,
            "max_active_jobs": 10,
            "max_library_bytes": 1_000_000,
            "max_upload_bytes": 100_000,
        },
    )
    repository = PostgresTenantRepository(database)

    def create(index: int):
        book_id = uuid.uuid4()
        incarnation = uuid.uuid4()
        return asyncio.run(
            repository.create_uploaded_book(
                OwnerId(corpus["owner_a"]),
                book_id=book_id,
                incarnation=incarnation,
                title=f"Atomic {index}",
                author=None,
                file_hash=f"{index + 1:064x}",
                content_language="en",
                book_type="novel",
                object_id=uuid.uuid4(),
                storage_provider="filesystem",
                media_type=EPUB_MEDIA_TYPE,
                byte_size=100,
                encryption="AES-256-GCM",
                chapter_count=1,
            )
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create, index) for index in range(2)]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except LimitExceededError as exc:
            outcomes.append(exc.code)
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert outcomes.count("book_quota_exceeded") == 1

    update_limits(database, corpus["owner_a"], {"max_books": 10, "max_upload_bytes": 99})
    with pytest.raises(LimitExceededError, match="upload_size_exceeded"):
        create(3)
    update_limits(
        database,
        corpus["owner_a"],
        {"max_upload_bytes": 100_000, "max_library_bytes": 100},
    )
    with pytest.raises(LimitExceededError, match="library_storage_exceeded"):
        create(4)
    update_limits(
        database,
        corpus["owner_a"],
        {"max_library_bytes": 1_000_000, "max_active_jobs": 1},
    )
    with pytest.raises(LimitExceededError, match="active_job_limit_exceeded"):
        create(5)


def test_own_memory_is_bookmark_bounded_and_reset_is_csrf_epoch_bound(
    tenant_app, corpus: dict
) -> None:
    client, tokens, _app = tenant_app
    csrf = _become(client, tokens, "a")
    snapshot = client.get(f"/api/books/{corpus['book_a']}/memory")
    assert snapshot.status_code == 200
    assert snapshot.headers["cache-control"] == "private, no-store"
    text = snapshot.text
    assert "A Visible One" in text and "A private summary" in text
    assert "A Future Secret" not in text
    position = client.get(f"/api/books/{corpus['book_a']}/position")
    assert position.json()["receipt_count"] == 1
    assert client.post(
        f"/api/books/{corpus['book_a']}/position/reset", json={"position_epoch": 0}
    ).status_code == 403
    reset = client.post(
        f"/api/books/{corpus['book_a']}/position/reset",
        json={"position_epoch": 0},
        headers={CSRF_HEADER: csrf},
    )
    assert reset.status_code == 200
    assert reset.json()["bookmark"] == 0
    assert reset.json()["cfi"] is None
    assert reset.json()["position_epoch"] == 1
    assert client.post(
        f"/api/books/{corpus['book_a']}/position/reset",
        json={"position_epoch": 0},
        headers={CSRF_HEADER: csrf},
    ).status_code == 409


def test_reader_memory_corrections_are_owner_scoped_and_frontier_bounded(
    tenant_app, corpus: dict, database: str
) -> None:
    client, tokens, _app = tenant_app
    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE reading_state SET bookmark=2 WHERE owner_id=%s AND book_id=%s",
            (corpus["owner_a"], corpus["book_a"]),
        )
    csrf_a = _become(client, tokens, "a")
    body = {
        "source_entity_id": str(corpus["entity_1"]),
        "canonical_name": "A Visible One Corrected",
        "reason": "The completed text establishes the longer name.",
        "bookmark": 2,
    }
    assert client.post(
        f"/api/books/{corpus['book_a']}/memory-corrections", json=body
    ).status_code == 403
    corrected = client.post(
        f"/api/books/{corpus['book_a']}/memory-corrections",
        json=body,
        headers={CSRF_HEADER: csrf_a},
    )
    assert corrected.status_code == 200
    item = corrected.json()["items"][-1]
    assert item["source_entities"] == [{
        "entity_id": str(corpus["entity_1"]), "name": "A Visible One"
    }]
    assert item["target_entities"][0]["name"] == "A Visible One Corrected"
    assert client.get(
        f"/api/books/{corpus['book_a']}/memory-corrections?bookmark=1"
    ).json()["items"] == []
    assert len(client.get(
        f"/api/books/{corpus['book_a']}/memory-corrections?bookmark=2"
    ).json()["items"]) == 1

    csrf_b = _become(client, tokens, "b")
    assert client.get(
        f"/api/books/{corpus['book_a']}/memory-corrections"
    ).status_code == 404
    foreign = client.post(
        f"/api/books/{corpus['book_a']}/memory-corrections",
        json=body,
        headers={CSRF_HEADER: csrf_b},
    )
    assert foreign.status_code == 404
    with psycopg.connect(database) as conn:
        row = conn.execute(
            "SELECT invalid_at FROM entities WHERE owner_id=%s AND id=%s",
            (corpus["owner_a"], corpus["entity_1"]),
        ).fetchone()
        assert row == (2,)
        assert conn.execute(
            "SELECT count(*) FROM entity_corrections WHERE owner_id=%s AND book_id=%s",
            (corpus["owner_a"], corpus["book_a"]),
        ).fetchone() == (1,)


def test_two_device_position_merge_is_monotonic_observable_and_rewindable(
    tenant_app, corpus: dict, database: str
) -> None:
    client, tokens, _app = tenant_app
    csrf = _become(client, tokens, "a")
    book = corpus["book_a"]
    device_a = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    device_b = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    device_c = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")

    opened = client.get(f"/api/books/{book}/position")
    assert opened.status_code == 200
    assert opened.json()["completed_chapter"] == opened.json()["bookmark"] == 1
    assert opened.json()["position_version"] == 0
    assert opened.json()["last_opened_at"] is not None
    shelf_book = next(item for item in client.get("/api/books").json() if item["id"] == str(book))
    assert shelf_book["last_opened_at"] is not None

    def update(*, device, sequence, offset, completed, base=0, epoch=0):
        return client.put(
            f"/api/books/{book}/position",
            json={
                "cfi": f"epubcfi(/6/{offset})",
                "offset": offset,
                "completed_chapter": completed,
                "position_epoch": epoch,
                "base_version": base,
                "client_id": str(device),
                "client_sequence": sequence,
            },
            headers={CSRF_HEADER: csrf},
        )

    no_csrf = client.put(
        f"/api/books/{book}/position",
        json={
            "cfi": "epubcfi(/6/200)",
            "offset": 200,
            "completed_chapter": 1,
            "position_epoch": 0,
            "base_version": 0,
            "client_id": str(device_a),
            "client_sequence": 1,
        },
    )
    assert no_csrf.status_code == 403
    first = update(device=device_a, sequence=1, offset=200, completed=1)
    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, no-store"
    assert first.json()["applied"] is True and first.json()["position_version"] == 1

    assert update(device=device_a, sequence=2, offset=210, completed=1, base=99).status_code == 409
    assert update(device=device_a, sequence=2, offset=210, completed=999, base=1).status_code == 422

    stale = update(device=device_b, sequence=1, offset=100, completed=1)
    assert stale.status_code == 200
    assert stale.json()["applied"] is False
    assert stale.json()["conflict"] == "stale_behind"
    assert stale.json()["current_offset"] == 200

    advance = update(device=device_b, sequence=2, offset=300, completed=2)
    assert advance.status_code == 200
    assert advance.json()["applied"] is True
    assert advance.json()["conflict"] == "merged_advance"
    assert advance.json()["bookmark"] == advance.json()["completed_chapter"] == 2

    tie_lost = update(device=device_a, sequence=99, offset=300, completed=2)
    assert tie_lost.status_code == 200
    assert tie_lost.json()["applied"] is False
    assert tie_lost.json()["conflict"] == "tie_lost"
    tie_won = update(device=device_c, sequence=1, offset=300, completed=2)
    assert tie_won.status_code == 200
    assert tie_won.json()["applied"] is True
    assert tie_won.json()["conflict"] == "tie_won"

    async def simultaneous_updates():
        repository = PostgresTenantRepository(database)
        owner = OwnerId(corpus["owner_a"])
        return await asyncio.gather(
            repository.update_position(
                owner,
                book,
                cfi="epubcfi(/6/400)",
                offset=400,
                completed_chapter=2,
                expected_epoch=0,
                base_version=3,
                client_id=device_a,
                client_sequence=2,
            ),
            repository.update_position(
                owner,
                book,
                cfi="epubcfi(/6/500)",
                offset=500,
                completed_chapter=2,
                expected_epoch=0,
                base_version=3,
                client_id=device_b,
                client_sequence=3,
            ),
        )

    simultaneous = asyncio.run(simultaneous_updates())
    assert any(item["applied"] is True for item in simultaneous)
    assert any(item["current_offset"] == 500 for item in simultaneous)
    canonical = client.get(f"/api/books/{book}/position").json()
    assert canonical["current_offset"] == canonical["high_water_offset"] == 500

    rewind = client.post(
        f"/api/books/{book}/position/reset",
        json={"position_epoch": 0},
        headers={CSRF_HEADER: csrf},
    )
    assert rewind.status_code == 200
    assert rewind.json()["position_epoch"] == 1
    assert rewind.json()["current_offset"] == rewind.json()["high_water_offset"] == 0
    assert update(
        device=device_a,
        sequence=4,
        offset=600,
        completed=2,
        base=canonical["position_version"],
        epoch=0,
    ).status_code == 409

    with psycopg.connect(database) as conn:
        bookmark, epoch, version = conn.execute(
            "SELECT bookmark,position_epoch,position_version FROM reading_state WHERE owner_id=%s AND book_id=%s",
            (corpus["owner_a"], book),
        ).fetchone()
    assert (bookmark, epoch, version) == (0, 1, rewind.json()["position_version"])

    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE books SET deleted_at=now() WHERE owner_id=%s AND id=%s",
            (corpus["owner_a"], book),
        )
    after_delete = update(
        device=device_a,
        sequence=5,
        offset=700,
        completed=2,
        base=rewind.json()["position_version"],
        epoch=1,
    )
    assert after_delete.status_code == 404
    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT bookmark,current_offset,position_version FROM reading_state WHERE owner_id=%s AND book_id=%s",
            (corpus["owner_a"], book),
        ).fetchone() == (0, 0, rewind.json()["position_version"])


def test_owner_identity_input_is_rejected_not_ignored(tenant_app, corpus: dict) -> None:
    client, tokens, _app = tenant_app
    csrf = _become(client, tokens, "a")
    assert client.get(
        "/api/books", params={"owner_id": str(corpus["owner_b"])}
    ).status_code == 422
    assert client.post(
        f"/api/books/{corpus['book_a']}/position/reset",
        json={"position_epoch": 0, "owner_id": str(corpus["owner_b"])},
        headers={CSRF_HEADER: csrf},
    ).status_code == 422
    assert client.put(
        f"/api/books/{corpus['book_a']}/position",
        json={
            "cfi": "epubcfi(/6/2)",
            "offset": 1,
            "completed_chapter": 0,
            "position_epoch": 0,
            "base_version": 0,
            "client_id": str(uuid.uuid4()),
            "client_sequence": 1,
            "owner_id": str(corpus["owner_b"]),
        },
        headers={CSRF_HEADER: csrf},
    ).status_code == 422
    assert client.get(
        f"/api/books/{corpus['book_a']}/epub", params={"storage_key": uuid.uuid4().hex}
    ).status_code == 422


def test_hosted_source_lifecycle_is_owner_scoped(tenant_app, corpus: dict, database: str) -> None:
    client, tokens, app = tenant_app
    payload = epub_ncx(
        [("chapter.xhtml", "Chapter I", "Chapter I", "Aldric arrived. " * 30)],
        title="Private upload",
        author="Owner A",
    )
    csrf_a = _become(client, tokens, "a")
    assert client.post(
        "/api/books", files={"file": ("private.epub", payload, EPUB_MEDIA_TYPE)}
    ).status_code == 403
    assert client.post(
        "/api/books",
        files={"file": ("private.epub", payload, "application/octet-stream")},
        headers={CSRF_HEADER: csrf_a},
    ).status_code == 415
    assert client.post(
        "/api/books",
        data={"storage_key": uuid.uuid4().hex},
        files={"file": ("private.epub", payload, EPUB_MEDIA_TYPE)},
        headers={CSRF_HEADER: csrf_a},
    ).status_code == 422
    uploaded = client.post(
        "/api/books",
        files={"file": ("private.epub", payload, EPUB_MEDIA_TYPE)},
        headers={CSRF_HEADER: csrf_a},
    )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert body["title"] == "Private upload"
    assert body["author"] == "Owner A"
    assert body["atoms"] == 1
    assert body["job"]["state"] == "waiting_configuration"
    assert body["job"]["book_id"] == body["id"]
    assert body["job"]["completed_chapters"] == 0
    assert body["job"]["total_chapters"] == 1
    assert not ({"worker_id", "lease_token", "payload_metadata"} & set(body["job"]))
    job_id = body["job"]["id"]
    assert not ({"storage_key", "object_id", "object_key"} & set(body))
    book_id = body["id"]
    with psycopg.connect(database) as conn:
        stored_metadata = conn.execute(
            """
            SELECT storage_provider,storage_key,media_type,byte_size,sha256,encryption_key_id,
                   verified_at IS NOT NULL
            FROM source_objects WHERE owner_id=%s AND book_id=%s AND deleted_at IS NULL
            """,
            (corpus["owner_a"], book_id),
        ).fetchone()
    assert stored_metadata[0] == "filesystem"
    assert len(stored_metadata[1]) == 32 and stored_metadata[1] not in body.values()
    assert stored_metadata[2:] == (
        EPUB_MEDIA_TYPE,
        len(payload),
        hashlib.sha256(payload).hexdigest(),
        "AES-256-GCM",
        True,
    )

    own_source = client.get(f"/api/books/{book_id}/epub")
    assert own_source.status_code == 200
    assert own_source.content == payload
    assert own_source.headers["content-type"] == EPUB_MEDIA_TYPE
    assert own_source.headers["cache-control"] == "private, no-store"
    assert client.get(f"/api/books/{book_id}").status_code == 200
    manifest = client.get(f"/api/books/{book_id}/manifest")
    assert manifest.status_code == 200
    assert [(atom["ordinal"], atom["title"]) for atom in manifest.json()["atoms"]] == [
        (1, "Chapter I")
    ]
    assert client.get(
        f"/api/books/{book_id}/search", params={"q": "Aldric arrived"}
    ).json()["hits"] == []
    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE reading_state SET bookmark=1 WHERE owner_id=%s AND book_id=%s",
            (corpus["owner_a"], book_id),
        )
    assert len(
        client.get(
            f"/api/books/{book_id}/search", params={"q": "Aldric arrived"}
        ).json()["hits"]
    ) == 1

    csrf_b = _become(client, tokens, "b")
    hidden = client.get(f"/api/books/{book_id}/epub")
    absent = client.get(f"/api/books/{uuid.uuid4()}/epub")
    assert (hidden.status_code, hidden.json()) == (absent.status_code, absent.json())
    assert hidden.status_code == 404
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert all(item["id"] != job_id for item in client.get("/api/jobs").json())
    assert client.post(
        f"/api/jobs/{job_id}/cancel", headers={CSRF_HEADER: csrf_b}
    ).status_code == 404
    assert client.delete(
        f"/api/books/{book_id}", headers={CSRF_HEADER: csrf_b}
    ).status_code == 404

    _become(client, tokens, "a")
    assert client.get(f"/api/books/{book_id}/epub").content == payload
    assert client.get(f"/api/jobs/{job_id}").json()["state"] == "waiting_configuration"
    cancelled = client.post(
        f"/api/jobs/{job_id}/cancel", headers={CSRF_HEADER: csrf_a}
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    assert cancelled.json()["cancellation_requested_at"] is not None
    assert client.delete(f"/api/books/{book_id}").status_code == 403
    deleted = client.delete(
        f"/api/books/{book_id}", headers={CSRF_HEADER: csrf_a}
    )
    assert deleted.status_code == 204
    assert client.get(f"/api/books/{book_id}").status_code == 404
    assert client.get(f"/api/books/{book_id}/epub").status_code == 404
    assert all(item["id"] != book_id for item in client.get("/api/books").json())
    assert not any(path.is_file() for path in app.state.object_storage._root.rglob("*"))
    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT deleted_at IS NOT NULL FROM source_objects WHERE owner_id=%s AND book_id=%s",
            (corpus["owner_a"], book_id),
        ).fetchone() == (True,)
        assert conn.execute(
            "SELECT count(*) FROM book_search_documents WHERE owner_id=%s AND book_id=%s",
            (corpus["owner_a"], book_id),
        ).fetchone()[0] == 0


def test_ask_the_book_is_owner_scoped_spoiler_bounded_and_costed(
    tenant_app, tenant_dsn: str, corpus: dict, monkeypatch
) -> None:
    client, tokens, _app = tenant_app
    csrf_b = _become(client, tokens, "b")
    foreign = client.post(
        f"/api/books/{corpus['book_a']}/ask",
        json={"question": "Where was the lantern?"},
        headers={CSRF_HEADER: csrf_b},
    )
    missing = client.post(
        f"/api/books/{uuid.uuid4()}/ask",
        json={"question": "Where was the lantern?"},
        headers={CSRF_HEADER: csrf_b},
    )
    assert (foreign.status_code, foreign.json()) == (missing.status_code, missing.json())
    assert foreign.status_code == 404

    csrf_a = _become(client, tokens, "a")
    own = client.post(
        f"/api/books/{corpus['book_a']}/ask",
        json={"question": "visible lantern", "bookmark": 99},
        headers={CSRF_HEADER: csrf_a},
    )
    assert own.status_code == 503
    assert own.json()["detail"]["code"] == "ai_not_configured"

    credential = client.post(
        "/api/credentials",
        json={"provider": "openai-compatible", "secret": "ask-test-key"},
        headers={CSRF_HEADER: csrf_a},
    ).json()
    configured = client.put(
        "/api/provider-settings/synthesis",
        json={
            "provider": "openai-compatible",
            "model": "available-model",
            "credential_id": credential["id"],
            "base_url": "https://api.openai.com/v1",
        },
        headers={CSRF_HEADER: csrf_a},
    )
    assert configured.status_code == 200
    validated = client.post(
        "/api/provider-settings/synthesis/validate",
        headers={CSRF_HEADER: csrf_a},
    )
    assert validated.status_code == 200
    synthesis_setting = validated.json()["setting"]
    configured_judge = client.put(
        "/api/provider-settings/judge",
        json={
            "provider": "openai-compatible",
            "model": "available-model",
            "credential_id": credential["id"],
            "base_url": "https://api.openai.com/v1",
        },
        headers={CSRF_HEADER: csrf_a},
    )
    assert configured_judge.status_code == 200
    assert client.post(
        "/api/provider-settings/judge/validate", headers={CSRF_HEADER: csrf_a}
    ).json()["code"] == "ok"

    class FakeCompletionClient:
        _is_native_openai = True

        def complete(self, _system, _user, *, tier, schema, max_output_tokens):
            del tier, max_output_tokens
            if "insufficient_evidence" in schema.model_fields:
                return {
                    "insufficient_evidence": False,
                    "claims": [{
                        "text": "A visible lantern stood beside the private window.",
                        "citation_ids": [1],
                    }],
                }, {"in": 18, "out": 9}
            return {"references_future": False, "unsupported_claims": []}, {"in": 10, "out": 3}

    monkeypatch.setattr(
        "app.hosted.tenant.api.completion_client",
        lambda _setting, _secret: FakeCompletionClient(),
    )
    monkeypatch.setattr("app.hosted.tenant.api.close_completion_client", lambda _client: None)
    answered = client.post(
        f"/api/books/{corpus['book_a']}/ask",
        json={"question": "visible lantern", "bookmark": 99},
        headers={CSRF_HEADER: csrf_a},
    )
    assert answered.status_code == 200
    payload = answered.json()
    assert payload["as_of_chapter"] == 1
    assert payload["claims"][0]["citation_ids"] == [1]
    assert payload["citations"][0]["href"] == "a-1.xhtml"
    assert payload["cost"]["input_tokens"] == 28
    assert payload["cost"]["output_tokens"] == 12
    assert payload["cost"]["pricing_known"] is False

    repository = PostgresTenantRepository(tenant_dsn)
    owner_a = OwnerId(corpus["owner_a"])
    owner_b = OwnerId(corpus["owner_b"])
    assert asyncio.run(
        repository.ask_context(
            owner_b,
            corpus["book_a"],
            "lantern",
            requested_bookmark=None,
        )
    ) is None
    context = asyncio.run(
        repository.ask_context(
            owner_a,
            corpus["book_a"],
            "lantern",
            requested_bookmark=99,
        )
    )
    assert context is not None
    assert context["as_of_chapter"] == 1
    assert context["sources"] and all(source["ordinal"] <= 1 for source in context["sources"])
    assert all("judgment" not in source["text"].casefold() for source in context["sources"])

    reservation = asyncio.run(
        repository.reserve_provider_call(
            owner_a,
            corpus["book_a"],
            phase="synthesis",
            provider="openai-compatible",
            model="available-model",
            reserved_input_tokens=20,
            reserved_output_tokens=10,
            reserved_usd="0",
            idempotency_key=f"ask-test:{uuid.uuid4()}",
            setting_id=uuid.UUID(synthesis_setting["id"]),
            expected_setting_updated_at=synthesis_setting["updated_at"],
            credential_id=uuid.UUID(synthesis_setting["credential_id"]),
        )
    )
    asyncio.run(
        repository.settle_provider_call(
            owner_a,
            reservation["id"],
            input_tokens=12,
            output_tokens=4,
            usd="0",
        )
    )
    costs = asyncio.run(repository.list_costs(owner_a, corpus["book_a"]))
    assert costs is not None
    assert any(item["phase"] == "synthesis" for item in costs["items"])


def test_reading_assistance_is_owner_scoped_frontier_bounded_and_costed(
    tenant_app, tenant_dsn: str, corpus: dict, monkeypatch
) -> None:
    client, tokens, _app = tenant_app
    csrf_b = _become(client, tokens, "b")
    for suffix, body in (
        ("selection-action", {
            "action": "define", "text": "comet", "atom": 2, "cfi": "epubcfi(/6/4)",
        }),
        ("chapter-closeout", {"chapter": 1}),
    ):
        foreign = client.post(
            f"/api/books/{corpus['book_a']}/{suffix}", json=body, headers={CSRF_HEADER: csrf_b}
        )
        missing = client.post(
            f"/api/books/{uuid.uuid4()}/{suffix}", json=body, headers={CSRF_HEADER: csrf_b}
        )
        assert (foreign.status_code, foreign.json()) == (missing.status_code, missing.json())
        assert foreign.status_code == 404

    csrf_a = _become(client, tokens, "a")
    unconfigured = client.post(
        f"/api/books/{corpus['book_a']}/chapter-closeout",
        json={"chapter": 1},
        headers={CSRF_HEADER: csrf_a},
    )
    assert unconfigured.status_code == 503

    credential = client.post(
        "/api/credentials",
        json={"provider": "openai-compatible", "secret": "assist-test-key"},
        headers={CSRF_HEADER: csrf_a},
    ).json()
    for capability in ("synthesis", "judge"):
        saved = client.put(
            f"/api/provider-settings/{capability}",
            json={
                "provider": "openai-compatible",
                "model": "available-model",
                "credential_id": credential["id"],
                "base_url": "https://api.openai.com/v1",
            },
            headers={CSRF_HEADER: csrf_a},
        )
        assert saved.status_code == 200
        checked = client.post(
            f"/api/provider-settings/{capability}/validate", headers={CSRF_HEADER: csrf_a}
        )
        assert checked.status_code == 200 and checked.json()["code"] == "ok"

    class FakeCompletionClient:
        _is_native_openai = True

        def complete(self, _system, _user, *, tier, schema, max_output_tokens):
            del tier, max_output_tokens
            fields = schema.model_fields
            if "citation_ids" in fields:
                return {
                    "insufficient_evidence": False,
                    "text": "Comet names the bright object mentioned in the selection.",
                    "citation_ids": [1],
                }, {"in": 16, "out": 8}
            if "claims" in fields:
                return {
                    "insufficient_evidence": False,
                    "claims": [{
                        "text": "A visible lantern stood beside the private window.",
                        "citation_ids": [1],
                    }],
                }, {"in": 24, "out": 9}
            return {"references_future": False, "unsupported_claims": []}, {"in": 10, "out": 3}

    monkeypatch.setattr(
        "app.hosted.tenant.api.completion_client",
        lambda _setting, _secret: FakeCompletionClient(),
    )
    monkeypatch.setattr("app.hosted.tenant.api.close_completion_client", lambda _client: None)

    selection = client.post(
        f"/api/books/{corpus['book_a']}/selection-action",
        json={
            "action": "define",
            "text": "comet",
            "atom": 2,
            "cfi": "epubcfi(/6/4)",
        },
        headers={CSRF_HEADER: csrf_a},
    )
    assert selection.status_code == 200
    selected = selection.json()
    assert selected["citation"]["ordinal"] == 2
    assert selected["citation"]["cfi"] == "epubcfi(/6/4)"
    assert selected["cost"]["input_tokens"] == 26
    assert selected["cost"]["pricing_known"] is False

    closeout = client.post(
        f"/api/books/{corpus['book_a']}/chapter-closeout",
        json={"chapter": 1},
        headers={CSRF_HEADER: csrf_a},
    )
    assert closeout.status_code == 200
    closed = closeout.json()
    assert closed["chapter"] == 1
    assert closed["citations"][0]["href"] == "a-1.xhtml"
    assert all(citation["ordinal"] == 1 for citation in closed["citations"])
    future = client.post(
        f"/api/books/{corpus['book_a']}/chapter-closeout",
        json={"chapter": 2},
        headers={CSRF_HEADER: csrf_a},
    )
    assert future.status_code == 409
    assert "ending" not in future.text.casefold()

    repository = PostgresTenantRepository(tenant_dsn)
    owner_a = OwnerId(corpus["owner_a"])
    owner_b = OwnerId(corpus["owner_b"])
    assert asyncio.run(
        repository.selection_action_context(owner_b, corpus["book_a"], 1)
    ) is None
    assert asyncio.run(
        repository.chapter_closeout_context(owner_b, corpus["book_a"], 1)
    ) is None
    context = asyncio.run(
        repository.chapter_closeout_context(owner_a, corpus["book_a"], 1)
    )
    assert context is not None and context["documents"]
    assert all("ending" not in document["content"].casefold() for document in context["documents"])
    costs = asyncio.run(repository.list_costs(owner_a, corpus["book_a"]))
    assert costs is not None
    assert sum(item["input_tokens"] for item in costs["items"]) >= 60


def test_unavailable_cross_tenant_surfaces_are_non_disclosing(
    tenant_app, corpus: dict
) -> None:
    client, tokens, _app = tenant_app
    _become(client, tokens, "b")
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    for item in inventory["unavailable"]:
        foreign_path = item["path"].replace("{book_id}", str(corpus["book_a"]))
        missing_path = item["path"].replace("{book_id}", str(uuid.uuid4()))
        foreign = client.request(item["method"], foreign_path)
        missing = client.request(item["method"], missing_path)
        assert (foreign.status_code, foreign.json()) == (missing.status_code, missing.json())
        assert foreign.status_code == item["expected_status"], item


def test_security_audit_events_and_canaries_are_content_free(
    tenant_app, corpus: dict, database: str, caplog
) -> None:
    client, tokens, _app = tenant_app
    caplog.set_level("DEBUG")
    csrf = _become(client, tokens, "a")
    credential_canary = "sk-audit-private-canary-4Zp8"
    provider_secret_canary = "invalid-owner-key"
    authorization_canary = "Bearer audit-authorization-canary-Q7v2"
    epub_text_canary = "AUDIT_EPUB_PRIVATE_TEXT_CANARY_R5m1"
    provider_error_canary = "private provider detail"
    responses = []

    created = client.post(
        "/api/credentials",
        json={"provider": "openai-compatible", "secret": credential_canary},
        headers={CSRF_HEADER: csrf, "Authorization": authorization_canary},
    )
    responses.append(created.text)
    assert created.status_code == 201
    invalid_provider = client.post(
        "/api/credentials",
        json={"provider": "openai-compatible", "secret": provider_secret_canary},
        headers={CSRF_HEADER: csrf},
    )
    responses.append(invalid_provider.text)
    assert invalid_provider.status_code == 201
    configured = client.put(
        "/api/provider-settings/extraction",
        json={
            "provider": "openai-compatible",
            "model": "available-model",
            "credential_id": invalid_provider.json()["id"],
            "base_url": "https://api.openai.com/v1",
        },
        headers={CSRF_HEADER: csrf},
    )
    responses.append(configured.text)
    assert configured.status_code == 200
    validated = client.post(
        "/api/provider-settings/extraction/validate", headers={CSRF_HEADER: csrf}
    )
    responses.append(validated.text)
    assert validated.status_code == 200
    assert validated.json()["code"] == "invalid_credentials"
    responses.append(
        client.get("/api/books", headers={"Authorization": authorization_canary}).text
    )
    payload = epub_ncx(
        [("chapter.xhtml", "Chapter I", "Chapter I", epub_text_canary)],
        title="Audit evidence book",
    )
    uploaded = client.post(
        "/api/books",
        files={"file": ("audit-canary.epub", payload, EPUB_MEDIA_TYPE)},
        headers={CSRF_HEADER: csrf, "Authorization": authorization_canary},
    )
    responses.append(uploaded.text)
    assert uploaded.status_code == 201
    cancelled = client.post(
        f"/api/jobs/{uploaded.json()['job']['id']}/cancel",
        headers={CSRF_HEADER: csrf, "Authorization": authorization_canary},
    )
    responses.append(cancelled.text)
    assert cancelled.status_code == 200
    invalid = client.put(
        f"/api/credentials/{created.json()['id']}",
        json={"secret": credential_canary + "\n"},
        headers={CSRF_HEADER: csrf, "Authorization": authorization_canary},
    )
    responses.append(invalid.text)
    assert invalid.status_code == 422

    with psycopg.connect(database) as conn:
        events = conn.execute(
            """
            SELECT actor_kind,action,target_kind,target_id,result,metadata,occurred_at
            FROM audit_events WHERE owner_id=%s ORDER BY occurred_at,id
            """,
            (corpus["owner_a"],),
        ).fetchall()
        persisted_jobs = conn.execute(
            "SELECT payload_metadata,sanitized_error FROM jobs WHERE owner_id=%s",
            (corpus["owner_a"],),
        ).fetchall()
    actions = {row[1] for row in events}
    assert {
        "credential.create",
        "provider_setting.update",
        "provider_setting.validate",
        "book.import",
        "job.cancel",
    } <= actions
    assert all(row[0] == "owner" for row in events)
    assert all(row[2] and row[3] and row[4] in {"succeeded", "denied", "failed"} for row in events)
    assert all(row[6].tzinfo is not None for row in events)
    assert all(set(row[5]) <= {"reason_code"} for row in events)

    inspected = inspect_events(database, owner_id=corpus["owner_a"], limit=100)
    assert inspected
    assert set(inspected[0]) == {
        "owner_id",
        "id",
        "actor_kind",
        "action",
        "target_kind",
        "target_id",
        "result",
        "reason_code",
        "occurred_at",
    }
    rendered = "\n".join(
        responses
        + [record.getMessage() for record in caplog.records]
        + [repr(events), repr(persisted_jobs), repr(inspected)]
    )
    for canary in (
        credential_canary,
        provider_secret_canary,
        authorization_canary,
        epub_text_canary,
        provider_error_canary,
    ):
        assert canary not in rendered

    with pytest.raises(psycopg.errors.CheckViolation):
        with psycopg.connect(database) as conn:
            conn.execute(
                """
                INSERT INTO audit_events
                  (owner_id,actor_kind,action,target_kind,result,metadata)
                VALUES (%s,'owner','credential.create','credential','succeeded',%s::jsonb)
                """,
                (corpus["owner_a"], json.dumps({"secret": credential_canary})),
            )

    cutoff = datetime.now(UTC) - timedelta(days=90)
    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE audit_events SET occurred_at=%s WHERE owner_id=%s AND id=("
            "SELECT id FROM audit_events WHERE owner_id=%s ORDER BY occurred_at,id LIMIT 1)",
            (cutoff - timedelta(seconds=1), corpus["owner_a"], corpus["owner_a"]),
        )
    assert purge_events(database, before=cutoff) == 1


def test_repository_owner_predicates_hold_when_rls_is_bypassed(database: str, corpus: dict) -> None:
    repository = PostgresTenantRepository(database)  # migration superuser deliberately bypasses RLS
    owner_b = OwnerId(corpus["owner_b"])
    assert asyncio.run(repository.get_book(owner_b, corpus["book_a"])) is None
    assert asyncio.run(repository.get_book_manifest(owner_b, corpus["book_a"])) is None
    assert asyncio.run(
        repository.search_book(owner_b, corpus["book_a"], "lantern", limit=20)
    ) is None
    assert asyncio.run(
        repository.ask_context(owner_b, corpus["book_a"], "lantern", requested_bookmark=None)
    ) is None
    assert asyncio.run(repository.selection_action_context(owner_b, corpus["book_a"], 1)) is None
    assert asyncio.run(repository.chapter_closeout_context(owner_b, corpus["book_a"], 1)) is None
    assert asyncio.run(repository.list_reader_marks(owner_b, corpus["book_a"])) is None
    assert asyncio.run(repository.get_position(owner_b, corpus["book_a"])) is None
    assert asyncio.run(repository.get_reader_preferences(owner_b, corpus["book_a"])) is None
    assert asyncio.run(repository.memory_snapshot(owner_b, corpus["book_a"])) is None
    assert asyncio.run(repository.list_costs(owner_b, corpus["book_a"])) is None
    assert asyncio.run(repository.source_object(owner_b, corpus["book_a"])) is None
    assert not asyncio.run(repository.soft_delete_book(owner_b, corpus["book_a"]))
    assert asyncio.run(repository.get_job(owner_b, uuid.uuid4())) is None
    assert asyncio.run(repository.cancel_job(owner_b, uuid.uuid4())) is None
    assert asyncio.run(repository.get_credential(owner_b, uuid.uuid4())) is None
    assert not asyncio.run(repository.delete_credential(owner_b, uuid.uuid4()))
    assert asyncio.run(repository.list_provider_settings(owner_b)) == []
    assert asyncio.run(repository.get_provider_setting(owner_b, "extraction")) is None
    with pytest.raises(MissingTenantResourceError):
        asyncio.run(repository.reset_position(owner_b, corpus["book_a"], 0))
    with pytest.raises(MissingTenantResourceError):
        asyncio.run(
            repository.upsert_reader_preferences(
                owner_b,
                corpus["book_a"],
                font_size="book",
                line_height="comfortable",
                measure="balanced",
                theme="paper",
                margins="balanced",
                typeface="publisher",
            )
        )


def test_tenant_role_is_rls_enforced_and_privilege_allow_list_is_exact(
    tenant_dsn: str, database: str
) -> None:
    repository = PostgresTenantRepository(tenant_dsn)
    asyncio.run(repository.check_runtime_role())
    role = conninfo.conninfo_to_dict(tenant_dsn)["user"]
    with psycopg.connect(database, autocommit=True) as conn:
        assert conn.execute(
            "SELECT rolsuper,rolbypassrls,rolinherit FROM pg_roles WHERE rolname=%s", (role,)
        ).fetchone() == (False, False, False)
        conn.execute(sql.SQL("REVOKE SELECT ON themes FROM {}").format(sql.Identifier(role)))
    try:
        with pytest.raises(AuthConfigurationError, match="allow-list"):
            asyncio.run(repository.check_runtime_role())
    finally:
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL("GRANT SELECT ON themes TO {}").format(sql.Identifier(role)))

    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute(sql.SQL("GRANT DELETE ON source_objects TO {}").format(sql.Identifier(role)))
    try:
        with pytest.raises(AuthConfigurationError, match="allow-list"):
            asyncio.run(repository.check_runtime_role())
    finally:
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL("REVOKE DELETE ON source_objects FROM {}").format(sql.Identifier(role)))
