"""Real-PostgreSQL acceptance tests for the hosted persistence foundation (LIT-38)."""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg import conninfo

from app.hosted.migrations import apply_migrations, discover_migrations
from app.hosted.credentials import CredentialCipher, rewrap_credentials
from app.hosted.tenant.models import OwnerId


pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def admin_dsn() -> str:
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for the real PostgreSQL suite")
    return dsn


@pytest.fixture()
def database(admin_dsn: str):
    database_name = f"lit38_{uuid.uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{database_name}"')
    dsn = conninfo.make_conninfo(admin_dsn, dbname=database_name)
    try:
        yield dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            admin.execute(f'DROP DATABASE "{database_name}"')


def test_migrations_are_contiguous_forward_only_files() -> None:
    migrations = discover_migrations()
    assert [migration.version for migration in migrations] == list(range(1, len(migrations) + 1))
    assert all("BEGIN" not in migration.sql.upper() for migration in migrations)
    assert all("COMMIT" not in migration.sql.upper() for migration in migrations)


def test_empty_database_is_reproducible_and_pgvector_enabled(database: str) -> None:
    apply_migrations(database)
    apply_migrations(database)

    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        rows = conn.execute(
            "SELECT version, name, checksum FROM app_schema_migrations ORDER BY version"
        ).fetchall()

    migrations = discover_migrations()
    assert [(row[0], row[1], row[2]) for row in rows] == [
        (migration.version, migration.name, migration.checksum) for migration in migrations
    ]


def test_applied_migration_checksum_and_history_are_immutable(database: str) -> None:
    apply_migrations(database)
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE app_schema_migrations SET checksum = repeat('0', 64) WHERE version = 1")
    with pytest.raises(RuntimeError, match="differs from committed file"):
        apply_migrations(database)

    with psycopg.connect(database) as conn:
        migration = discover_migrations()[0]
        conn.execute(
            "UPDATE app_schema_migrations SET checksum = %s WHERE version = 1",
            (migration.checksum,),
        )
        conn.execute("DELETE FROM app_schema_migrations WHERE version = 2")
    with pytest.raises(RuntimeError, match="not a contiguous prefix"):
        apply_migrations(database)


def test_every_tenant_table_has_non_null_owner_and_rls(database: str) -> None:
    apply_migrations(database)
    service_tables = {"app_schema_migrations", "provider_capabilities", "oidc_login_attempts"}
    expected_tables = {
        "users",
        "external_identities",
        "sessions",
        "books",
        "source_objects",
        "reading_state",
        "reader_preferences",
        "book_search_documents",
        "chapters",
        "ingested_chapters",
        "chapter_summaries",
        "entities",
        "aliases",
        "edges",
        "events",
        "event_participants",
        "themes",
        "entity_state",
        "entity_corrections",
        "chunks",
        "chunk_embeddings",
        "provider_credentials",
        "provider_model_settings",
        "jobs",
        "job_attempts",
        "cost_ledger",
        "cost_reservations",
        "highlights",
        "annotations",
        "bookmarks",
        "audit_events",
    }

    with psycopg.connect(database) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
        tenant_tables = tables - service_tables - {"users"}
        nullable_owner_columns = conn.execute(
            """
            SELECT table_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND column_name = 'owner_id'
              AND is_nullable <> 'NO'
            """
        ).fetchall()
        owner_tables = {
            row[0]
            for row in conn.execute(
                """
                SELECT table_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND column_name = 'owner_id'
                """
            )
        }
        rls_tables = {
            row[0]
            for row in conn.execute(
                "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace "
                "AND relkind = 'r' AND relrowsecurity"
            )
        }

    assert expected_tables <= tables
    assert tenant_tables <= owner_tables
    assert nullable_owner_columns == []
    assert tenant_tables <= rls_tables


