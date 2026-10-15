"""Real-PostgreSQL correction regression; skipped without the isolated database harness."""

import json
import uuid

import psycopg
import pytest

from app.hosted.auth.api import CSRF_HEADER
from tests.hosted import test_tenant_postgres as hosted_fixtures

admin_dsn = hosted_fixtures.admin_dsn
database = hosted_fixtures.database
tenant_dsn = hosted_fixtures.tenant_dsn
corpus = hosted_fixtures.corpus
tenant_app = hosted_fixtures.tenant_app

pytestmark = pytest.mark.postgres


def test_hosted_reread_correction_retains_later_owner_scoped_dependencies(
    tenant_app, corpus, database
):
    client, tokens, _app = tenant_app
    owner, book, incarnation = (corpus[key] for key in ("owner_a", "book_a", "incarnation_a"))
    source, friend = corpus["entity_1"], corpus["entity_2"]
    chapter, event = uuid.uuid4(), uuid.uuid4()
    scope = (owner, book, incarnation)
    with psycopg.connect(database) as conn:
        first_chapter = conn.execute(
            "SELECT id FROM chapters WHERE owner_id=%s AND book_id=%s AND revealed_at=1",
            (owner, book),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO chapters
                 (owner_id,book_id,book_incarnation,id,chapter_key,revealed_at,content_hash)
               VALUES (%s,%s,%s,%s,'audit-chapter-three',3,%s)""",
            (*scope, chapter, "c" * 64),
        )
        conn.execute(
            """INSERT INTO ingested_chapters
                 (owner_id,book_id,book_incarnation,chapter_id,content_hash,completed_at)
               VALUES (%s,%s,%s,%s,%s,now())""",
            (*scope, chapter, "c" * 64),
        )
        conn.execute(
            """INSERT INTO aliases
                 (owner_id,book_id,book_incarnation,entity_id,source_chapter_id,surface_form,revealed_at)
               VALUES (%s,%s,%s,%s,%s,'The Later Smith',3)""",
            (*scope, source, chapter),
        )
        for chapter_id, reveal, expiry, note in (
            (first_chapter, 1, 3, "earlier"), (chapter, 3, 4, "later"),
        ):
            conn.execute(
                """INSERT INTO entity_state
                     (owner_id,book_id,book_incarnation,entity_id,source_chapter_id,status,
                      revealed_at,invalid_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (*scope, source, chapter_id, psycopg.types.json.Jsonb({"note": note}), reveal, expiry),
            )
        conn.execute(
            """INSERT INTO edges
                 (owner_id,book_id,book_incarnation,source_chapter_id,src_entity_id,dst_entity_id,
                  relationship_type,label,revealed_at)
               VALUES (%s,%s,%s,%s,%s,%s,'social','Later work',3)""",
            (*scope, chapter, source, friend),
        )
        conn.execute(
            """INSERT INTO events
                 (owner_id,book_id,book_incarnation,id,source_chapter_id,order_idx,summary,revealed_at)
               VALUES (%s,%s,%s,%s,%s,0,'A later audit event',3)""",
            (*scope, event, chapter),
        )
        conn.execute(
            """INSERT INTO event_participants
                 (owner_id,book_id,book_incarnation,event_id,entity_id,source_chapter_id,role,revealed_at)
               VALUES (%s,%s,%s,%s,%s,%s,'worker',3)""",
            (*scope, event, source, chapter),
        )
        conn.execute("UPDATE reading_state SET bookmark=3 WHERE owner_id=%s AND book_id=%s", (owner, book))
    csrf = hosted_fixtures._become(client, tokens, "a")
    body = {
        "source_entity_id": str(source), "canonical_name": "A Corrected Visible One",
        "reason": "A name-only correction during a later reading pass.", "bookmark": 2,
    }
    assert client.post(
        f"/api/books/{book}/memory-corrections", json=body, headers={CSRF_HEADER: csrf}
    ).status_code == 409
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE reading_state SET bookmark=2 WHERE owner_id=%s AND book_id=%s", (owner, book))
    corrected = client.post(
        f"/api/books/{book}/memory-corrections", json=body, headers={CSRF_HEADER: csrf}
    )
    assert corrected.status_code == 200
    target = uuid.UUID(corrected.json()["target_entity_id"])
    assert "Later Smith" not in json.dumps(corrected.json())
    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT surface_form,revealed_at FROM aliases WHERE owner_id=%s AND book_id=%s "
            "AND entity_id=%s AND surface_form='The Later Smith'", (owner, book, target)
        ).fetchall() == [("The Later Smith", 3)]
        assert conn.execute(
            "SELECT revealed_at,invalid_at FROM entity_state WHERE owner_id=%s AND book_id=%s "
            "AND entity_id=%s ORDER BY revealed_at", (owner, book, target)
        ).fetchall() == [(2, 3), (3, 4)]
        assert conn.execute(
            "SELECT entity_id,revealed_at FROM event_participants WHERE owner_id=%s AND book_id=%s "
            "AND event_id=%s ORDER BY revealed_at,entity_id", (owner, book, event)
        ).fetchall() == sorted([(source, 3), (target, 3)], key=lambda row: (row[1], row[0]))
        assert conn.execute(
            "SELECT count(*) FROM ingested_chapters WHERE owner_id=%s AND book_id=%s", (owner, book)
        ).fetchone() == (3,)
        assert conn.execute(
            "SELECT count(*) FROM entities WHERE owner_id=%s AND canonical_name=%s",
            (corpus["owner_b"], body["canonical_name"]),
        ).fetchone() == (0,)
    at_two = client.get(f"/api/books/{book}/memory").json()
    assert "A later audit event" not in json.dumps(at_two)
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE reading_state SET bookmark=3 WHERE owner_id=%s AND book_id=%s", (owner, book))
    at_three = client.get(f"/api/books/{book}/memory").json()
    later_edge = next(row for row in at_three["relationships"] if row["label"] == "Later work")
    assert later_edge["src_entity_id"] == str(target)
    assert "invalid_at" not in later_edge
