# ADR 0036: Offline reading is explicit, tenant-scoped, and purgeable

## Status

Accepted — 2026-07-21

## Context

An installable reader must keep an explicitly saved EPUB usable through a network outage, preserve
position and annotation work until connectivity returns, and never leave one hosted tenant's library
behind after sign-out or account switching. A service worker is a second persistence surface, so an
ordinary shared cache would violate the owner boundary even if the API remains correctly scoped.

## Decision

The browser registers one root service worker and keeps only the application shell in a shared,
code-only cache. Book manifests, EPUB responses, positions, marks, and preferences are stored in a
separate cache named for the authenticated owner. Books enter that cache only through the explicit
“Save for offline reading” action; the action succeeds only after both the manifest and EPUB are
confirmed present. Authentication endpoints and unsafe API responses are never cached.

The client persists a minimal, expiring session envelope containing only owner identity and display
name. It may restore that envelope only when session verification is unreachable, never after an
authoritative signed-out response or expiry. Invalid owner identifiers fail closed. Switching owners
or signing out deletes the prior owner's response cache, mutation outbox, restored session, and all
owner-scoped reader-local state. The versioned application shell remains because it contains no
tenant data.

Transient position and mark failures enter a bounded owner-scoped outbox. Position writes coalesce by
book; mark updates fold into an unsynchronized create, and deleting such a create cancels it. The
reader overlays queued state without lowering the canonical spoiler frontier. Replay uses the current
session and CSRF token, sends positions before marks, removes successful and terminal mutations, and
retains only retryable failures. Synchronization runs on application startup and browser `online`
events; no unsupported promise of background execution is made.

## Consequences

- Offline access is deliberate and storage/quota failure is reported without breaking online reading.
- Queued work remains visible and reconciles with authoritative position, marks, and manifest state.
- Tenant data is segregated across Cache Storage, local storage, and the mutation queue, then erased
  together on sign-out or owner change.
- A closed browser cannot guarantee immediate synchronization; the queue resumes on the next open or
  reconnect while the application is running.
- Local/community mode uses the same offline machinery under its explicit local owner namespace and
  does not require a hosted CSRF token.