def test_reading_state_has_monotonic_cross_device_clock(database: str) -> None:
    apply_migrations(database)
    with psycopg.connect(database) as conn:
        columns = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                """SELECT column_name,is_nullable,column_default
                   FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='reading_state'"""
            )
        }
    for name in (
        "current_offset",
        "high_water_offset",
        "position_version",
        "last_opened_at",
        "last_client_id",
        "last_client_sequence",
    ):
        assert name in columns
    assert all(columns[name][0] == "NO" for name in (
        "current_offset", "high_water_offset", "position_version", "last_opened_at"
    ))


def test_reader_preferences_are_constrained_and_owner_scoped(database: str) -> None:
    apply_migrations(database)
    with psycopg.connect(database) as conn:
        columns = {
            row[0]
            for row in conn.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='reader_preferences'"""
            )
        }
        rls = conn.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
            "WHERE oid='reader_preferences'::regclass"
        ).fetchone()
    assert {
        "owner_id",
        "font_size",
        "line_height",
        "measure",
        "theme",
        "margins",
        "typeface",
        "preference_version",
        "updated_at",
    } <= columns
    assert rls == (True, True)


def test_book_search_documents_have_generated_index_and_forced_rls(database: str) -> None:
    apply_migrations(database)
    with psycopg.connect(database) as conn:
        columns = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                """SELECT column_name,is_generated,generation_expression
                   FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='book_search_documents'"""
            )
        }
        rls = conn.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
            "WHERE oid='book_search_documents'::regclass"
        ).fetchone()
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename='book_search_documents'"
            )
        }
    assert columns["search_vector"][0] == "ALWAYS"
    assert "to_tsvector" in columns["search_vector"][1]
    assert "ix_book_search_documents_owner_vector" in indexes
    assert rls == (True, True)


def test_reader_marks_require_portable_bounded_anchors_and_versions(database: str) -> None:
    apply_migrations(database)
    with psycopg.connect(database) as conn:
        columns = {
            (row[0], row[1])
            for row in conn.execute(
                """SELECT table_name,column_name FROM information_schema.columns
                   WHERE table_schema='public'
                     AND table_name IN ('highlights','annotations','bookmarks')"""
            )
        }
        constraints = {
            row[0]
            for row in conn.execute(
                """SELECT conname FROM pg_constraint
                   WHERE conrelid IN ('highlights'::regclass,'annotations'::regclass,
                                      'bookmarks'::regclass)"""
            )
        }
    assert all((table, "version") in columns for table in (
        "highlights", "annotations", "bookmarks"
    ))
    assert {
        "highlights_anchor_portable",
        "annotations_anchor_portable",
        "bookmarks_anchor_portable",
        "highlights_text_bounded",
        "annotations_body_bounded",
        "bookmarks_label_bounded",
    } <= constraints


def test_provider_credentials_have_ciphertext_not_plaintext_fields(database: str) -> None:
    apply_migrations(database)
    with psycopg.connect(database) as conn:
        columns = {
            row[0]
            for row in conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'provider_credentials'
                """
            )
        }

    assert {"ciphertext", "encrypted_data_key", "key_version", "nonce"} <= columns
    assert {"plaintext", "api_key", "secret", "token"}.isdisjoint(columns)


