"""PostgreSQL repositories whose API makes tenant scope impossible to omit accidentally.

Every operation opens one transaction, sets the RLS owner locally, and still includes the same owner
in every predicate. The two barriers are independently testable by calling this repository through a
non-BYPASSRLS runtime role or a superuser connection that bypasses RLS.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import AsyncIterator

import psycopg
from psycopg.rows import dict_row

from app.hosted.audit import record_event_async
from app.hosted.auth.repository import AuthConfigurationError
from app.hosted.credentials import (
    CredentialCipher,
    CredentialUnavailableError,
    EncryptedCredential,
    ResolvedProviderCredential,
)
from app.hosted.limits import LimitExceededError
from app.hosted.provider_settings import ValidationResult
from app.hosted.tenant.models import (
    FuturePositionVersionError,
    InvalidPositionError,
    MissingTenantResourceError,
    OwnerId,
    SourceObjectRecord,
    StalePositionEpochError,
)


@dataclass(slots=True)
class PostgresTenantRepository:
    _dsn: str = field(repr=False)

    async def _connect(self) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(self._dsn, row_factory=dict_row)

    @asynccontextmanager
    async def _transaction(self, owner_id: OwnerId) -> AsyncIterator[psycopg.AsyncConnection]:
        async with await self._connect() as conn, conn.transaction():
            await conn.execute(
                "SELECT set_config('app.owner_id', %s, true)", (str(owner_id.value),)
            )
            yield conn

    async def check_runtime_role(self) -> None:
        required = {
            "public.books": {"SELECT", "INSERT", "UPDATE"},
            "public.source_objects": {"SELECT", "INSERT", "UPDATE"},
            "public.reading_state": {"SELECT", "INSERT", "UPDATE"},
            "public.reader_preferences": {"SELECT", "INSERT", "UPDATE"},
            "public.book_search_documents": {"SELECT", "INSERT", "DELETE"},
            "public.highlights": {"SELECT", "INSERT", "UPDATE"},
            "public.annotations": {"SELECT", "INSERT", "UPDATE"},
            "public.bookmarks": {"SELECT", "INSERT", "UPDATE"},
            "public.chapters": {"SELECT"},
            "public.ingested_chapters": {"SELECT"},
            "public.chapter_summaries": {"SELECT"},
            "public.entities": {"SELECT", "INSERT", "UPDATE"},
            "public.aliases": {"SELECT", "INSERT"},
            "public.edges": {"SELECT", "INSERT"},
            "public.events": {"SELECT"},
            "public.event_participants": {"SELECT", "INSERT"},
            "public.entity_state": {"SELECT", "INSERT"},
            "public.entity_corrections": {"SELECT", "INSERT"},
            "public.themes": {"SELECT"},
            "public.cost_ledger": {"SELECT", "INSERT"},
            "public.cost_reservations": {"SELECT", "INSERT", "UPDATE"},
            "public.jobs": {"SELECT", "INSERT", "UPDATE"},
            "public.provider_credentials": {"SELECT", "INSERT", "UPDATE"},
            "public.provider_model_settings": {"SELECT", "INSERT", "UPDATE"},
            "public.owner_limits": {"SELECT"},
            "public.owner_request_windows": {"SELECT", "INSERT", "UPDATE"},
            "public.audit_events": {"INSERT"},
        }
        all_privileges = {
            "SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"
        }
        async with await self._connect() as conn:
            role = await (
                await conn.execute(
                    """
                    SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole,
                           rolreplication, rolinherit,
                           EXISTS (SELECT 1 FROM pg_auth_members WHERE member = role_row.oid)
                             AS has_membership
                    FROM pg_roles AS role_row WHERE rolname = current_user
                    """
                )
            ).fetchone()
            if role is None or role["rolsuper"] or role["rolbypassrls"] or any(
                role[name]
                for name in (
                    "rolcreatedb", "rolcreaterole", "rolreplication", "rolinherit", "has_membership"
                )
            ):
                raise AuthConfigurationError(
                    "HOSTED_TENANT_DSN must use an isolated non-superuser RLS-enforced role"
                )

            incorrect = []
            for table, expected in sorted(required.items()):
                for privilege in sorted(all_privileges):
                    row = await (
                        await conn.execute(
                            "SELECT has_table_privilege(current_user, %s, %s)",
                            (table, privilege),
                        )
                    ).fetchone()
                    held = bool(row and row["has_table_privilege"])
                    if held != (privilege in expected):
                        incorrect.append(f"{table}:{privilege}")

            unexpected = await (
                await conn.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                      AND table_name <> ALL(%s)
                      AND has_table_privilege(
                            current_user, quote_ident(table_schema) || '.' || quote_ident(table_name),
                            'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                          )
                    ORDER BY table_name
                    """,
                    ([name.removeprefix("public.") for name in required],),
                )
            ).fetchall()
        if incorrect or unexpected:
            raise AuthConfigurationError(
                "tenant runtime role privileges are not the reviewed repository allow-list"
            )

    async def consume_request_limit(self, owner_id: OwnerId) -> dict:
        async with self._transaction(owner_id) as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text,47))",
                (owner_id.value,),
            )
            await conn.execute(
                """
                INSERT INTO public.owner_request_windows (owner_id,request_count)
                VALUES (%s,0) ON CONFLICT (owner_id) DO NOTHING
                """,
                (owner_id.value,),
            )
            row = await (
                await conn.execute(
                    """
                    SELECT limits.requests_per_window,limits.request_window_seconds,
                           rate_window.window_started_at,rate_window.request_count,
                           clock_timestamp() AS checked_at
                    FROM public.owner_limits AS limits
                    JOIN public.owner_request_windows AS rate_window USING (owner_id)
                    WHERE limits.owner_id=%s
                    FOR UPDATE OF rate_window
                    """,
                    (owner_id.value,),
                )
            ).fetchone()
            if row is None:
                raise RuntimeError("owner limit policy is unavailable")
            elapsed = (row["checked_at"] - row["window_started_at"]).total_seconds()
            if elapsed >= row["request_window_seconds"]:
                used = 1
                elapsed = 0
                await conn.execute(
                    """
                    UPDATE public.owner_request_windows
                    SET window_started_at=%s,request_count=1,updated_at=now()
                    WHERE owner_id=%s
                    """,
                    (row["checked_at"], owner_id.value),
                )
            elif row["request_count"] >= row["requests_per_window"]:
                retry = max(1, math.ceil(row["request_window_seconds"] - elapsed))
                raise LimitExceededError(
                    "request_rate_exceeded",
                    row["requests_per_window"],
                    retry,
                    "Retry after the current request window resets.",
                )
            else:
                used = row["request_count"] + 1
                await conn.execute(
                    """
                    UPDATE public.owner_request_windows
                    SET request_count=%s,updated_at=now() WHERE owner_id=%s
                    """,
                    (used, owner_id.value),
                )
        return {
            "limit": row["requests_per_window"],
            "remaining": row["requests_per_window"] - used,
            "reset_after_seconds": max(1, math.ceil(row["request_window_seconds"] - elapsed)),
        }

    async def limit_status(self, owner_id: OwnerId) -> dict:
        async with self._transaction(owner_id) as conn:
            row = await (
                await conn.execute(
                    """
                    SELECT limits.max_upload_bytes,limits.max_library_bytes,limits.max_books,
                           limits.max_active_jobs,limits.requests_per_window,
                           limits.request_window_seconds,limits.max_provider_concurrency,
                           limits.max_spend_usd,limits.updated_at,
                           (SELECT count(*) FROM public.books AS book
                            WHERE book.owner_id=limits.owner_id AND book.deleted_at IS NULL) AS books,
                           COALESCE((SELECT sum(object.byte_size)
                            FROM public.source_objects AS object
                            WHERE object.owner_id=limits.owner_id AND object.deleted_at IS NULL),0)
                              AS library_bytes,
                           (SELECT count(*) FROM public.jobs AS job
                            WHERE job.owner_id=limits.owner_id AND job.state IN
                              ('waiting_configuration','pending','leased','running')) AS active_jobs,
                           COALESCE((SELECT sum(entry.usd) FROM public.cost_ledger AS entry
                            WHERE entry.owner_id=limits.owner_id),0) AS spend_usd,
                           COALESCE((SELECT sum(reservation.reserved_usd)
                            FROM public.cost_reservations AS reservation
                            WHERE reservation.owner_id=limits.owner_id
                              AND reservation.state='reserved'),0) AS reserved_usd
                    FROM public.owner_limits AS limits WHERE limits.owner_id=%s
                    """,
                    (owner_id.value,),
                )
            ).fetchone()
        if row is None:
            raise RuntimeError("owner limit policy is unavailable")
        return self._json_row(row)

    async def list_books(self, owner_id: OwnerId) -> list[dict]:
        async with self._transaction(owner_id) as conn:
            rows = await (
                await conn.execute(
                    """
                    SELECT b.id,b.title,b.author,b.content_language,b.book_type,b.created_at,
                           s.last_opened_at,
                           COALESCE(s.bookmark,0) AS bookmark,
                           COALESCE(s.position_epoch,0) AS position_epoch
                    FROM public.books AS b
                    LEFT JOIN public.reading_state AS s
                      ON (s.owner_id,s.book_id,s.book_incarnation)=(b.owner_id,b.id,b.incarnation)
                    WHERE b.owner_id=%s AND b.deleted_at IS NULL
                    ORDER BY s.last_opened_at DESC NULLS LAST,b.created_at,b.id
                    """,
                    (owner_id.value,),
                )
            ).fetchall()
        return [self._book_payload(row) for row in rows]

    async def get_book(self, owner_id: OwnerId, book_id: uuid.UUID) -> dict | None:
        async with self._transaction(owner_id) as conn:
            row = await (
                await conn.execute(
                    """
                    SELECT b.id,b.title,b.author,b.content_language,b.book_type,b.created_at,
                           s.last_opened_at,
                           COALESCE(s.bookmark,0) AS bookmark,
                           COALESCE(s.position_epoch,0) AS position_epoch
                    FROM public.books AS b
                    LEFT JOIN public.reading_state AS s
                      ON (s.owner_id,s.book_id,s.book_incarnation)=(b.owner_id,b.id,b.incarnation)
                    WHERE b.owner_id=%s AND b.id=%s AND b.deleted_at IS NULL
                    """,
                    (owner_id.value, book_id),
                )
            ).fetchone()
        return self._book_payload(row) if row else None

    async def get_book_manifest(self, owner_id: OwnerId, book_id: uuid.UUID) -> dict | None:
        async with self._transaction(owner_id) as conn:
            book = await (
                await conn.execute(
                    """SELECT book.incarnation,book.content_language,book.book_type,
                              COALESCE(state.bookmark,0) AS bookmark
                       FROM public.books AS book
                       LEFT JOIN public.reading_state AS state
                         ON (state.owner_id,state.book_id,state.book_incarnation)
                          =(book.owner_id,book.id,book.incarnation)
                       WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL""",
                    (owner_id.value, book_id),
                )
            ).fetchone()
            if book is None:
                return None
            rows = await (
                await conn.execute(
                    """SELECT ordinal,href,title,part_label,char_end-char_start AS char_len
                       FROM public.book_search_documents
                       WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                       ORDER BY ordinal""",
                    (owner_id.value, book_id, book["incarnation"]),
                )
            ).fetchall()
        released = book["bookmark"] + 1
        atoms = [
            {
                "ordinal": row["ordinal"],
                "href": row["href"],
                "title": row["title"] if row["ordinal"] <= released else "",
                "part_label": row["part_label"] if row["ordinal"] <= released else "",
                "char_len": row["char_len"],
            }
            for row in rows
        ]
        identity = "|".join(
            f"{atom['ordinal']}:{atom['href']}:{atom['char_len']}" for atom in atoms
        )
        return {
            "book_id": str(book_id),
            "atom_set_version": hashlib.sha256(identity.encode()).hexdigest()[:16],
            "mode": "indexed",
            "content_language": book["content_language"],
            "book_profile": {
                "book_type": book["book_type"],
                "confidence": 1,
                "detector_version": "hosted-import-v1",
                "signals": [],
            },
            "atoms": atoms,
        }

    async def search_book(
        self, owner_id: OwnerId, book_id: uuid.UUID, query: str, *, limit: int
    ) -> dict | None:
        """Search only completed chapters of one live owned book.

        Reading-state ``bookmark`` is the spoiler frontier: the current chapter is deliberately not
        included because a context excerpt could otherwise reveal prose below the visible viewport.
        """
        async with self._transaction(owner_id) as conn:
            book = await (
                await conn.execute(
                    """SELECT book.incarnation,COALESCE(state.bookmark,0) AS bookmark
                       FROM public.books AS book
                       LEFT JOIN public.reading_state AS state
                         ON (state.owner_id,state.book_id,state.book_incarnation)
                          =(book.owner_id,book.id,book.incarnation)
                       WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL""",
                    (owner_id.value, book_id),
                )
            ).fetchone()
            if book is None:
                return None
            rows = await (
                await conn.execute(
                    """WITH search_query AS (
                         SELECT websearch_to_tsquery('simple',%s) AS value
                       )
                       SELECT document.ordinal,document.href,document.title,document.part_label,
                              ts_headline(
                                'simple',document.content,search_query.value,
                                'MaxFragments=1,MinWords=12,MaxWords=36,StartSel=[,StopSel=]'
                              ) AS snippet,
                              ts_rank_cd(document.search_vector,search_query.value) AS score
                       FROM public.book_search_documents AS document
                       CROSS JOIN search_query
                       WHERE document.owner_id=%s AND document.book_id=%s
                         AND document.book_incarnation=%s AND document.ordinal<=%s
                         AND document.search_vector @@ search_query.value
                       ORDER BY score DESC,document.ordinal,document.href
                       LIMIT %s""",
                    (
                        query,
                        owner_id.value,
                        book_id,
                        book["incarnation"],
                        book["bookmark"],
                        limit,
                    ),
                )
            ).fetchall()
        return {
            "as_of_chapter": book["bookmark"],
            "hits": [self._json_row(row) for row in rows],
        }

    async def ask_context(
        self,
        owner_id: OwnerId,
        book_id: uuid.UUID,
        question: str,
        *,
        requested_bookmark: int | None,
        limit: int = 6,
    ) -> dict | None:
        """Return only completed source passages plus this owner's current AI settings."""
        if limit < 1 or limit > 12:
            raise ValueError("ask passage limit must be between 1 and 12")
        async with self._transaction(owner_id) as conn:
            book = await (
                await conn.execute(
                    """SELECT book.incarnation,COALESCE(state.bookmark,0) AS bookmark
                       FROM public.books AS book
                       LEFT JOIN public.reading_state AS state
                         ON (state.owner_id,state.book_id,state.book_incarnation)
                          =(book.owner_id,book.id,book.incarnation)
                       WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL""",
                    (owner_id.value, book_id),
                )
            ).fetchone()
            if book is None:
                return None
            effective = int(book["bookmark"])
            if requested_bookmark is not None:
                effective = min(effective, requested_bookmark)
            settings = await (
                await conn.execute(
                    """SELECT id,provider,capability,credential_id,model,base_url,enabled,
                              validation_status,validation_error_code,validated_at,updated_at
                       FROM public.provider_model_settings
                       WHERE owner_id=%s AND capability IN ('synthesis','judge')
                       ORDER BY capability""",
                    (owner_id.value,),
                )
            ).fetchall()
            rows = []
            if effective > 0:
                rows = await (
                    await conn.execute(
                        """WITH search_query AS (
                             SELECT websearch_to_tsquery('simple',%s) AS value
                           )
                           SELECT document.ordinal,document.href,document.title,
                                  regexp_replace(
                                    ts_headline(
                                      'simple',document.content,search_query.value,
                                      'MaxFragments=2,MinWords=16,MaxWords=70'
                                    ), '</?b>', '', 'g'
                                  ) AS text,
                                  ts_rank_cd(document.search_vector,search_query.value) AS score
                           FROM public.book_search_documents AS document
                           CROSS JOIN search_query
                           WHERE document.owner_id=%s AND document.book_id=%s
                             AND document.book_incarnation=%s AND document.ordinal<=%s
                             AND document.search_vector @@ search_query.value
                           ORDER BY score DESC,document.ordinal,document.href
                           LIMIT %s""",
                        (
                            question,
                            owner_id.value,
                            book_id,
                            book["incarnation"],
                            effective,
                            limit,
                        ),
                    )
                ).fetchall()
        passages = []
        for row in rows:
            value = self._json_row(row)
            passages.append({
                "id": len(passages) + 1,
                "ordinal": value["ordinal"],
                "chapter_key": str(value["ordinal"]),
                "href": value["href"],
                "title": value["title"] or f"Chapter {value['ordinal']}",
                "text": " ".join(value["text"].split())[:2000],
                "score": value["score"],
            })
        return {
            "as_of_chapter": effective,
            "book_incarnation": str(book["incarnation"]),
            "settings": {row["capability"]: self._json_row(row) for row in settings},
            "sources": passages,
        }

    async def selection_action_context(
        self,
        owner_id: OwnerId,
        book_id: uuid.UUID,
        atom: int,
    ) -> dict | None:
        """Resolve a reader-supplied selection to one server-owned current/revealed chapter anchor."""
        async with self._transaction(owner_id) as conn:
            book = await (
                await conn.execute(
                    """SELECT book.incarnation,COALESCE(state.bookmark,0) AS bookmark
                       FROM public.books AS book
                       LEFT JOIN public.reading_state AS state
                         ON (state.owner_id,state.book_id,state.book_incarnation)
                          =(book.owner_id,book.id,book.incarnation)
                       WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL""",
                    (owner_id.value, book_id),
                )
            ).fetchone()
            if book is None:
                return None
            source = None
            if atom <= int(book["bookmark"]) + 1:
                source = await (
                    await conn.execute(
                        """SELECT ordinal,href,title
                           FROM public.book_search_documents
                           WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s AND ordinal=%s
                           ORDER BY char_start,href LIMIT 1""",
                        (owner_id.value, book_id, book["incarnation"], atom),
                    )
                ).fetchone()
            settings = await (
                await conn.execute(
                    """SELECT id,provider,capability,credential_id,model,base_url,enabled,
                              validation_status,validation_error_code,validated_at,updated_at
                       FROM public.provider_model_settings
                       WHERE owner_id=%s AND capability IN ('synthesis','judge')
                       ORDER BY capability""",
                    (owner_id.value,),
                )
            ).fetchall()
        return {
            "as_of_chapter": int(book["bookmark"]),
            "source": self._json_row(source) if source else None,
            "settings": {row["capability"]: self._json_row(row) for row in settings},
        }

    async def chapter_closeout_context(
        self,
        owner_id: OwnerId,
        book_id: uuid.UUID,
        chapter: int,
    ) -> dict | None:
        """Return exactly one completed chapter's text and current owner AI settings."""
        async with self._transaction(owner_id) as conn:
            book = await (
                await conn.execute(
                    """SELECT book.incarnation,COALESCE(state.bookmark,0) AS bookmark
                       FROM public.books AS book
                       LEFT JOIN public.reading_state AS state
                         ON (state.owner_id,state.book_id,state.book_incarnation)
                          =(book.owner_id,book.id,book.incarnation)
                       WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL""",
                    (owner_id.value, book_id),
                )
            ).fetchone()
            if book is None:
                return None
            rows = []
            if chapter <= int(book["bookmark"]):
                rows = await (
                    await conn.execute(
                        """WITH ranked AS (
                             SELECT ordinal,href,title,content,char_start,
                                    row_number() OVER (ORDER BY char_start,href) AS row_number,
                                    count(*) OVER () AS row_count
                             FROM public.book_search_documents
                             WHERE owner_id=%s AND book_id=%s
                               AND book_incarnation=%s AND ordinal=%s
                           )
                           SELECT ordinal,href,title,char_start,
                                  left(content,2500) || ' ' ||
                                  substring(content FROM greatest(1,length(content)/2-1250) FOR 2500)
                                  || ' ' || right(content,2500) AS content
                           FROM ranked
                           WHERE row_number=1 OR row_number=row_count
                              OR mod(row_number-1,greatest(1,ceil(row_count/4.0)::integer))=0
                           ORDER BY char_start,href LIMIT 6""",
                        (owner_id.value, book_id, book["incarnation"], chapter),
                    )
                ).fetchall()
            settings = await (
                await conn.execute(
                    """SELECT id,provider,capability,credential_id,model,base_url,enabled,
                              validation_status,validation_error_code,validated_at,updated_at
                       FROM public.provider_model_settings
                       WHERE owner_id=%s AND capability IN ('synthesis','judge')
                       ORDER BY capability""",
                    (owner_id.value,),
                )
            ).fetchall()
        documents = [self._json_row(row) for row in rows]
        return {
            "as_of_chapter": int(book["bookmark"]),
            "documents": documents,
            "settings": {row["capability"]: self._json_row(row) for row in settings},
        }

    async def reserve_provider_call(
        self,
        owner_id: OwnerId,
        book_id: uuid.UUID,
        *,
        phase: str,
        provider: str,
        model: str,
        reserved_input_tokens: int,
        reserved_output_tokens: int,
        reserved_usd: str | Decimal,
        idempotency_key: str,
        setting_id: uuid.UUID,
        expected_setting_updated_at: str,
        credential_id: uuid.UUID,
    ) -> dict:
        """Atomically enforce owner spend/concurrency before synchronous provider I/O."""
        if phase not in {"synthesis", "judge"}:
            raise ValueError("unsupported interactive provider phase")
        amount = Decimal(reserved_usd)
        if reserved_input_tokens < 0 or reserved_output_tokens < 0 or amount < 0:
            raise ValueError("provider reservation values cannot be negative")
        async with self._transaction(owner_id) as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text,47))",
                (owner_id.value,),
            )
            book = await (
                await conn.execute(
                    "SELECT incarnation FROM public.books "
                    "WHERE owner_id=%s AND id=%s AND deleted_at IS NULL",
                    (owner_id.value, book_id),
                )
            ).fetchone()
            if book is None:
                raise MissingTenantResourceError("unknown book")
            current_setting = await (
                await conn.execute(
                    """SELECT 1 FROM public.provider_model_settings
                       WHERE owner_id=%s AND id=%s AND capability=%s AND provider=%s AND model=%s
                         AND credential_id=%s AND enabled=true AND validation_status='ready'
                         AND updated_at=%s::timestamptz""",
                    (
                        owner_id.value,
                        setting_id,
                        phase,
                        provider,
                        model,
                        credential_id,
                        expected_setting_updated_at,
                    ),
                )
            ).fetchone()
            if current_setting is None:
                raise CredentialUnavailableError("provider setting changed before the call")
            policy = await (
                await conn.execute(
                    "SELECT max_provider_concurrency,max_spend_usd FROM public.owner_limits "
                    "WHERE owner_id=%s",
                    (owner_id.value,),
                )
            ).fetchone()
            if policy is None:
                raise RuntimeError("owner limit policy is unavailable")
            usage = await (
                await conn.execute(
                    """SELECT
                         (SELECT count(*) FROM public.cost_reservations
                          WHERE owner_id=%s AND state='reserved') AS active,
                         COALESCE((SELECT sum(usd) FROM public.cost_ledger
                                   WHERE owner_id=%s),0)
                         + COALESCE((SELECT sum(reserved_usd) FROM public.cost_reservations
                                     WHERE owner_id=%s AND state='reserved'),0) AS spend""",
                    (owner_id.value, owner_id.value, owner_id.value),
                )
            ).fetchone()
            if usage["active"] >= policy["max_provider_concurrency"]:
                raise LimitExceededError(
                    "provider_concurrency_exceeded",
                    policy["max_provider_concurrency"],
                    1,
                    "wait for another AI request to finish",
                )
            projected = Decimal(usage["spend"]) + amount
            if policy["max_spend_usd"] is not None and projected > policy["max_spend_usd"]:
                raise LimitExceededError(
                    "spend_limit_exceeded",
                    str(policy["max_spend_usd"]),
                    None,
                    "raise the provider spend limit or use offline reading features",
                )
            reservation_id = uuid.uuid4()
            await conn.execute(
                """INSERT INTO public.cost_reservations
                     (owner_id,id,book_id,book_incarnation,phase,provider,model,
                      reserved_input_tokens,reserved_output_tokens,reserved_usd,idempotency_key)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    owner_id.value,
                    reservation_id,
                    book_id,
                    book["incarnation"],
                    phase,
                    provider,
                    model,
                    reserved_input_tokens,
                    reserved_output_tokens,
                    amount,
                    idempotency_key,
                ),
            )
        return {
            "id": reservation_id,
            "book_incarnation": book["incarnation"],
            "reserved_input_tokens": reserved_input_tokens,
            "reserved_output_tokens": reserved_output_tokens,
            "reserved_usd": amount,
        }

    async def settle_provider_call(
        self,
        owner_id: OwnerId,
        reservation_id: uuid.UUID,
        *,
        input_tokens: int,
        output_tokens: int,
        usd: str | Decimal,
    ) -> None:
        """Publish measured (or conservative failure) usage and close one reservation atomically."""
        amount = Decimal(usd)
        if input_tokens < 0 or output_tokens < 0 or amount < 0:
            raise ValueError("provider usage cannot be negative")
        async with self._transaction(owner_id) as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text,47))",
                (owner_id.value,),
            )
            row = await (
                await conn.execute(
                    """SELECT id,book_id,book_incarnation,phase,provider,model,
                              reserved_input_tokens,reserved_output_tokens,reserved_usd,
                              idempotency_key,state
                       FROM public.cost_reservations
                       WHERE owner_id=%s AND id=%s FOR UPDATE""",
                    (owner_id.value, reservation_id),
                )
            ).fetchone()
            if row is None or row["state"] != "reserved":
                raise RuntimeError("provider cost reservation is unavailable")
            if (
                input_tokens > row["reserved_input_tokens"]
                or output_tokens > row["reserved_output_tokens"]
                or amount > row["reserved_usd"]
            ):
                raise RuntimeError("provider usage exceeded its reservation")
            await conn.execute(
                """INSERT INTO public.cost_ledger
                     (owner_id,book_id,book_incarnation,phase,provider,model,input_tokens,
                      output_tokens,usd,idempotency_key)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    owner_id.value,
                    row["book_id"],
                    row["book_incarnation"],
                    row["phase"],
                    row["provider"],
                    row["model"],
                    input_tokens,
                    output_tokens,
                    amount,
                    row["idempotency_key"],
                ),
            )
            await conn.execute(
                """UPDATE public.cost_reservations
                   SET actual_input_tokens=%s,actual_output_tokens=%s,actual_usd=%s,
                       state='settled',settled_at=now()
                   WHERE owner_id=%s AND id=%s AND state='reserved'""",
                (input_tokens, output_tokens, amount, owner_id.value, reservation_id),
            )

    @staticmethod
    async def _select_position(conn, owner_id: OwnerId, book_id: uuid.UUID):
        return await (
            await conn.execute(
                """SELECT state.bookmark,state.current_cfi AS cfi,state.position_epoch,
                          state.current_offset,state.high_water_offset,state.position_version,
                          state.last_opened_at,state.updated_at,
                          (SELECT count(receipt.chapter_id)
                           FROM public.chapters AS chapter
                           JOIN public.ingested_chapters AS receipt
                             ON (receipt.owner_id,receipt.book_id,receipt.book_incarnation,receipt.chapter_id)
                              =(chapter.owner_id,chapter.book_id,chapter.book_incarnation,chapter.id)
                           WHERE chapter.owner_id=state.owner_id
                             AND chapter.book_id=state.book_id
                             AND chapter.book_incarnation=state.book_incarnation
                             AND chapter.revealed_at<=state.bookmark
                             AND chapter.retracted_at IS NULL
                             AND receipt.retracted_at IS NULL) AS receipt_count
                   FROM public.books AS book
                   JOIN public.reading_state AS state
                     ON (state.owner_id,state.book_id,state.book_incarnation)
                      =(book.owner_id,book.id,book.incarnation)
                   WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL""",
                (owner_id.value, book_id),
            )
        ).fetchone()

    async def get_position(self, owner_id: OwnerId, book_id: uuid.UUID) -> dict | None:
        async with self._transaction(owner_id) as conn:
            touched = await (
                await conn.execute(
                    """UPDATE public.reading_state AS state SET last_opened_at=now()
                       FROM public.books AS book
                       WHERE (book.owner_id,book.id,book.incarnation)
                           =(state.owner_id,state.book_id,state.book_incarnation)
                         AND state.owner_id=%s AND state.book_id=%s
                         AND book.deleted_at IS NULL
                       RETURNING state.book_id""",
                    (owner_id.value, book_id),
                )
            ).fetchone()
            row = await self._select_position(conn, owner_id, book_id) if touched else None
        return self._position_payload(row) if row else None

    async def get_reader_preferences(
        self, owner_id: OwnerId, book_id: uuid.UUID
    ) -> dict | None:
        async with self._transaction(owner_id) as conn:
            row = await (
                await conn.execute(
                    """SELECT COALESCE(preference.font_size,'book') AS font_size,
                              COALESCE(preference.line_height,'comfortable') AS line_height,
                              COALESCE(preference.measure,'balanced') AS measure,
                              COALESCE(preference.theme,'paper') AS theme,
                              COALESCE(preference.margins,'balanced') AS margins,
                              COALESCE(preference.typeface,'publisher') AS typeface,
                              COALESCE(preference.preference_version,0) AS preference_version
                       FROM public.books AS book
                       LEFT JOIN public.reader_preferences AS preference
                         ON (preference.owner_id,preference.book_id,preference.book_incarnation)
                          =(book.owner_id,book.id,book.incarnation)
                       WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL""",
                    (owner_id.value, book_id),
                )
            ).fetchone()
        return None if row is None else dict(row)

    async def upsert_reader_preferences(
        self,
        owner_id: OwnerId,
        book_id: uuid.UUID,
        *,
        font_size: str,
        line_height: str,
        measure: str,
        theme: str,
        margins: str,
        typeface: str,
    ) -> dict:
        async with self._transaction(owner_id) as conn:
            book = await (
                await conn.execute(
                    """SELECT incarnation FROM public.books
                       WHERE owner_id=%s AND id=%s AND deleted_at IS NULL
                       FOR SHARE""",
                    (owner_id.value, book_id),
                )
            ).fetchone()
            if book is None:
                raise MissingTenantResourceError
            row = await (
                await conn.execute(
                    """INSERT INTO public.reader_preferences
                         (owner_id,book_id,book_incarnation,font_size,line_height,measure,theme,
                          margins,typeface,preferences,preference_version,updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb,1,now())
                       ON CONFLICT (owner_id,book_id,book_incarnation) DO UPDATE SET
                         font_size=EXCLUDED.font_size,line_height=EXCLUDED.line_height,
                         measure=EXCLUDED.measure,theme=EXCLUDED.theme,margins=EXCLUDED.margins,
                         typeface=EXCLUDED.typeface,preferences='{}'::jsonb,
                         preference_version=reader_preferences.preference_version+1,
                         updated_at=now()
                       RETURNING font_size,line_height,measure,theme,margins,typeface,
                                 preference_version""",
                    (
                        owner_id.value,
                        book_id,
                        book["incarnation"],
                        font_size,
                        line_height,
                        measure,
                        theme,
                        margins,
                        typeface,
                    ),
                )
            ).fetchone()
        return dict(row)

    @staticmethod
    async def _reader_mark_book(conn, owner_id: OwnerId, book_id: uuid.UUID):
        return await (
            await conn.execute(
                """SELECT book.incarnation,COALESCE(state.bookmark,0) AS bookmark
                   FROM public.books AS book
                   LEFT JOIN public.reading_state AS state
                     ON (state.owner_id,state.book_id,state.book_incarnation)
                      =(book.owner_id,book.id,book.incarnation)
                   WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL""",
                (owner_id.value, book_id),
            )
        ).fetchone()

    async def list_reader_marks(
        self, owner_id: OwnerId, book_id: uuid.UUID
    ) -> dict | None:
        """Return only marks at the current spoiler frontier.

        Starting a new reading pass lowers ``bookmark``. Older marks remain durable, but the query
        hides marks from later chapters until that frontier is reached again.
        """
        async with self._transaction(owner_id) as conn:
            book = await self._reader_mark_book(conn, owner_id, book_id)
            if book is None:
                return None
            rows = await (
                await conn.execute(
                    """SELECT * FROM (
                         SELECT 'highlight'::text AS kind,id,anchor,color,selected_text,
                                NULL::text AS body,NULL::text AS label,NULL::uuid AS highlight_id,
                                version,created_at,updated_at
                         FROM public.highlights
                         WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                           AND deleted_at IS NULL AND (anchor->>'atom')::integer<=%s
                         UNION ALL
                         SELECT 'annotation',id,anchor,NULL,NULL,body,NULL,highlight_id,
                                version,created_at,updated_at
                         FROM public.annotations
                         WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                           AND deleted_at IS NULL AND (anchor->>'atom')::integer<=%s
                         UNION ALL
                         SELECT 'bookmark',id,anchor,NULL,NULL,NULL,label,NULL::uuid,
                                version,created_at,updated_at
                         FROM public.bookmarks
                         WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                           AND deleted_at IS NULL AND (anchor->>'atom')::integer<=%s
                       ) AS mark ORDER BY created_at,id""",
                    (
                        owner_id.value, book_id, book["incarnation"], book["bookmark"] + 1,
                        owner_id.value, book_id, book["incarnation"], book["bookmark"] + 1,
                        owner_id.value, book_id, book["incarnation"], book["bookmark"] + 1,
                    ),
                )
            ).fetchall()
        return {
            "as_of_chapter": book["bookmark"],
            "marks": [self._json_row(row) for row in rows],
        }

    async def create_reader_mark(
        self,
        owner_id: OwnerId,
        book_id: uuid.UUID,
        *,
        kind: str,
        anchor: dict,
        color: str | None = None,
        selected_text: str | None = None,
        body: str | None = None,
        label: str | None = None,
        highlight_id: uuid.UUID | None = None,
    ) -> dict:
        async with self._transaction(owner_id) as conn:
            book = await self._reader_mark_book(conn, owner_id, book_id)
            if book is None:
                raise MissingTenantResourceError
            if anchor["atom"] > book["bookmark"] + 1:
                raise InvalidPositionError("mark anchor is beyond the revealed frontier")
            if kind == "highlight":
                row = await (
                    await conn.execute(
                        """INSERT INTO public.highlights
                             (owner_id,book_id,book_incarnation,anchor,color,selected_text)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           RETURNING 'highlight'::text AS kind,id,anchor,color,selected_text,
                                     NULL::text AS body,NULL::text AS label,
                                     NULL::uuid AS highlight_id,version,
                                     created_at,updated_at""",
                        (
                            owner_id.value, book_id, book["incarnation"],
                            psycopg.types.json.Jsonb(anchor), color, selected_text,
                        ),
                    )
                ).fetchone()
            elif kind == "annotation":
                if highlight_id is not None:
                    linked = await (
                        await conn.execute(
                            """SELECT id FROM public.highlights
                               WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                                 AND id=%s AND deleted_at IS NULL""",
                            (owner_id.value, book_id, book["incarnation"], highlight_id),
                        )
                    ).fetchone()
                    if linked is None:
                        raise MissingTenantResourceError
                row = await (
                    await conn.execute(
                        """INSERT INTO public.annotations
                             (owner_id,book_id,book_incarnation,highlight_id,anchor,body)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           RETURNING 'annotation'::text AS kind,id,anchor,NULL::text AS color,
                                     NULL::text AS selected_text,body,NULL::text AS label,
                                     highlight_id,version,
                                     created_at,updated_at""",
                        (
                            owner_id.value, book_id, book["incarnation"], highlight_id,
                            psycopg.types.json.Jsonb(anchor), body,
                        ),
                    )
                ).fetchone()
            elif kind == "bookmark":
                row = await (
                    await conn.execute(
                        """INSERT INTO public.bookmarks
                             (owner_id,book_id,book_incarnation,anchor,label)
                           VALUES (%s,%s,%s,%s,%s)
                           RETURNING 'bookmark'::text AS kind,id,anchor,NULL::text AS color,
                                     NULL::text AS selected_text,NULL::text AS body,label,
                                     NULL::uuid AS highlight_id,version,
                                     created_at,updated_at""",
                        (
                            owner_id.value, book_id, book["incarnation"],
                            psycopg.types.json.Jsonb(anchor), label,
                        ),
                    )
                ).fetchone()
            else:
                raise ValueError("unknown reader mark kind")
        return self._json_row(row)

    async def update_reader_mark(
        self,
        owner_id: OwnerId,
        book_id: uuid.UUID,
        mark_id: uuid.UUID,
        *,
        kind: str,
        value: str,
    ) -> dict | None:
        columns = {"highlight": "color", "annotation": "body", "bookmark": "label"}
        tables = {
            "highlight": "highlights",
            "annotation": "annotations",
            "bookmark": "bookmarks",
        }
        returning = {
            "highlight": (
                "color,selected_text,NULL::text AS body,NULL::text AS label,"
                "NULL::uuid AS highlight_id"
            ),
            "annotation": (
                "NULL::text AS color,NULL::text AS selected_text,body,"
                "NULL::text AS label,highlight_id"
            ),
            "bookmark": (
                "NULL::text AS color,NULL::text AS selected_text,NULL::text AS body,"
                "label,NULL::uuid AS highlight_id"
            ),
        }
        column = columns[kind]
        table = tables[kind]
        async with self._transaction(owner_id) as conn:
            book = await self._reader_mark_book(conn, owner_id, book_id)
            if book is None:
                return None
            row = await (
                await conn.execute(
                    f"""UPDATE public.{table} SET {column}=%s,version=version+1,updated_at=now()
                        WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                          AND id=%s AND deleted_at IS NULL
                        RETURNING id,anchor,{returning[kind]},version,created_at,updated_at""",
                    (value, owner_id.value, book_id, book["incarnation"], mark_id),
                )
            ).fetchone()
        if row is None:
            return None
        payload = self._json_row(row)
        payload["kind"] = kind
        return payload

    async def delete_reader_mark(
        self,
        owner_id: OwnerId,
        book_id: uuid.UUID,
        mark_id: uuid.UUID,
        *,
        kind: str,
    ) -> bool:
        tables = {"highlight": "highlights", "annotation": "annotations", "bookmark": "bookmarks"}
        async with self._transaction(owner_id) as conn:
            book = await self._reader_mark_book(conn, owner_id, book_id)
            if book is None:
                return False
            row = await (
                await conn.execute(
                    f"""UPDATE public.{tables[kind]} SET deleted_at=now(),updated_at=now(),
                               version=version+1
                        WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                          AND id=%s AND deleted_at IS NULL RETURNING id""",
                    (owner_id.value, book_id, book["incarnation"], mark_id),
                )
            ).fetchone()
        return row is not None

    async def update_position(
        self,
        owner_id: OwnerId,
        book_id: uuid.UUID,
        *,
        cfi: str,
        offset: int,
        completed_chapter: int,
        expected_epoch: int,
        base_version: int,
        client_id: uuid.UUID,
        client_sequence: int,
    ) -> dict:
        async with self._transaction(owner_id) as conn:
            state = await (
                await conn.execute(
                    """SELECT state.book_incarnation,state.bookmark,state.current_offset,
                              state.high_water_offset,state.position_epoch,state.position_version,
                              state.last_client_id,state.last_client_sequence
                       FROM public.reading_state AS state
                       JOIN public.books AS book
                         ON (book.owner_id,book.id,book.incarnation)
                          =(state.owner_id,state.book_id,state.book_incarnation)
                       WHERE state.owner_id=%s AND state.book_id=%s
                         AND book.deleted_at IS NULL
                       FOR UPDATE OF state""",
                    (owner_id.value, book_id),
                )
            ).fetchone()
            if state is None:
                raise MissingTenantResourceError
            if state["position_epoch"] != expected_epoch:
                raise StalePositionEpochError
            if base_version > state["position_version"]:
                raise FuturePositionVersionError
            maximum = await (
                await conn.execute(
                    """SELECT COALESCE(max(revealed_at),0) AS maximum
                       FROM public.chapters
                       WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                         AND retracted_at IS NULL""",
                    (owner_id.value, book_id, state["book_incarnation"]),
                )
            ).fetchone()
            if completed_chapter > maximum["maximum"]:
                raise InvalidPositionError("completed chapter is outside the current book")

            stale_base = base_version < state["position_version"]
            incoming_writer = (client_id.hex, client_sequence)
            current_writer = (
                state["last_client_id"].hex if state["last_client_id"] else "",
                state["last_client_sequence"] or 0,
            )
            advances = (
                completed_chapter > state["bookmark"]
                or offset > state["high_water_offset"]
            )
            wins_tie = (
                completed_chapter == state["bookmark"]
                and offset == state["high_water_offset"]
                and incoming_writer > current_writer
            )
            applied = not stale_base or advances or wins_tie
            if not stale_base:
                conflict = None
            elif advances:
                conflict = "merged_advance"
            elif wins_tie:
                conflict = "tie_won"
            elif completed_chapter < state["bookmark"] or offset < state["high_water_offset"]:
                conflict = "stale_behind"
            else:
                conflict = "tie_lost"

            if applied:
                await conn.execute(
                    """UPDATE public.reading_state
                       SET bookmark=GREATEST(bookmark,%s),
                           current_cfi=CASE WHEN NOT %s OR %s>=current_offset THEN %s
                                            ELSE current_cfi END,
                           current_offset=CASE WHEN %s THEN GREATEST(current_offset,%s)
                                               ELSE %s END,
                           high_water_cfi=CASE WHEN %s>=high_water_offset THEN %s
                                               ELSE high_water_cfi END,
                           high_water_offset=GREATEST(high_water_offset,%s),
                           position_version=position_version+1,
                           last_client_id=%s,last_client_sequence=%s,
                           updated_at=now(),last_opened_at=now()
                       WHERE owner_id=%s AND book_id=%s""",
                    (
                        completed_chapter,
                        stale_base,
                        offset,
                        cfi,
                        stale_base,
                        offset,
                        offset,
                        offset,
                        cfi,
                        offset,
                        client_id,
                        client_sequence,
                        owner_id.value,
                        book_id,
                    ),
                )
            else:
                await conn.execute(
                    """UPDATE public.reading_state SET last_opened_at=now()
                       WHERE owner_id=%s AND book_id=%s""",
                    (owner_id.value, book_id),
                )
            row = await self._select_position(conn, owner_id, book_id)
        return self._position_payload(row, applied=applied, conflict=conflict)

    async def reset_position(
        self, owner_id: OwnerId, book_id: uuid.UUID, expected_epoch: int
    ) -> dict:
        async with self._transaction(owner_id) as conn:
            current = await (
                await conn.execute(
                    """
                    SELECT state.position_epoch
                    FROM public.reading_state AS state
                    JOIN public.books AS book
                      ON (book.owner_id,book.id,book.incarnation)
                       =(state.owner_id,state.book_id,state.book_incarnation)
                    WHERE state.owner_id=%s AND state.book_id=%s AND book.deleted_at IS NULL
                    FOR UPDATE OF state
                    """,
                    (owner_id.value, book_id),
                )
            ).fetchone()
            if current is None:
                raise MissingTenantResourceError
            if current["position_epoch"] != expected_epoch:
                raise StalePositionEpochError
            row = await (
                await conn.execute(
                    """
                    UPDATE public.reading_state
                    SET bookmark=0,high_water_cfi=NULL,current_cfi=NULL,
                        current_offset=0,high_water_offset=0,
                        position_epoch=position_epoch+1,position_version=position_version+1,
                        last_client_id=NULL,last_client_sequence=NULL,
                        updated_at=now(),last_opened_at=now()
                    WHERE owner_id=%s AND book_id=%s AND position_epoch=%s
                      AND position_epoch<9223372036854775807
                    RETURNING book_id
                    """,
                    (owner_id.value, book_id, expected_epoch),
                )
            ).fetchone()
            if row is None:
                raise StalePositionEpochError
            position = await self._select_position(conn, owner_id, book_id)
        return self._position_payload(position, applied=True, conflict=None)

    async def memory_snapshot(self, owner_id: OwnerId, book_id: uuid.UUID) -> dict | None:
        async with self._transaction(owner_id) as conn:
            scope = await (
                await conn.execute(
                    """
                    SELECT book.incarnation,state.bookmark
                    FROM public.books AS book
                    JOIN public.reading_state AS state
                      ON (state.owner_id,state.book_id,state.book_incarnation)
                       =(book.owner_id,book.id,book.incarnation)
                    WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL
                    """,
                    (owner_id.value, book_id),
                )
            ).fetchone()
            if scope is None:
                return None
            incarnation = scope["incarnation"]
            bookmark = scope["bookmark"]
            params = (owner_id.value, book_id, incarnation, bookmark, bookmark)

            entities = await (
                await conn.execute(
                    """
                    SELECT entity.id,entity.canonical_name AS name,entity.entity_type AS type,
                           entity.revealed_at
                    FROM public.entities AS entity
                    JOIN public.ingested_chapters AS receipt
                      ON (receipt.owner_id,receipt.book_id,receipt.book_incarnation,receipt.chapter_id)
                       =(entity.owner_id,entity.book_id,entity.book_incarnation,entity.source_chapter_id)
                    JOIN public.chapters AS chapter
                      ON (chapter.owner_id,chapter.book_id,chapter.book_incarnation,chapter.id)
                       =(receipt.owner_id,receipt.book_id,receipt.book_incarnation,receipt.chapter_id)
                    WHERE entity.owner_id=%s AND entity.book_id=%s AND entity.book_incarnation=%s
                      AND entity.revealed_at<=%s AND entity.retracted_at IS NULL
                      AND (entity.invalid_at IS NULL OR entity.invalid_at>%s)
                      AND receipt.retracted_at IS NULL AND chapter.retracted_at IS NULL
                    ORDER BY entity.revealed_at,entity.id
                    """,
                    params,
                )
            ).fetchall()
            edges = await (
                await conn.execute(
                    """
                    SELECT edge.id,edge.src_entity_id,edge.dst_entity_id,
                           edge.relationship_type AS type,edge.label,edge.revealed_at
                    FROM public.edges AS edge
                    JOIN public.entities AS src
                      ON (src.owner_id,src.book_id,src.book_incarnation,src.id)
                       =(edge.owner_id,edge.book_id,edge.book_incarnation,edge.src_entity_id)
                    JOIN public.entities AS dst
                      ON (dst.owner_id,dst.book_id,dst.book_incarnation,dst.id)
                       =(edge.owner_id,edge.book_id,edge.book_incarnation,edge.dst_entity_id)
                    JOIN public.ingested_chapters AS receipt
                      ON (receipt.owner_id,receipt.book_id,receipt.book_incarnation,receipt.chapter_id)
                       =(edge.owner_id,edge.book_id,edge.book_incarnation,edge.source_chapter_id)
                    JOIN public.chapters AS chapter
                      ON (chapter.owner_id,chapter.book_id,chapter.book_incarnation,chapter.id)
                       =(receipt.owner_id,receipt.book_id,receipt.book_incarnation,receipt.chapter_id)
                    WHERE edge.owner_id=%s AND edge.book_id=%s AND edge.book_incarnation=%s
                      AND edge.revealed_at<=%s AND edge.retracted_at IS NULL
                      AND (edge.invalid_at IS NULL OR edge.invalid_at>%s)
                      AND src.revealed_at<=%s AND src.retracted_at IS NULL
                      AND (src.invalid_at IS NULL OR src.invalid_at>%s)
                      AND dst.revealed_at<=%s AND dst.retracted_at IS NULL
                      AND (dst.invalid_at IS NULL OR dst.invalid_at>%s)
                      AND receipt.retracted_at IS NULL AND chapter.retracted_at IS NULL
                    ORDER BY edge.revealed_at,edge.id
                    """,
                    (*params, bookmark, bookmark, bookmark, bookmark),
                )
            ).fetchall()
            facts = {}
            for name, table, columns in (
                ("events", "events", "fact.id,fact.summary,fact.kind,fact.revealed_at"),
                ("themes", "themes", "fact.id,fact.name,fact.description,fact.revealed_at"),
                (
                    "summaries",
                    "chapter_summaries",
                    "fact.id,fact.summary,fact.kind,fact.revealed_at",
                ),
            ):
                rows = await (
                    await conn.execute(
                        f"""
                        SELECT {columns}
                        FROM public.{table} AS fact
                        JOIN public.ingested_chapters AS receipt
                          ON (receipt.owner_id,receipt.book_id,receipt.book_incarnation,receipt.chapter_id)
                           =(fact.owner_id,fact.book_id,fact.book_incarnation,fact.source_chapter_id)
                        JOIN public.chapters AS chapter
                          ON (chapter.owner_id,chapter.book_id,chapter.book_incarnation,chapter.id)
                           =(receipt.owner_id,receipt.book_id,receipt.book_incarnation,receipt.chapter_id)
                        WHERE fact.owner_id=%s AND fact.book_id=%s AND fact.book_incarnation=%s
                          AND fact.revealed_at<=%s AND fact.retracted_at IS NULL
                          AND (fact.invalid_at IS NULL OR fact.invalid_at>%s)
                          AND receipt.retracted_at IS NULL AND chapter.retracted_at IS NULL
                        ORDER BY fact.revealed_at,fact.id
                        """,
                        params,
                    )
                ).fetchall()
                facts[name] = [self._json_row(row) for row in rows]

        return {
            "bookmark": bookmark,
            "entities": [self._json_row(row) for row in entities],
            "relationships": [self._json_row(row) for row in edges],
            **facts,
        }

    @staticmethod
    async def _correction_history_rows(conn, owner_id, book_id, incarnation, bookmark):
        rows = await (
            await conn.execute(
                """
                SELECT id,correction_kind,source_entity_ids,target_entity_ids,reason,
                       revealed_at,recorded_at
                FROM public.entity_corrections
                WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                  AND revealed_at<=%s AND retracted_at IS NULL
                ORDER BY revealed_at,id
                """,
                (owner_id.value, book_id, incarnation, bookmark),
            )
        ).fetchall()
        entity_ids = {
            uuid.UUID(str(value))
            for row in rows
            for values in (row["source_entity_ids"], row["target_entity_ids"])
            for value in values
        }
        names = {}
        if entity_ids:
            entities = await (
                await conn.execute(
                    """
                    SELECT id,canonical_name FROM public.entities
                    WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                      AND id=ANY(%s::uuid[]) AND retracted_at IS NULL
                    """,
                    (owner_id.value, book_id, incarnation, list(entity_ids)),
                )
            ).fetchall()
            names = {str(row["id"]): row["canonical_name"] for row in entities}
        return [
            {
                "correction_id": str(row["id"]),
                "kind": row["correction_kind"],
                "effective_at": row["revealed_at"],
                "source_entities": [
                    {"entity_id": str(value), "name": names.get(str(value), "Earlier memory")}
                    for value in row["source_entity_ids"]
                ],
                "target_entities": [
                    {"entity_id": str(value), "name": names.get(str(value), "Corrected memory")}
                    for value in row["target_entity_ids"]
                ],
                "reason": row["reason"],
                "recorded_at": row["recorded_at"].isoformat(),
            }
            for row in rows
        ]

    async def memory_corrections(
        self, owner_id: OwnerId, book_id: uuid.UUID, requested_bookmark: int | None = None
    ) -> dict | None:
        async with self._transaction(owner_id) as conn:
            scope = await (
                await conn.execute(
                    """
                    SELECT book.incarnation,state.bookmark
                    FROM public.books AS book
                    JOIN public.reading_state AS state
                      ON (state.owner_id,state.book_id,state.book_incarnation)
                       =(book.owner_id,book.id,book.incarnation)
                    WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL
                    """,
                    (owner_id.value, book_id),
                )
            ).fetchone()
            if scope is None:
                return None
            bookmark = scope["bookmark"]
            if requested_bookmark is not None:
                bookmark = min(bookmark, requested_bookmark)
            items = await self._correction_history_rows(
                conn, owner_id, book_id, scope["incarnation"], bookmark
            ) if bookmark > 0 else []
        return {"as_of_chapter": bookmark, "items": items}

    async def replace_memory_entity(
        self,
        owner_id: OwnerId,
        book_id: uuid.UUID,
        *,
        source_entity_id: uuid.UUID,
        canonical_name: str,
        reason: str,
        bookmark: int,
    ) -> dict | None:
        """Atomically publish a reader correction without rewriting earlier story time."""
        async with self._transaction(owner_id) as conn:
            scope = await (
                await conn.execute(
                    """
                    SELECT book.incarnation,state.bookmark,receipt.chapter_id AS source_chapter_id
                    FROM public.books AS book
                    JOIN public.reading_state AS state
                      ON (state.owner_id,state.book_id,state.book_incarnation)
                       =(book.owner_id,book.id,book.incarnation)
                    LEFT JOIN public.chapters AS chapter
                      ON (chapter.owner_id,chapter.book_id,chapter.book_incarnation)
                       =(book.owner_id,book.id,book.incarnation)
                     AND chapter.revealed_at=state.bookmark AND chapter.retracted_at IS NULL
                    LEFT JOIN public.ingested_chapters AS receipt
                      ON (receipt.owner_id,receipt.book_id,receipt.book_incarnation,receipt.chapter_id)
                       =(chapter.owner_id,chapter.book_id,chapter.book_incarnation,chapter.id)
                     AND receipt.retracted_at IS NULL
                    WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL
                    FOR UPDATE OF state
                    """,
                    (owner_id.value, book_id),
                )
            ).fetchone()
            if scope is None:
                return None
            if scope["bookmark"] != bookmark or scope["source_chapter_id"] is None:
                raise ValueError("reading progress changed; reopen the codex and try again")
            incarnation = scope["incarnation"]
            params = (owner_id.value, book_id, incarnation)
            source = await (
                await conn.execute(
                    """
                    SELECT id,canonical_name,entity_type,revealed_at
                    FROM public.entities
                    WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s AND id=%s
                      AND revealed_at<%s AND retracted_at IS NULL
                      AND (invalid_at IS NULL OR invalid_at>%s)
                    FOR UPDATE
                    """,
                    (*params, source_entity_id, bookmark, bookmark),
                )
            ).fetchone()
            if source is None:
                raise ValueError("that memory cannot be corrected at this reading point")
            if source["canonical_name"].strip().casefold() == canonical_name.casefold():
                raise ValueError("corrected name must differ from the current name")
            collision = await (
                await conn.execute(
                    """
                    SELECT 1 FROM public.entities
                    WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s AND id<>%s
                      AND lower(canonical_name)=lower(%s) AND revealed_at<=%s
                      AND retracted_at IS NULL AND (invalid_at IS NULL OR invalid_at>%s)
                    """,
                    (*params, source_entity_id, canonical_name, bookmark, bookmark),
                )
            ).fetchone()
            if collision is not None:
                raise ValueError("that name already belongs to another visible memory")

            aliases = await (
                await conn.execute(
                    """SELECT surface_form FROM public.aliases
                       WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s AND entity_id=%s
                         AND revealed_at<=%s AND retracted_at IS NULL
                         AND (invalid_at IS NULL OR invalid_at>%s) ORDER BY id""",
                    (*params, source_entity_id, bookmark, bookmark),
                )
            ).fetchall()
            edges = await (
                await conn.execute(
                    """
                    SELECT edge.id,edge.src_entity_id,edge.dst_entity_id,
                           edge.relationship_type,edge.label,edge.invalid_at
                    FROM public.edges AS edge
                    JOIN public.entities AS src
                      ON (src.owner_id,src.book_id,src.book_incarnation,src.id)
                       =(edge.owner_id,edge.book_id,edge.book_incarnation,edge.src_entity_id)
                    JOIN public.entities AS dst
                      ON (dst.owner_id,dst.book_id,dst.book_incarnation,dst.id)
                       =(edge.owner_id,edge.book_id,edge.book_incarnation,edge.dst_entity_id)
                    WHERE edge.owner_id=%s AND edge.book_id=%s AND edge.book_incarnation=%s
                      AND (edge.src_entity_id=%s OR edge.dst_entity_id=%s)
                      AND edge.revealed_at<=%s AND edge.retracted_at IS NULL
                      AND (edge.invalid_at IS NULL OR edge.invalid_at>%s)
                      AND src.revealed_at<=%s AND src.retracted_at IS NULL
                      AND (src.invalid_at IS NULL OR src.invalid_at>%s)
                      AND dst.revealed_at<=%s AND dst.retracted_at IS NULL
                      AND (dst.invalid_at IS NULL OR dst.invalid_at>%s)
                    ORDER BY edge.id
                    """,
                    (*params, source_entity_id, source_entity_id, bookmark, bookmark,
                     bookmark, bookmark, bookmark, bookmark),
                )
            ).fetchall()
            participants = await (
                await conn.execute(
                    """
                    SELECT participant.event_id,participant.role
                    FROM public.event_participants AS participant
                    JOIN public.events AS event
                      ON (event.owner_id,event.book_id,event.book_incarnation,event.id)
                       =(participant.owner_id,participant.book_id,participant.book_incarnation,
                         participant.event_id)
                    WHERE participant.owner_id=%s AND participant.book_id=%s
                      AND participant.book_incarnation=%s AND participant.entity_id=%s
                      AND participant.revealed_at<=%s AND participant.retracted_at IS NULL
                      AND (participant.invalid_at IS NULL OR participant.invalid_at>%s)
                      AND event.revealed_at<=%s AND event.retracted_at IS NULL
                      AND (event.invalid_at IS NULL OR event.invalid_at>%s)
                    ORDER BY participant.event_id
                    """,
                    (*params, source_entity_id, bookmark, bookmark, bookmark, bookmark),
                )
            ).fetchall()
            state = await (
                await conn.execute(
                    """
                    SELECT status FROM public.entity_state
                    WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s AND entity_id=%s
                      AND revealed_at<=%s AND retracted_at IS NULL
                      AND (invalid_at IS NULL OR invalid_at>%s)
                    ORDER BY revealed_at DESC,id DESC LIMIT 1
                    """,
                    (*params, source_entity_id, bookmark, bookmark),
                )
            ).fetchone()

            target_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO public.entities
                  (owner_id,book_id,book_incarnation,id,source_chapter_id,canonical_name,
                   entity_type,revealed_at,schema_version,extractor_version)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,'lit59-reader-correction')
                """,
                (*params, target_id, scope["source_chapter_id"], canonical_name,
                 source["entity_type"], bookmark),
            )
            seen = {canonical_name.casefold()}
            for alias in [source["canonical_name"], *(row["surface_form"] for row in aliases)]:
                if alias.casefold() in seen:
                    continue
                seen.add(alias.casefold())
                await conn.execute(
                    """
                    INSERT INTO public.aliases
                      (owner_id,book_id,book_incarnation,entity_id,source_chapter_id,surface_form,
                       revealed_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (*params, target_id, scope["source_chapter_id"], alias, bookmark),
                )
            if state is not None:
                await conn.execute(
                    """
                    INSERT INTO public.entity_state
                      (owner_id,book_id,book_incarnation,entity_id,source_chapter_id,status,
                       revealed_at,schema_version,extractor_version)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,1,'lit59-reader-correction')
                    """,
                    (*params, target_id, scope["source_chapter_id"],
                     psycopg.types.json.Jsonb(state["status"]), bookmark),
                )
            for edge in edges:
                await conn.execute(
                    """
                    INSERT INTO public.edges
                      (owner_id,book_id,book_incarnation,source_chapter_id,src_entity_id,
                       dst_entity_id,relationship_type,label,revealed_at,invalid_at,schema_version,
                       extractor_version)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,'lit59-reader-correction')
                    """,
                    (*params, scope["source_chapter_id"],
                     target_id if edge["src_entity_id"] == source_entity_id else edge["src_entity_id"],
                     target_id if edge["dst_entity_id"] == source_entity_id else edge["dst_entity_id"],
                     edge["relationship_type"], edge["label"], bookmark, edge["invalid_at"]),
                )
            for participant in participants:
                await conn.execute(
                    """
                    INSERT INTO public.event_participants
                      (owner_id,book_id,book_incarnation,event_id,entity_id,source_chapter_id,role,
                       revealed_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (*params, participant["event_id"], target_id,
                     scope["source_chapter_id"], participant["role"], bookmark),
                )
            await conn.execute(
                """UPDATE public.entities SET invalid_at=%s
                   WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s AND id=%s""",
                (bookmark, *params, source_entity_id),
            )
            correction_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO public.entity_corrections
                  (owner_id,book_id,book_incarnation,id,source_chapter_id,correction_kind,
                   source_entity_ids,target_entity_ids,assignments,reason,revealed_at,schema_version)
                VALUES (%s,%s,%s,%s,%s,'replace',%s,%s,%s,%s,%s,1)
                """,
                (*params, correction_id, scope["source_chapter_id"],
                 psycopg.types.json.Jsonb([str(source_entity_id)]),
                 psycopg.types.json.Jsonb([str(target_id)]),
                 psycopg.types.json.Jsonb({
                     "previous_name": source["canonical_name"],
                     "corrected_name": canonical_name,
                     "copied_edge_ids": [str(row["id"]) for row in edges],
                     "copied_event_ids": [str(row["event_id"]) for row in participants],
                 }), reason, bookmark),
            )
            await record_event_async(
                conn,
                owner_id=owner_id.value,
                actor_kind="owner",
                action="memory.correct",
                target_kind="entity_correction",
                target_id=correction_id,
                result="succeeded",
            )
            items = await self._correction_history_rows(
                conn, owner_id, book_id, incarnation, bookmark
            )
        return {
            "as_of_chapter": bookmark,
            "correction_id": str(correction_id),
            "target_entity_id": str(target_id),
            "items": items,
        }

    async def list_costs(self, owner_id: OwnerId, book_id: uuid.UUID | None = None) -> dict | None:
        async with self._transaction(owner_id) as conn:
            if book_id is not None:
                exists = await (
                    await conn.execute(
                        "SELECT 1 FROM public.books WHERE owner_id=%s AND id=%s "
                        "AND deleted_at IS NULL",
                        (owner_id.value, book_id),
                    )
                ).fetchone()
                if exists is None:
                    return None
            rows = await (
                await conn.execute(
                    """
                    SELECT id,book_id,phase,provider,model,input_tokens,output_tokens,usd,recorded_at
                    FROM public.cost_ledger
                    WHERE owner_id=%s AND (%s::uuid IS NULL OR book_id=%s::uuid)
                    ORDER BY recorded_at,id
                    """,
                    (owner_id.value, book_id, book_id),
                )
            ).fetchall()
        payload = [self._json_row(row) for row in rows]
        return {
            "items": payload,
            "total_input_tokens": sum(item["input_tokens"] or 0 for item in payload),
            "total_output_tokens": sum(item["output_tokens"] or 0 for item in payload),
            "total_usd": str(sum((row["usd"] or 0 for row in rows), start=0)),
        }

    async def create_uploaded_book(
        self,
        owner_id: OwnerId,
        *,
        book_id: uuid.UUID,
        incarnation: uuid.UUID,
        title: str,
        author: str | None,
        file_hash: str,
        content_language: str,
        book_type: str,
        object_id: uuid.UUID,
        storage_provider: str,
        media_type: str,
        byte_size: int,
        encryption: str,
        chapter_count: int,
        search_documents: list[dict] | None = None,
    ) -> dict:
        job_id = uuid.uuid4()
        async with self._transaction(owner_id) as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text,47))",
                (owner_id.value,),
            )
            limits = await (
                await conn.execute(
                    """
                    SELECT max_upload_bytes,max_library_bytes,max_books,max_active_jobs
                    FROM public.owner_limits WHERE owner_id=%s
                    """,
                    (owner_id.value,),
                )
            ).fetchone()
            if limits is None:
                raise RuntimeError("owner limit policy is unavailable")
            usage = await (
                await conn.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM public.books
                       WHERE owner_id=%s AND deleted_at IS NULL) AS books,
                      COALESCE((SELECT sum(byte_size) FROM public.source_objects
                       WHERE owner_id=%s AND deleted_at IS NULL),0) AS library_bytes,
                      (SELECT count(*) FROM public.jobs WHERE owner_id=%s AND state IN
                       ('waiting_configuration','pending','leased','running')) AS active_jobs
                    """,
                    (owner_id.value, owner_id.value, owner_id.value),
                )
            ).fetchone()
            assert usage is not None
            if byte_size > limits["max_upload_bytes"]:
                raise LimitExceededError(
                    "upload_size_exceeded",
                    limits["max_upload_bytes"],
                    None,
                    "Choose a smaller EPUB or ask an operator to raise the upload limit.",
                )
            if usage["books"] >= limits["max_books"]:
                raise LimitExceededError(
                    "book_quota_exceeded",
                    limits["max_books"],
                    None,
                    "Delete an existing book or ask an operator to raise the book limit.",
                )
            if usage["library_bytes"] + byte_size > limits["max_library_bytes"]:
                raise LimitExceededError(
                    "library_storage_exceeded",
                    limits["max_library_bytes"],
                    None,
                    "Delete stored books or ask an operator to raise the storage limit.",
                )
            if usage["active_jobs"] >= limits["max_active_jobs"]:
                raise LimitExceededError(
                    "active_job_limit_exceeded",
                    limits["max_active_jobs"],
                    15,
                    "Wait for an active ingestion job to finish or cancel one before retrying.",
                )
            extraction = await (
                await conn.execute(
                    """
                    SELECT credential_id,validation_status
                    FROM public.provider_model_settings
                    WHERE owner_id=%s AND capability='extraction' AND enabled
                    """,
                    (owner_id.value,),
                )
            ).fetchone()
            ready_credential = (
                extraction["credential_id"]
                if extraction is not None and extraction["validation_status"] == "ready"
                else None
            )
            initial_job_state = "pending" if ready_credential is not None else "waiting_configuration"
            row = await (
                await conn.execute(
                    """
                    INSERT INTO public.books
                      (owner_id,id,incarnation,title,author,source_kind,file_hash,schema_version,
                       content_language,book_type)
                    VALUES (%s,%s,%s,%s,%s,'upload',%s,1,%s,%s)
                    RETURNING id,title,author,content_language,book_type,created_at
                    """,
                    (
                        owner_id.value,
                        book_id,
                        incarnation,
                        title,
                        author,
                        file_hash,
                        content_language,
                        book_type,
                    ),
                )
            ).fetchone()
            await conn.execute(
                """
                INSERT INTO public.reading_state
                  (owner_id,book_id,book_incarnation,bookmark,position_epoch)
                VALUES (%s,%s,%s,0,0)
                """,
                (owner_id.value, book_id, incarnation),
            )
            await conn.execute(
                """
                INSERT INTO public.source_objects
                  (owner_id,id,book_id,book_incarnation,storage_provider,storage_key,media_type,
                   byte_size,sha256,encryption_key_id,verified_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                """,
                (
                    owner_id.value,
                    object_id,
                    book_id,
                    incarnation,
                    storage_provider,
                    object_id.hex,
                    media_type,
                    byte_size,
                    file_hash,
                    encryption,
                ),
            )
            if search_documents:
                await conn.execute(
                    """INSERT INTO public.book_search_documents
                         (owner_id,book_id,book_incarnation,ordinal,href,title,part_label,content,
                          char_start,char_end)
                       SELECT %s,%s,%s,document.ordinal,document.href,document.title,
                              document.part_label,document.content,document.char_start,
                              document.char_end
                       FROM jsonb_to_recordset(%s::jsonb) AS document(
                         ordinal integer,href text,title text,part_label text,content text,
                         char_start bigint,char_end bigint
                       )""",
                    (
                        owner_id.value,
                        book_id,
                        incarnation,
                        psycopg.types.json.Jsonb(search_documents),
                    ),
                )
            job_row = await (
                await conn.execute(
                    """
                    INSERT INTO public.jobs
                      (owner_id,id,book_id,book_incarnation,credential_id,kind,state,
                       idempotency_key,payload_metadata)
                    VALUES (%s,%s,%s,%s,%s,'ingest_book',%s,%s,
                            jsonb_build_object('chapter_count',%s))
                    RETURNING id,book_id,kind,state,attempt_count,max_attempts,run_after,
                              cancellation_requested_at,sanitized_error,created_at,updated_at,
                              completed_at
                    """,
                    (
                        owner_id.value,
                        job_id,
                        book_id,
                        incarnation,
                        ready_credential,
                        initial_job_state,
                        f"ingest:{book_id.hex}:{incarnation.hex}",
                        chapter_count,
                    ),
                )
            ).fetchone()
            job_row["completed_chapters"] = 0
            job_row["total_chapters"] = chapter_count
            await record_event_async(
                conn,
                owner_id=owner_id.value,
                actor_kind="owner",
                action="book.import",
                target_kind="book",
                target_id=book_id,
                result="succeeded",
            )
        payload = self._book_payload(row)
        payload.update(bookmark=0, position_epoch=0)
        payload["job"] = self._job_payload(job_row)
        return payload

    async def list_jobs(self, owner_id: OwnerId) -> list[dict]:
        async with self._transaction(owner_id) as conn:
            rows = await (
                await conn.execute(
                    """
                    SELECT job.id,job.book_id,job.kind,job.state,job.attempt_count,
                           job.max_attempts,job.run_after,job.cancellation_requested_at,
                           job.sanitized_error,job.created_at,job.updated_at,job.completed_at,
                           COALESCE((job.payload_metadata->>'chapter_count')::integer,0)
                             AS total_chapters,
                           (SELECT count(*) FROM public.ingested_chapters AS receipt
                            WHERE receipt.owner_id=job.owner_id
                              AND receipt.book_id=job.book_id
                              AND receipt.book_incarnation=job.book_incarnation)
                             AS completed_chapters
                    FROM public.jobs AS job
                    WHERE job.owner_id=%s
                    ORDER BY job.created_at DESC,job.id DESC
                    """,
                    (owner_id.value,),
                )
            ).fetchall()
        return [self._job_payload(row) for row in rows]

    async def get_job(self, owner_id: OwnerId, job_id: uuid.UUID) -> dict | None:
        async with self._transaction(owner_id) as conn:
            row = await (
                await conn.execute(
                    """
                    SELECT job.id,job.book_id,job.kind,job.state,job.attempt_count,
                           job.max_attempts,job.run_after,job.cancellation_requested_at,
                           job.sanitized_error,job.created_at,job.updated_at,job.completed_at,
                           COALESCE((job.payload_metadata->>'chapter_count')::integer,0)
                             AS total_chapters,
                           (SELECT count(*) FROM public.ingested_chapters AS receipt
                            WHERE receipt.owner_id=job.owner_id
                              AND receipt.book_id=job.book_id
                              AND receipt.book_incarnation=job.book_incarnation)
                             AS completed_chapters
                    FROM public.jobs AS job
                    WHERE job.owner_id=%s AND job.id=%s
                    """,
                    (owner_id.value, job_id),
                )
            ).fetchone()
        return self._job_payload(row) if row else None

    async def cancel_job(self, owner_id: OwnerId, job_id: uuid.UUID) -> dict | None:
        async with self._transaction(owner_id) as conn:
            row = await (
                await conn.execute(
                    """
                    SELECT id,state FROM public.jobs
                    WHERE owner_id=%s AND id=%s
                    FOR UPDATE
                    """,
                    (owner_id.value, job_id),
                )
            ).fetchone()
            if row is None:
                await record_event_async(
                    conn,
                    owner_id=owner_id.value,
                    actor_kind="owner",
                    action="job.cancel",
                    target_kind="job",
                    target_id=job_id,
                    result="denied",
                    reason_code="not_found",
                )
                return None
            if row["state"] in {"waiting_configuration", "pending"}:
                await conn.execute(
                    """
                    UPDATE public.jobs
                    SET state='cancelled',cancellation_requested_at=COALESCE(
                          cancellation_requested_at,now()),completed_at=now(),updated_at=now()
                    WHERE owner_id=%s AND id=%s
                    """,
                    (owner_id.value, job_id),
                )
            elif row["state"] in {"leased", "running"}:
                await conn.execute(
                    """
                    UPDATE public.jobs
                    SET cancellation_requested_at=COALESCE(cancellation_requested_at,now()),
                        updated_at=now()
                    WHERE owner_id=%s AND id=%s
                    """,
                    (owner_id.value, job_id),
                )
            result = await (
                await conn.execute(
                    """
                    SELECT job.id,job.book_id,job.kind,job.state,job.attempt_count,
                           job.max_attempts,job.run_after,job.cancellation_requested_at,
                           job.sanitized_error,job.created_at,job.updated_at,job.completed_at,
                           COALESCE((job.payload_metadata->>'chapter_count')::integer,0)
                             AS total_chapters,
                           (SELECT count(*) FROM public.ingested_chapters AS receipt
                            WHERE receipt.owner_id=job.owner_id
                              AND receipt.book_id=job.book_id
                              AND receipt.book_incarnation=job.book_incarnation)
                             AS completed_chapters
                    FROM public.jobs AS job WHERE job.owner_id=%s AND job.id=%s
                    """,
                    (owner_id.value, job_id),
                )
            ).fetchone()
            await record_event_async(
                conn,
                owner_id=owner_id.value,
                actor_kind="owner",
                action="job.cancel",
                target_kind="job",
                target_id=job_id,
                result="succeeded",
            )
        return self._job_payload(result)

    async def create_credential(
        self, owner_id: OwnerId, envelope: EncryptedCredential
    ) -> dict:
        if envelope.owner_id != owner_id.value:
            raise ValueError("credential envelope owner does not match repository scope")
        async with self._transaction(owner_id) as conn:
            row = await (
                await conn.execute(
                    """
                    INSERT INTO public.provider_credentials
                      (owner_id,id,provider,masked_label,ciphertext,encrypted_data_key,
                       encryption_algorithm,key_version,nonce,metadata)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb)
                    RETURNING id,provider,masked_label,key_version,created_at,rotated_at,disabled_at
                    """,
                    (
                        owner_id.value,
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
            ).fetchone()
            await record_event_async(
                conn,
                owner_id=owner_id.value,
                actor_kind="owner",
                action="credential.create",
                target_kind="credential",
                target_id=envelope.credential_id,
                result="succeeded",
            )
        assert row is not None
        return self._json_row(row)

    async def list_credentials(self, owner_id: OwnerId) -> list[dict]:
        async with self._transaction(owner_id) as conn:
            rows = await (
                await conn.execute(
                    """
                    SELECT id,provider,masked_label,key_version,created_at,rotated_at,disabled_at
                    FROM public.provider_credentials
                    WHERE owner_id=%s AND deleted_at IS NULL
                    ORDER BY created_at,id
                    """,
                    (owner_id.value,),
                )
            ).fetchall()
        return [self._json_row(row) for row in rows]

    async def get_credential(self, owner_id: OwnerId, credential_id: uuid.UUID) -> dict | None:
        async with self._transaction(owner_id) as conn:
            row = await (
                await conn.execute(
                    """
                    SELECT id,provider,masked_label,key_version,created_at,rotated_at,disabled_at
                    FROM public.provider_credentials
                    WHERE owner_id=%s AND id=%s AND deleted_at IS NULL
                    """,
                    (owner_id.value, credential_id),
                )
            ).fetchone()
        return None if row is None else self._json_row(row)

    async def replace_credential(
        self, owner_id: OwnerId, envelope: EncryptedCredential
    ) -> dict | None:
        if envelope.owner_id != owner_id.value:
            raise ValueError("credential envelope owner does not match repository scope")
        async with self._transaction(owner_id) as conn:
            row = await (
                await conn.execute(
                    """
                    UPDATE public.provider_credentials
                    SET masked_label=%s,ciphertext=%s,encrypted_data_key=%s,
                        encryption_algorithm=%s,key_version=%s,nonce=%s,metadata='{}'::jsonb,
                        rotated_at=now(),disabled_at=NULL
                    WHERE owner_id=%s AND id=%s AND provider=%s AND deleted_at IS NULL
                    RETURNING id,provider,masked_label,key_version,created_at,rotated_at,disabled_at
                    """,
                    (
                        envelope.masked_label,
                        envelope.ciphertext,
                        envelope.encrypted_data_key,
                        envelope.encryption_algorithm,
                        envelope.key_version,
                        envelope.nonce,
                        owner_id.value,
                        envelope.credential_id,
                        envelope.provider,
                    ),
                )
            ).fetchone()
            if row is not None:
                await conn.execute(
                    """
                    UPDATE public.provider_model_settings
                    SET validation_status='unchecked',validation_error_code=NULL,
                        validated_at=NULL,updated_at=now()
                    WHERE owner_id=%s AND credential_id=%s
                    """,
                    (owner_id.value, envelope.credential_id),
                )
                await conn.execute(
                    """
                    UPDATE public.jobs
                    SET state='waiting_configuration',credential_id=NULL,updated_at=now()
                    WHERE owner_id=%s AND credential_id=%s AND state='pending'
                    """,
                    (owner_id.value, envelope.credential_id),
                )
            await record_event_async(
                conn,
                owner_id=owner_id.value,
                actor_kind="owner",
                action="credential.replace",
                target_kind="credential",
                target_id=envelope.credential_id,
                result="succeeded" if row is not None else "denied",
                reason_code=None if row is not None else "not_found",
            )
        return None if row is None else self._json_row(row)

    async def delete_credential(self, owner_id: OwnerId, credential_id: uuid.UUID) -> bool:
        """Destroy the live envelope as part of logical deletion, not merely hide metadata."""
        async with self._transaction(owner_id) as conn:
            row = await (
                await conn.execute(
                    """
                    UPDATE public.provider_credentials
                    SET ciphertext='\\x00'::bytea,encrypted_data_key='\\x00'::bytea,
                        nonce=decode(repeat('00',12),'hex'),metadata='{}'::jsonb,
                        disabled_at=COALESCE(disabled_at,now()),deleted_at=now()
                    WHERE owner_id=%s AND id=%s AND deleted_at IS NULL
                    RETURNING id
                    """,
                    (owner_id.value, credential_id),
                )
            ).fetchone()
            if row is not None:
                await conn.execute(
                    """
                    UPDATE public.provider_model_settings
                    SET enabled=false,validation_status='unchecked',validation_error_code=NULL,
                        validated_at=NULL,updated_at=now()
                    WHERE owner_id=%s AND credential_id=%s
                    """,
                    (owner_id.value, credential_id),
                )
                await conn.execute(
                    """
                    UPDATE public.jobs
                    SET state='waiting_configuration',credential_id=NULL,updated_at=now()
                    WHERE owner_id=%s AND credential_id=%s AND state='pending'
                    """,
                    (owner_id.value, credential_id),
                )
            await record_event_async(
                conn,
                owner_id=owner_id.value,
                actor_kind="owner",
                action="credential.delete",
                target_kind="credential",
                target_id=credential_id,
                result="succeeded" if row is not None else "denied",
                reason_code=None if row is not None else "not_found",
            )
        return row is not None

    async def list_provider_settings(self, owner_id: OwnerId) -> list[dict]:
        async with self._transaction(owner_id) as conn:
            rows = await (
                await conn.execute(
                    """
                    SELECT id,provider,capability,credential_id,model,base_url,enabled,
                           validation_status,validation_error_code,validated_at,created_at,updated_at
                    FROM public.provider_model_settings
                    WHERE owner_id=%s
                    ORDER BY capability
                    """,
                    (owner_id.value,),
                )
            ).fetchall()
        return [self._json_row(row) for row in rows]

    async def get_provider_setting(self, owner_id: OwnerId, capability: str) -> dict | None:
        async with self._transaction(owner_id) as conn:
            row = await (
                await conn.execute(
                    """
                    SELECT id,provider,capability,credential_id,model,base_url,enabled,
                           validation_status,validation_error_code,validated_at,created_at,updated_at
                    FROM public.provider_model_settings
                    WHERE owner_id=%s AND capability=%s
                    """,
                    (owner_id.value, capability),
                )
            ).fetchone()
        return None if row is None else self._json_row(row)

    async def upsert_provider_setting(
        self,
        owner_id: OwnerId,
        *,
        capability: str,
        provider: str,
        credential_id: uuid.UUID | None,
        model: str,
        base_url: str | None,
    ) -> dict | None:
        async with self._transaction(owner_id) as conn:
            if provider != "offline":
                credential = await (
                    await conn.execute(
                        """
                        SELECT id FROM public.provider_credentials
                        WHERE owner_id=%s AND id=%s AND provider=%s
                          AND disabled_at IS NULL AND deleted_at IS NULL
                        FOR SHARE
                        """,
                        (owner_id.value, credential_id, provider),
                    )
                ).fetchone()
                if credential is None:
                    await record_event_async(
                        conn,
                        owner_id=owner_id.value,
                        actor_kind="owner",
                        action="provider_setting.update",
                        target_kind="provider_setting",
                        target_id=None,
                        result="denied",
                        reason_code="credential_unavailable",
                    )
                    return None
            status = "offline" if provider == "offline" else "unchecked"
            row = await (
                await conn.execute(
                    """
                    INSERT INTO public.provider_model_settings
                      (owner_id,id,provider,capability,credential_id,model,base_url,settings,
                       enabled,validation_status,validation_error_code,validated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb,true,%s,NULL,
                            CASE WHEN %s='offline' THEN now() ELSE NULL END)
                    ON CONFLICT (owner_id,capability) DO UPDATE SET
                      provider=EXCLUDED.provider,credential_id=EXCLUDED.credential_id,
                      model=EXCLUDED.model,base_url=EXCLUDED.base_url,settings='{}'::jsonb,
                      enabled=true,validation_status=EXCLUDED.validation_status,
                      validation_error_code=NULL,validated_at=EXCLUDED.validated_at,updated_at=now()
                    RETURNING id,provider,capability,credential_id,model,base_url,enabled,
                              validation_status,validation_error_code,validated_at,created_at,updated_at
                    """,
                    (
                        owner_id.value,
                        uuid.uuid4(),
                        provider,
                        capability,
                        credential_id,
                        model,
                        base_url,
                        status,
                        status,
                    ),
                )
            ).fetchone()
            if capability == "extraction":
                await conn.execute(
                    """
                    UPDATE public.jobs
                    SET state='waiting_configuration',credential_id=NULL,updated_at=now()
                    WHERE owner_id=%s AND state='pending'
                    """,
                    (owner_id.value,),
                )
            await record_event_async(
                conn,
                owner_id=owner_id.value,
                actor_kind="owner",
                action="provider_setting.update",
                target_kind="provider_setting",
                target_id=row["id"],
                result="succeeded",
            )
        assert row is not None
        return self._json_row(row)

    async def record_provider_validation(
        self,
        owner_id: OwnerId,
        *,
        setting_id: uuid.UUID,
        expected_updated_at: str,
        result: ValidationResult,
    ) -> dict | None:
        error_code = None if result.code in {"ok", "offline"} else result.code
        async with self._transaction(owner_id) as conn:
            row = await (
                await conn.execute(
                    """
                    UPDATE public.provider_model_settings
                    SET validation_status=%s,validation_error_code=%s,validated_at=now(),updated_at=now()
                    WHERE owner_id=%s AND id=%s AND updated_at=%s::timestamptz
                    RETURNING id,provider,capability,credential_id,model,base_url,enabled,
                              validation_status,validation_error_code,validated_at,created_at,updated_at
                    """,
                    (
                        result.status,
                        error_code,
                        owner_id.value,
                        setting_id,
                        expected_updated_at,
                    ),
                )
            ).fetchone()
            if row is not None and row["capability"] == "extraction":
                if result.status == "ready" and row["credential_id"] is not None:
                    await conn.execute(
                        """
                        UPDATE public.jobs
                        SET state='pending',credential_id=%s,run_after=now(),updated_at=now()
                        WHERE owner_id=%s AND state='waiting_configuration'
                          AND cancellation_requested_at IS NULL
                        """,
                        (row["credential_id"], owner_id.value),
                    )
            await record_event_async(
                conn,
                owner_id=owner_id.value,
                actor_kind="owner",
                action="provider_setting.validate",
                target_kind="provider_setting",
                target_id=setting_id,
                result=(
                    "denied" if row is None else "succeeded"
                    if result.status in {"ready", "offline"}
                    else "failed"
                ),
                reason_code=(
                    "stale_setting" if row is None else None
                    if result.code in {"ok", "offline"}
                    else result.code
                ),
            )
        return None if row is None else self._json_row(row)

    async def resolve_credential(
        self,
        owner_id: OwnerId,
        credential_id: uuid.UUID,
        cipher: CredentialCipher,
    ) -> ResolvedProviderCredential:
        async with self._transaction(owner_id) as conn:
            row = await (
                await conn.execute(
                    """
                    SELECT owner_id,id,provider,masked_label,ciphertext,encrypted_data_key,
                           encryption_algorithm,key_version,nonce
                    FROM public.provider_credentials
                    WHERE owner_id=%s AND id=%s AND disabled_at IS NULL AND deleted_at IS NULL
                    """,
                    (owner_id.value, credential_id),
                )
            ).fetchone()
        if row is None:
            raise CredentialUnavailableError("credential is unavailable")
        envelope = EncryptedCredential(
            owner_id=row["owner_id"],
            credential_id=row["id"],
            provider=row["provider"],
            masked_label=row["masked_label"],
            ciphertext=bytes(row["ciphertext"]),
            encrypted_data_key=bytes(row["encrypted_data_key"]),
            encryption_algorithm=row["encryption_algorithm"],
            key_version=row["key_version"],
            nonce=bytes(row["nonce"]),
        )
        return ResolvedProviderCredential(envelope.provider, cipher.decrypt(envelope))

    async def source_object(
        self, owner_id: OwnerId, book_id: uuid.UUID
    ) -> SourceObjectRecord | None:
        async with self._transaction(owner_id) as conn:
            row = await (
                await conn.execute(
                    """
                    SELECT source.book_id,source.book_incarnation,source.storage_key,
                           source.storage_provider,source.media_type,source.byte_size,source.sha256,
                           source.encryption_key_id
                    FROM public.source_objects AS source
                    JOIN public.books AS book
                      ON (book.owner_id,book.id,book.incarnation)
                       =(source.owner_id,source.book_id,source.book_incarnation)
                    WHERE source.owner_id=%s AND source.book_id=%s
                      AND source.deleted_at IS NULL AND book.deleted_at IS NULL
                    """,
                    (owner_id.value, book_id),
                )
            ).fetchone()
        if row is None:
            return None
        try:
            object_id = uuid.UUID(hex=row["storage_key"])
        except (ValueError, TypeError) as exc:
            raise RuntimeError("stored source object identity is invalid") from exc
        return SourceObjectRecord(
            book_id=row["book_id"],
            book_incarnation=row["book_incarnation"],
            object_id=object_id,
            provider=row["storage_provider"],
            media_type=row["media_type"],
            byte_size=row["byte_size"],
            sha256=row["sha256"],
            encryption=row["encryption_key_id"],
        )

    async def soft_delete_book(self, owner_id: OwnerId, book_id: uuid.UUID) -> bool:
        async with self._transaction(owner_id) as conn:
            row = await (
                await conn.execute(
                    """
                    SELECT incarnation FROM public.books
                    WHERE owner_id=%s AND id=%s AND deleted_at IS NULL
                    FOR UPDATE
                    """,
                    (owner_id.value, book_id),
                )
            ).fetchone()
            if row is None:
                await record_event_async(
                    conn,
                    owner_id=owner_id.value,
                    actor_kind="owner",
                    action="book.delete",
                    target_kind="book",
                    target_id=book_id,
                    result="denied",
                    reason_code="not_found",
                )
                return False
            await conn.execute(
                """
                UPDATE public.jobs
                SET cancellation_requested_at=COALESCE(cancellation_requested_at,now()),
                    state=CASE WHEN state IN ('waiting_configuration','pending')
                               THEN 'cancelled' ELSE state END,
                    completed_at=CASE WHEN state IN ('waiting_configuration','pending')
                                      THEN now() ELSE completed_at END,
                    updated_at=now()
                WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                  AND state IN ('waiting_configuration','pending','leased','running')
                """,
                (owner_id.value, book_id, row["incarnation"]),
            )
            await record_event_async(
                conn,
                owner_id=owner_id.value,
                actor_kind="owner",
                action="book.delete",
                target_kind="book",
                target_id=book_id,
                result="succeeded",
            )
            await conn.execute(
                """
                DELETE FROM public.book_search_documents
                WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                """,
                (owner_id.value, book_id, row["incarnation"]),
            )
            for table in ("highlights", "annotations", "bookmarks"):
                await conn.execute(
                    f"""UPDATE public.{table} SET deleted_at=now(),updated_at=now(),
                               version=version+1
                        WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                          AND deleted_at IS NULL""",
                    (owner_id.value, book_id, row["incarnation"]),
                )
            await conn.execute(
                """
                UPDATE public.source_objects
                SET deleted_at=now()
                WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s AND deleted_at IS NULL
                """,
                (owner_id.value, book_id, row["incarnation"]),
            )
            await conn.execute(
                """
                UPDATE public.books SET deleted_at=now(),updated_at=now()
                WHERE owner_id=%s AND id=%s AND incarnation=%s AND deleted_at IS NULL
                """,
                (owner_id.value, book_id, row["incarnation"]),
            )
        return True

    @staticmethod
    def _book_payload(row: dict) -> dict:
        payload = dict(row)
        payload["id"] = str(payload["id"])
        payload["created_at"] = payload["created_at"].isoformat()
        if payload.get("last_opened_at") is not None:
            payload["last_opened_at"] = payload["last_opened_at"].isoformat()
        return payload

    @classmethod
    def _position_payload(
        cls, row: dict, *, applied: bool | None = None, conflict: str | None = None
    ) -> dict:
        payload = cls._json_row(row)
        payload["completed_chapter"] = payload["bookmark"]
        if applied is not None:
            payload["applied"] = applied
            payload["conflict"] = conflict
        return payload

    @staticmethod
    def _json_row(row: dict) -> dict:
        payload = dict(row)
        for key, value in tuple(payload.items()):
            if isinstance(value, uuid.UUID):
                payload[key] = str(value)
            elif hasattr(value, "isoformat"):
                payload[key] = value.isoformat()
            elif (key == "usd" or key.endswith("_usd")) and value is not None:
                payload[key] = str(value)
        return payload

    @classmethod
    def _job_payload(cls, row: dict) -> dict:
        return cls._json_row(row)
