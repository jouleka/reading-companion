# ADR 0031 — Cross-device reading state uses a versioned monotonic merge

**Status:** Accepted (2026-07-19)
**Ticket:** LIT-53 / SYNC-1

## Context

A reader can have the same hosted book open on several devices, including devices that reconnect
after being offline. Last-write-wins timestamps can silently replace newer progress with an older
page, while rejecting every stale update makes normal reconnects brittle. Resetting a book also has
to invalidate delayed writes from the previous reading pass.

## Decision

Every owner/book reading-state row carries a server `position_version`, a reset `position_epoch`, the
current and high-water offsets, completed chapter, last writer UUID/sequence, and `last_opened_at`.
Clients submit their observed version and epoch with a stable browser UUID and increasing sequence.
The repository locks the row and applies one deterministic policy:

- an update based on the current version may move the current page in either direction while the
  high-water offset and completed chapter remain monotonic;
- a stale update is accepted only when it advances the completed chapter or high-water offset;
- an equal-frontier stale tie is resolved by the `(client UUID, client sequence)` tuple;
- a stale update behind the frontier is acknowledged with the canonical state and `applied=false`;
- a future version or old reset epoch is rejected with `409` so the client reloads canonical state.

An explicit “start over” increments both epoch and version, clears the position/frontier, and clears
the writer clock. This is the only cross-session rewind. Soft-deleted books are hidden before merge,
and every read/write remains owner-predicated, RLS-enforced, CSRF-protected where mutating, and
`private, no-store`.

The browser uses a single serialized debounce/outbox. It retains transient failures, retries with
bounded exponential backoff and on `online`, coalesces to the newest relocation, flushes via a
keepalive request on page teardown, and exposes saved, queued, conflict, and error states in the
reader UI.

## Consequences

- Concurrent and reconnecting devices converge on a deterministic non-regressing frontier.
- Readers may intentionally revisit an earlier page without losing completed-chapter/high-water
  progress.
- An explicit reset invalidates every delayed write from the prior pass.
- The protocol is additive for the local API because local Pydantic inputs ignore hosted clock fields;
  hosted responses add optional fields consumed by the shared frontend.
- Correctness is covered with real-PostgreSQL two-device advance, stale, simultaneous, reset, and
  deletion cases plus browser debounce, failure, retry, serialization, and conflict tests.