def test_provider_credential_master_key_rotation_rewraps_without_plaintext(database: str) -> None:
    apply_migrations(database)
    owner = OwnerId(uuid.uuid4())
    credential_id = uuid.uuid4()
    old_cipher = CredentialCipher({"old": b"o" * 32}, active_version="old")
    envelope = old_cipher.encrypt(owner, credential_id, "anthropic", "rotation-canary")
    with psycopg.connect(database) as conn:
        conn.execute("INSERT INTO users (id,display_name) VALUES (%s,'Rotation Owner')", (owner.value,))
        conn.execute(
            """
            INSERT INTO provider_credentials
              (owner_id,id,provider,masked_label,ciphertext,encrypted_data_key,
               encryption_algorithm,key_version,nonce)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                envelope.owner_id,
                envelope.credential_id,
                envelope.provider,
                envelope.masked_label,
                envelope.ciphertext,
                envelope.encrypted_data_key,
                envelope.encryption_algorithm,
                envelope.key_version,
                envelope.nonce,
            ),
        )

    active = CredentialCipher(
        {"old": b"o" * 32, "new": b"n" * 32}, active_version="new"
    )
    assert rewrap_credentials(database, active, batch_size=1) == 1
    assert rewrap_credentials(database, active, batch_size=1) == 0
    with psycopg.connect(database) as conn:
        row = conn.execute(
            """
            SELECT owner_id,id,provider,masked_label,ciphertext,encrypted_data_key,
                   encryption_algorithm,key_version,nonce
            FROM provider_credentials WHERE owner_id=%s AND id=%s
            """,
            (owner.value, credential_id),
        ).fetchone()
    rotated = type(envelope)(
        owner_id=row[0],credential_id=row[1],provider=row[2],masked_label=row[3],
        ciphertext=bytes(row[4]),encrypted_data_key=bytes(row[5]),
        encryption_algorithm=row[6],key_version=row[7],nonce=bytes(row[8]),
    )
    assert rotated.key_version == "new" and rotated.ciphertext == envelope.ciphertext
    with active.decrypt(rotated) as resolved:
        assert resolved.get_secret_value() == "rotation-canary"


def test_source_object_rows_require_opaque_encrypted_verified_epub_metadata(database: str) -> None:
    apply_migrations(database)
    owner = uuid.uuid4()
    book = uuid.uuid4()
    incarnation = uuid.uuid4()
    base = {
        "provider": "filesystem",
        "media_type": "application/epub+zip",
        "byte_size": 4,
        "encryption": "AES-256-GCM",
        "verified": True,
    }
    statement = """
        INSERT INTO source_objects
          (owner_id,id,book_id,book_incarnation,storage_provider,storage_key,media_type,
           byte_size,sha256,encryption_key_id,verified_at)
        VALUES (%(owner)s,%(id)s,%(book)s,%(incarnation)s,%(provider)s,%(storage_key)s,
                %(media_type)s,%(byte_size)s,%(sha256)s,%(encryption)s,
                CASE WHEN %(verified)s THEN now() ELSE NULL END)
    """
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute("INSERT INTO users (id,display_name) VALUES (%s,'Owner')", (owner,))
        conn.execute(
            """
            INSERT INTO books (owner_id,id,incarnation,title,schema_version)
            VALUES (%s,%s,%s,'Book',1)
            """,
            (owner, book, incarnation),
        )
        for override in (
            {"provider": "client-path"},
            {"storage_key": "owners/client/supplied.epub"},
            {"media_type": "application/octet-stream"},
            {"byte_size": 0},
            {"encryption": None},
            {"verified": False},
        ):
            object_id = uuid.uuid4()
            values = {
                **base,
                "storage_key": object_id.hex,
                **override,
                "owner": owner,
                "id": object_id,
                "book": book,
                "incarnation": incarnation,
                "sha256": "a" * 64,
            }
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(statement, values)

        valid_id = uuid.uuid4()
        valid = {
            **base,
            "storage_key": valid_id.hex,
            "owner": owner,
            "id": valid_id,
            "book": book,
            "incarnation": incarnation,
            "sha256": "b" * 64,
        }
        conn.execute(statement, valid)
        duplicate_id = uuid.uuid4()
        duplicate = {**valid, "id": duplicate_id, "storage_key": duplicate_id.hex}
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(statement, duplicate)


def test_tenant_foreign_keys_and_indexes_carry_owner_scope(database: str) -> None:
    apply_migrations(database)
    reviewed_non_owner_first_indexes = {
        "external_identities_issuer_subject_key",
        "sessions_session_digest_key",
        # Privileged worker scheduler scans all tenants; every mutation remains owner-qualified.
        "ix_jobs_global_claim",
        # PostgreSQL's built-in GIN tsvector operator class cannot prefix UUID owner_id. Queries
        # still require owner/book/incarnation predicates and forced RLS before heap rows are visible.
        "ix_book_search_documents_owner_vector",
    }
    with psycopg.connect(database) as conn:
        foreign_keys = conn.execute(
            """
            SELECT child.relname, constraint_row.conname,
                   array_agg(child_column.attname ORDER BY key_column.ordinality)
            FROM pg_constraint AS constraint_row
            JOIN pg_class AS child ON child.oid = constraint_row.conrelid
            JOIN unnest(constraint_row.conkey) WITH ORDINALITY AS key_column(attnum, ordinality)
              ON true
            JOIN pg_attribute AS child_column
              ON child_column.attrelid = child.oid AND child_column.attnum = key_column.attnum
            WHERE constraint_row.contype = 'f'
              AND EXISTS (
                SELECT 1 FROM pg_attribute AS parent_owner
                WHERE parent_owner.attrelid = constraint_row.confrelid
                  AND parent_owner.attname = 'owner_id'
                  AND NOT parent_owner.attisdropped
              )
            GROUP BY child.relname, constraint_row.conname
            """
        ).fetchall()
        indexes = conn.execute(
            """
            SELECT table_row.relname, index_row.relname,
                   pg_get_indexdef(index_row.oid, 1, true)
            FROM pg_index AS index_meta
            JOIN pg_class AS index_row ON index_row.oid = index_meta.indexrelid
            JOIN pg_class AS table_row ON table_row.oid = index_meta.indrelid
            WHERE table_row.relnamespace = 'public'::regnamespace
              AND EXISTS (
                SELECT 1 FROM pg_attribute AS owner_column
                WHERE owner_column.attrelid = table_row.oid
                  AND owner_column.attname = 'owner_id'
                  AND NOT owner_column.attisdropped
              )
            """
        ).fetchall()

    assert foreign_keys
    assert all("owner_id" in columns for _, _, columns in foreign_keys)
    assert all(
        first_column == "owner_id" or index_name in reviewed_non_owner_first_indexes
        for _, index_name, first_column in indexes
    )


def test_vector_foundation_has_no_cross_tenant_ann_index(database: str) -> None:
    apply_migrations(database)
    with psycopg.connect(database) as conn:
        access_methods = {
            row[0]
            for row in conn.execute(
                """
                SELECT access_method.amname
                FROM pg_index AS index_meta
                JOIN pg_class AS table_row ON table_row.oid = index_meta.indrelid
                JOIN pg_class AS index_row ON index_row.oid = index_meta.indexrelid
                JOIN pg_am AS access_method ON access_method.oid = index_row.relam
                WHERE table_row.oid = 'chunk_embeddings'::regclass
                """
            )
        }
        function_sql = conn.execute(
            "SELECT pg_get_functiondef('search_chunks_prefiltered'::regproc)"
        ).fetchone()[0]

    assert access_methods == {"btree"}
    assert "WITH eligible AS MATERIALIZED" in function_sql


def _seed_owner_and_book(conn, owner_id: uuid.UUID, book_id: uuid.UUID, incarnation: uuid.UUID) -> None:
    conn.execute("INSERT INTO users (id, display_name) VALUES (%s, %s)", (owner_id, "Reader"))
    conn.execute(
        """
        INSERT INTO books (owner_id, id, incarnation, title, schema_version)
        VALUES (%s, %s, %s, %s, 1)
        """,
        (owner_id, book_id, incarnation, "Test book"),
    )


def test_composite_foreign_keys_reject_cross_owner_references(database: str) -> None:
    apply_migrations(database)
    owner_a, owner_b = uuid.uuid4(), uuid.uuid4()
    book_id, incarnation = uuid.uuid4(), uuid.uuid4()

    with psycopg.connect(database) as conn:
        _seed_owner_and_book(conn, owner_a, book_id, incarnation)
        conn.execute("INSERT INTO users (id, display_name) VALUES (%s, %s)", (owner_b, "Intruder"))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                """
                INSERT INTO reading_state (owner_id, book_id, book_incarnation)
                VALUES (%s, %s, %s)
                """,
                (owner_b, book_id, incarnation),
            )


def test_derived_rows_require_matching_completed_receipt(database: str) -> None:
    apply_migrations(database)
    owner_id, book_id, incarnation = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    chapter_id = uuid.uuid4()

    with psycopg.connect(database) as conn:
        _seed_owner_and_book(conn, owner_id, book_id, incarnation)
        conn.execute(
            """
            INSERT INTO chapters
              (owner_id, book_id, book_incarnation, id, chapter_key, revealed_at, content_hash)
            VALUES (%s, %s, %s, %s, 'chapter-1', 1, repeat('a', 64))
            """,
            (owner_id, book_id, incarnation, chapter_id),
        )
        conn.execute(
            """
            INSERT INTO chunks
              (owner_id, book_id, book_incarnation, id, chapter_id, revealed_at, text)
            VALUES (%s, %s, %s, %s, %s, 1, 'visible text')
            """,
            (owner_id, book_id, incarnation, uuid.uuid4(), chapter_id),
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.commit()


def test_completed_receipt_is_bound_to_exact_chapter_content(database: str) -> None:
    apply_migrations(database)
    owner_id, book_id, incarnation = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    chapter_id = uuid.uuid4()

    with psycopg.connect(database) as conn:
        _seed_owner_and_book(conn, owner_id, book_id, incarnation)
        conn.execute(
            """
            INSERT INTO chapters
              (owner_id, book_id, book_incarnation, id, chapter_key, revealed_at, content_hash)
            VALUES (%s, %s, %s, %s, 'chapter-1', 1, repeat('a', 64))
            """,
            (owner_id, book_id, incarnation, chapter_id),
        )
        conn.execute(
            """
            INSERT INTO ingested_chapters
              (owner_id, book_id, book_incarnation, chapter_id, content_hash, completed_at)
            VALUES (%s, %s, %s, %s, repeat('b', 64), now())
            """,
            (owner_id, book_id, incarnation, chapter_id),
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.commit()


def test_idempotency_keys_are_tenant_scoped(database: str) -> None:
    apply_migrations(database)
    owner_a, owner_b = uuid.uuid4(), uuid.uuid4()
    job_a, job_b = uuid.uuid4(), uuid.uuid4()

    with psycopg.connect(database) as conn:
        conn.execute(
            "INSERT INTO users (id, display_name) VALUES (%s, 'A'), (%s, 'B')",
            (owner_a, owner_b),
        )
        conn.execute(
            "INSERT INTO jobs (owner_id, id, kind, idempotency_key, payload_metadata) "
            "VALUES (%s, %s, 'ingest_book', 'same', '{\"chapter_count\":1}')",
            (owner_a, job_a),
        )
        conn.execute(
            "INSERT INTO jobs (owner_id, id, kind, idempotency_key, payload_metadata) "
            "VALUES (%s, %s, 'ingest_book', 'same', '{\"chapter_count\":1}')",
            (owner_b, job_b),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO jobs (owner_id, id, kind, idempotency_key, payload_metadata) "
                "VALUES (%s, %s, 'ingest_book', 'same', '{\"chapter_count\":1}')",
                (owner_a, uuid.uuid4()),
            )


def test_rls_uses_transaction_local_owner_context(database: str) -> None:
    apply_migrations(database)
    owner_a, owner_b = uuid.uuid4(), uuid.uuid4()

    with psycopg.connect(database, autocommit=True) as admin:
        if admin.execute("SELECT 1 FROM pg_roles WHERE rolname = 'lit38_runtime'").fetchone() is None:
            admin.execute("CREATE ROLE lit38_runtime NOLOGIN")
        admin.execute("GRANT USAGE ON SCHEMA public TO lit38_runtime")
        admin.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO lit38_runtime")
        admin.execute(
            "INSERT INTO users (id, display_name) VALUES (%s, 'A'), (%s, 'B')",
            (owner_a, owner_b),
        )

    with psycopg.connect(database) as conn:
        conn.execute("SET LOCAL ROLE lit38_runtime")
        conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(owner_a),))
        assert conn.execute("SELECT id FROM users").fetchall() == [(owner_a,)]
        conn.commit()
        conn.execute("SET LOCAL ROLE lit38_runtime")
        assert conn.execute("SELECT id FROM users").fetchall() == []


def test_prefiltered_vector_search_excludes_ineligible_candidates(database: str) -> None:
    apply_migrations(database)
    owner_id, book_id, incarnation = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    chapter_1, chapter_2 = uuid.uuid4(), uuid.uuid4()
    chunk_visible, chunk_future, chunk_retracted, chunk_wrong_space = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )

    with psycopg.connect(database) as conn:
        _seed_owner_and_book(conn, owner_id, book_id, incarnation)
        for chapter_id, key, ordinal in (
            (chapter_1, "chapter-1", 1),
            (chapter_2, "chapter-2", 2),
        ):
            conn.execute(
                """
                INSERT INTO chapters
                  (owner_id, book_id, book_incarnation, id, chapter_key, revealed_at, content_hash)
                VALUES (%s, %s, %s, %s, %s, %s, repeat(%s, 64))
                """,
                (owner_id, book_id, incarnation, chapter_id, key, ordinal, str(ordinal)),
            )
            conn.execute(
                """
                INSERT INTO ingested_chapters
                  (owner_id, book_id, book_incarnation, chapter_id, content_hash, completed_at)
                VALUES (%s, %s, %s, %s, repeat(%s, 64), now())
                """,
                (owner_id, book_id, incarnation, chapter_id, str(ordinal)),
            )
        for chunk_id, chapter_id, ordinal, text, retracted_at in (
            (chunk_visible, chapter_1, 1, "eligible", None),
            (chunk_future, chapter_2, 2, "future", None),
            (chunk_retracted, chapter_1, 1, "retracted", "now()"),
            (chunk_wrong_space, chapter_1, 1, "wrong space", None),
        ):
            conn.execute(
                """
                INSERT INTO chunks
                  (owner_id, book_id, book_incarnation, id, chapter_id, revealed_at, text, retracted_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, CASE WHEN %s::boolean THEN now() ELSE NULL END)
                """,
                (
                    owner_id,
                    book_id,
                    incarnation,
                    chunk_id,
                    chapter_id,
                    ordinal,
                    text,
                    retracted_at is not None,
                ),
            )
        for chunk_id, vector, embedding_space in (
            (chunk_visible, "[0,1,0]", "test-space"),
            (chunk_future, "[1,0,0]", "test-space"),
            (chunk_retracted, "[1,0,0]", "test-space"),
            (chunk_wrong_space, "[1,0,0]", "other-space"),
        ):
            conn.execute(
                """
                INSERT INTO chunk_embeddings
                  (owner_id, book_id, book_incarnation, chunk_id, embedding_model,
                   embedding_dimension, embedding_space, distance_metric, embedding)
                VALUES (%s, %s, %s, %s, 'test-model', 3, %s, 'cosine', %s::vector)
                """,
                (owner_id, book_id, incarnation, chunk_id, embedding_space, vector),
            )

        rows = conn.execute(
            """
            SELECT chunk_id FROM search_chunks_prefiltered(
              %s, %s, %s, 1, 'test-model', 3, 'test-space', 'cosine', '[1,0,0]'::vector, 10
            )
            """,
            (owner_id, book_id, incarnation),
        ).fetchall()

    assert rows == [(chunk_visible,)]
