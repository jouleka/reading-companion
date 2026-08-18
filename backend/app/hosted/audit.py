"""Content-free hosted security audit events and operator retention tooling."""

from __future__ import annotations

import argparse
import os
import re
import uuid
from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row

_ACTOR_KINDS = frozenset({"owner", "worker", "system"})
_RESULTS = frozenset({"succeeded", "denied", "failed"})
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,31}$")
_TARGET_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Any new application log site is a security-policy change and must update the static gate. Runtime
# events use this fixed vocabulary; request bodies, headers, exception strings, filenames, prompts,
# model payloads, and source text are never logging arguments.
APPROVED_SECURITY_LOG_MESSAGES = frozenset(
    {
        "hosted runtime shutdown timed out with %d active operations",
        "source object compensation failed after metadata transaction error",
    }
)


def _event_values(
    *,
    owner_id: uuid.UUID,
    actor_kind: str,
    action: str,
    target_kind: str,
    target_id: uuid.UUID | None,
    result: str,
    reason_code: str | None = None,
    request_id: uuid.UUID | None = None,
) -> tuple:
    if not isinstance(owner_id, uuid.UUID):
        raise TypeError("audit owner must be a UUID")
    if actor_kind not in _ACTOR_KINDS:
        raise ValueError("audit actor kind is not reviewed")
    if _ACTION_RE.fullmatch(action) is None:
        raise ValueError("audit action must use the reviewed content-free format")
    if _TARGET_RE.fullmatch(target_kind) is None:
        raise ValueError("audit target kind must use the reviewed content-free format")
    if target_id is not None and not isinstance(target_id, uuid.UUID):
        raise TypeError("audit target id must be a UUID")
    if result not in _RESULTS:
        raise ValueError("audit result is not reviewed")
    if reason_code is not None and _REASON_RE.fullmatch(reason_code) is None:
        raise ValueError("audit reason code is not reviewed")
    if request_id is not None and not isinstance(request_id, uuid.UUID):
        raise TypeError("audit request id must be a UUID")
    metadata = {} if reason_code is None else {"reason_code": reason_code}
    return (
        owner_id,
        uuid.uuid4(),
        actor_kind,
        action,
        target_kind,
        target_id,
        result,
        request_id,
        psycopg.types.json.Jsonb(metadata),
    )


async def record_event_async(conn, **values) -> None:
    await conn.execute(
        """
        INSERT INTO public.audit_events
          (owner_id,id,actor_kind,action,target_kind,target_id,result,request_id,metadata)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        _event_values(**values),
    )


def record_event(conn, **values) -> None:
    conn.execute(
        """
        INSERT INTO public.audit_events
          (owner_id,id,actor_kind,action,target_kind,target_id,result,request_id,metadata)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        _event_values(**values),
    )


def inspect_events(
    dsn: str,
    *,
    owner_id: uuid.UUID | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return opaque identifiers and closed audit vocabulary; never content or credentials."""
    if limit < 1 or limit > 1_000:
        raise ValueError("audit inspection limit must be between 1 and 1000")
    clauses: list[str] = []
    parameters: list[object] = []
    if owner_id is not None:
        clauses.append("owner_id=%s")
        parameters.append(owner_id)
    if since is not None:
        if since.tzinfo is None:
            raise ValueError("audit inspection timestamp must be timezone-aware")
        clauses.append("occurred_at>=%s")
        parameters.append(since)
    predicate = " WHERE " + " AND ".join(clauses) if clauses else ""
    parameters.append(limit)
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT owner_id,id,actor_kind,action,target_kind,target_id,result,
                   metadata->>'reason_code' AS reason_code,occurred_at
            FROM public.audit_events
            """
            + predicate
            + " ORDER BY occurred_at DESC,owner_id,id LIMIT %s",
            parameters,
        ).fetchall()
    return [
        {
            key: value.isoformat() if hasattr(value, "isoformat") else str(value)
            if isinstance(value, uuid.UUID)
            else value
            for key, value in row.items()
        }
        for row in rows
    ]


def purge_events(dsn: str, *, before: datetime) -> int:
    if before.tzinfo is None:
        raise ValueError("audit retention timestamp must be timezone-aware")
    with psycopg.connect(dsn) as conn:
        cursor = conn.execute("DELETE FROM public.audit_events WHERE occurred_at < %s", (before,))
        return cursor.rowcount


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or expire content-free security audit events")
    parser.add_argument("--dsn-env", default="HOSTED_AUDIT_DSN")
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show")
    show.add_argument("--owner", type=uuid.UUID)
    show.add_argument("--since", type=_timestamp)
    show.add_argument("--limit", type=int, default=100)
    purge = commands.add_parser("purge")
    purge.add_argument("--before", type=_timestamp, required=True)
    args = parser.parse_args(argv)
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        parser.error(f"environment variable {args.dsn_env!r} is not set")
    if args.command == "show":
        import json

        print(json.dumps(inspect_events(dsn, owner_id=args.owner, since=args.since, limit=args.limit)))
    else:
        print(purge_events(dsn, before=args.before))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
