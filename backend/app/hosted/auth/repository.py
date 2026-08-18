"""Narrow PostgreSQL repository for pre-owner authentication bootstrap.

The configured login role must be non-superuser ``BYPASSRLS`` and have DML privileges on only the
four authentication tables plus INSERT-only access to the content-free audit sink. Ordinary hosted
repositories use the transaction-local owner policy; this deliberately separate role exists because
session/identity lookup happens before an owner is known.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

import psycopg

from app.hosted.audit import record_event_async
from app.hosted.auth.models import IdentityClaims, LoginAttempt, Principal


class AuthConfigurationError(RuntimeError):
    pass


class InactiveUserError(RuntimeError):
    pass


class AuthRepository(Protocol):
    async def check_runtime_role(self) -> None: ...

    async def create_login_attempt(
        self,
        *,
        state_digest: bytes,
        browser_digest: bytes,
        issuer: str,
        code_verifier: str,
        nonce: str,
        return_to: str,
        now: datetime,
        expires_at: datetime,
    ) -> None: ...

    async def consume_login_attempt(
        self, *, state_digest: bytes, browser_digest: bytes, issuer: str, now: datetime
    ) -> LoginAttempt | None: ...

    async def resolve_identity(self, claims: IdentityClaims, *, now: datetime) -> uuid.UUID: ...

    async def create_session(
        self,
        *,
        owner_id: uuid.UUID,
        session_digest: bytes,
        csrf_digest: bytes,
        issuer: str,
        old_session_digest: bytes | None,
        now: datetime,
        expires_at: datetime,
    ) -> uuid.UUID: ...

    async def authenticate_session(
        self, *, session_digest: bytes, now: datetime, idle_ttl: timedelta
    ) -> Principal | None: ...

    async def revoke_session(self, *, session_digest: bytes, now: datetime) -> bool: ...


@dataclass(slots=True)
class PostgresAuthRepository:
    _dsn: str = field(repr=False)

    async def _connect(self) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(self._dsn)

    async def check_runtime_role(self) -> None:
        required = {
            "public.users": "SELECT,INSERT,UPDATE,DELETE",
            "public.external_identities": "SELECT,INSERT,UPDATE",
            "public.sessions": "SELECT,INSERT,UPDATE",
            "public.oidc_login_attempts": "SELECT,INSERT,DELETE",
            "public.audit_events": "INSERT",
        }
        async with await self._connect() as conn:
            role = await (
                await conn.execute(
                    """
                    SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole,
                           rolreplication, rolinherit,
                           EXISTS (SELECT 1 FROM pg_auth_members WHERE member = role_row.oid)
                    FROM pg_roles AS role_row WHERE rolname = current_user
                    """
                )
            ).fetchone()
            if role is None or role[0] or not role[1] or any(role[2:]):
                raise AuthConfigurationError(
                    "HOSTED_AUTH_DSN must use an isolated non-superuser BYPASSRLS authentication role"
                )
            missing = []
            for table, privileges in sorted(required.items()):
                for privilege in privileges.split(","):
                    row = await (
                        await conn.execute(
                            "SELECT has_table_privilege(current_user, %s, %s)",
                            (table, privilege),
                        )
                    ).fetchone()
                    if row is None or not row[0]:
                        missing.append(f"{table}:{privilege}")
            unexpected = await (
                await conn.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_type = 'BASE TABLE'
                      AND table_name <> ALL(%s)
                      AND has_table_privilege(
                            current_user, quote_ident(table_schema) || '.' || quote_ident(table_name),
                            'SELECT,INSERT,UPDATE,DELETE'
                          )
                    ORDER BY table_name
                    """,
                    ([name.removeprefix("public.") for name in required],),
                )
            ).fetchall()
        if missing or unexpected:
            raise AuthConfigurationError(
                "authentication role privileges are not restricted to the authentication tables"
            )

    async def create_login_attempt(
        self,
        *,
        state_digest: bytes,
        browser_digest: bytes,
        issuer: str,
        code_verifier: str,
        nonce: str,
        return_to: str,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        async with await self._connect() as conn:
            await conn.execute(
                "DELETE FROM public.oidc_login_attempts WHERE expires_at <= %s", (now,)
            )
            await conn.execute(
                """
                INSERT INTO public.oidc_login_attempts
                  (state_digest, browser_digest, issuer, code_verifier, nonce, return_to,
                   created_at, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    state_digest,
                    browser_digest,
                    issuer,
                    code_verifier,
                    nonce,
                    return_to,
                    now,
                    expires_at,
                ),
            )

    async def consume_login_attempt(
        self, *, state_digest: bytes, browser_digest: bytes, issuer: str, now: datetime
    ) -> LoginAttempt | None:
        async with await self._connect() as conn:
            row = await (
                await conn.execute(
                    """
                    DELETE FROM public.oidc_login_attempts
                    WHERE state_digest = %s AND browser_digest = %s
                      AND issuer = %s AND expires_at > %s
                    RETURNING issuer, code_verifier, nonce, return_to
                    """,
                    (state_digest, browser_digest, issuer, now),
                )
            ).fetchone()
        return LoginAttempt(*row) if row else None

    async def resolve_identity(self, claims: IdentityClaims, *, now: datetime) -> uuid.UUID:
        candidate = uuid.uuid4()
        async with await self._connect() as conn:
            row = await (
                await conn.execute(
                    "SELECT owner_id FROM public.external_identities "
                    "WHERE issuer = %s AND subject = %s",
                    (claims.issuer, claims.subject),
                )
            ).fetchone()
            if row:
                owner_id = row[0]
            else:
                await conn.execute(
                    "INSERT INTO public.users (id, display_name, email, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (candidate, claims.display_name, claims.email, now, now),
                )
                row = await (
                    await conn.execute(
                        """
                        INSERT INTO public.external_identities
                          (owner_id, issuer, subject, email, email_verified, linked_at, last_login_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (issuer, subject) DO UPDATE SET
                          email = EXCLUDED.email,
                          email_verified = EXCLUDED.email_verified,
                          last_login_at = EXCLUDED.last_login_at
                        RETURNING owner_id
                        """,
                        (
                            candidate,
                            claims.issuer,
                            claims.subject,
                            claims.email,
                            claims.email_verified,
                            now,
                            now,
                        ),
                    )
                ).fetchone()
                assert row is not None
                owner_id = row[0]
                if owner_id != candidate:
                    await conn.execute("DELETE FROM public.users WHERE id = %s", (candidate,))

            await conn.execute(
                """
                UPDATE public.external_identities
                SET email = %s, email_verified = %s, last_login_at = %s
                WHERE owner_id = %s AND issuer = %s AND subject = %s
                """,
                (
                    claims.email,
                    claims.email_verified,
                    now,
                    owner_id,
                    claims.issuer,
                    claims.subject,
                ),
            )
            await conn.execute(
                "UPDATE public.users SET display_name = %s, email = %s, updated_at = %s "
                "WHERE id = %s",
                (claims.display_name, claims.email, now, owner_id),
            )
        return owner_id

    async def create_session(
        self,
        *,
        owner_id: uuid.UUID,
        session_digest: bytes,
        csrf_digest: bytes,
        issuer: str,
        old_session_digest: bytes | None,
        now: datetime,
        expires_at: datetime,
    ) -> uuid.UUID:
        session_id = uuid.uuid4()
        async with await self._connect() as conn:
            if old_session_digest is not None:
                await conn.execute(
                    "UPDATE public.sessions SET revoked_at = %s WHERE session_digest = %s "
                    "AND revoked_at IS NULL",
                    (now, old_session_digest),
                )
            row = await (
                await conn.execute(
                    """
                    INSERT INTO public.sessions
                      (owner_id, id, session_digest, csrf_digest, oidc_issuer,
                       created_at, last_seen_at, expires_at)
                    SELECT id, %s, %s, %s, %s, %s, %s, %s
                    FROM public.users WHERE id = %s AND status = 'active' AND deleted_at IS NULL
                    RETURNING id
                    """,
                    (
                        session_id,
                        session_digest,
                        csrf_digest,
                        issuer,
                        now,
                        now,
                        expires_at,
                        owner_id,
                    ),
                )
            ).fetchone()
            if row is None:
                raise InactiveUserError("account is not active")
            await record_event_async(
                conn,
                owner_id=owner_id,
                actor_kind="owner",
                action="session.login",
                target_kind="session",
                target_id=session_id,
                result="succeeded",
            )
        return session_id

    async def authenticate_session(
        self, *, session_digest: bytes, now: datetime, idle_ttl: timedelta
    ) -> Principal | None:
        async with await self._connect() as conn:
            row = await (
                await conn.execute(
                    """
                    SELECT s.owner_id, s.id, u.display_name, u.email, s.csrf_digest,
                           s.expires_at, s.last_seen_at, s.revoked_at, u.status, u.deleted_at
                    FROM public.sessions AS s
                    JOIN public.users AS u ON u.id = s.owner_id
                    WHERE s.session_digest = %s
                    """,
                    (session_digest,),
                )
            ).fetchone()
            if row is None:
                return None
            invalid = (
                row[7] is not None
                or row[5] <= now
                or row[6] <= now - idle_ttl
                or row[8] != "active"
                or row[9] is not None
            )
            if invalid:
                if row[7] is None:
                    await conn.execute(
                        "UPDATE public.sessions SET revoked_at = %s "
                        "WHERE owner_id = %s AND id = %s",
                        (now, row[0], row[1]),
                    )
                return None
            await conn.execute(
                "UPDATE public.sessions SET last_seen_at = %s WHERE owner_id = %s AND id = %s",
                (now, row[0], row[1]),
            )
        return Principal(
            owner_id=row[0],
            session_id=row[1],
            display_name=row[2],
            email=row[3],
            csrf_digest=bytes(row[4]),
            expires_at=row[5],
        )

    async def revoke_session(self, *, session_digest: bytes, now: datetime) -> bool:
        async with await self._connect() as conn:
            row = await (
                await conn.execute(
                "UPDATE public.sessions SET revoked_at = %s WHERE session_digest = %s "
                "AND revoked_at IS NULL RETURNING owner_id,id",
                (now, session_digest),
                )
            ).fetchone()
            if row is not None:
                await record_event_async(
                    conn,
                    owner_id=row[0],
                    actor_kind="owner",
                    action="session.logout",
                    target_kind="session",
                    target_id=row[1],
                    result="succeeded",
                )
            return row is not None
