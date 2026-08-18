# ADR 0022 — Tenant-scoped hosted runtime lifecycle

**Status:** Accepted (2026-07-17; LIT-42)

## Context

LIT-41 makes database access owner-explicit and RLS-backed, but process-local state can bypass that
boundary if a cache or lock is keyed by a bare book UUID. A valid UUID may occur in requests from
different users, cached values can outlive a request, and an unbounded lock map can retain tenant
identifiers indefinitely. Hosted shutdown also needs a finite drain policy that does not leak those
identifiers or cached content through diagnostics.

The local SQLite runtime already has separate book-handle, segmentation, and recap lifecycle
contracts. This decision adds a hosted-only boundary; it does not merge the two compositions or
change the permanent local library.

## Decision

### Runtime keys are closed, typed, and owner-complete

Every hosted process cache and concurrency key is constructed from `TenantResourceKey`:

- a typed `OwnerId` derived from the verified session;
- a closed `ResourceKind`; and
- a UUID resource identity.

Book operations use the book UUID. Owner-wide library and cost operations use the owner UUID as the
resource identity while retaining a distinct resource kind. Cache keys additionally require a closed
`CacheNamespace` and an explicit generation. Raw tuples, strings, and UUID-only keys are rejected at
the runtime boundary.

The schema-migration advisory lock and the OIDC client's two single-instance discovery/JWKS refresh
locks are infrastructure-scoped exceptions: they coordinate one global schema or one configured
issuer's public key material, contain no tenant content, and are not resource-keyed tenant state.

The currently enabled metadata cache stores successful owned-book results only. It does not cache a
missing/foreign result, mutable reading state, memory snapshots, or costs. Values are copied on cache
entry and exit so a response caller cannot mutate the retained object.

### One lifespan owns a bounded cache and lock registry

Hosted startup creates one `HostedRuntimeRegistry` and places it in application state. The registry
uses access-order eviction with configurable positive bounds. Cache insertion immediately converges
to its bound.

Lock entries count both holders and waiters. The same owner/resource key serializes; the same resource
UUID under another owner has a different lock and can proceed independently. An active lock entry is
never evicted. If all candidates are active, temporary overflow is allowed and is trimmed as leases
release. This avoids replacing a live lock and accidentally allowing two same-resource operations to
run concurrently.

The enabled hosted routes use these scopes:

| Surface | Runtime resource |
| --- | --- |
| book metadata, position, reset, and memory | owner + book + book UUID |
| book list | owner + library + owner UUID |
| costs for one book | owner + book + book UUID |
| aggregate costs | owner + costs + owner UUID |

This is process-local coordination, not distributed locking. Later durable workers and cross-process
mutations must use PostgreSQL/object-store concurrency controls in addition to this request boundary.

### Shutdown and telemetry are bounded and content-free

Shutdown atomically enters `closing`, clears cached content, and rejects new cache or lock work. It
waits only for the configured timeout for existing lock leases. A timed-out app shutdown emits one
aggregate warning containing the active-operation count; the final releasing lease transitions the
registry to `closed` and drops retained lock keys.

`stats()` exposes only state, entry/lease counts, cache hit/miss/eviction counters, and lock eviction
count. Owner IDs, resource IDs, cache generations, cached values, session material, and provider
secrets are never metric labels or log values.

The operator bounds are:

| Setting | Default |
| --- | ---: |
| `HOSTED_RUNTIME_CACHE_MAX_ENTRIES` | 256 |
| `HOSTED_RUNTIME_LOCK_MAX_ENTRIES` | 256 |
| `HOSTED_RUNTIME_SHUTDOWN_TIMEOUT_SECONDS` | 5.0 |

## Verification

The focused runtime suite proves:

- identical book UUIDs under two owners cannot share cached content;
- the same owner/book serializes while another owner with the same UUID proceeds independently;
- active overflow never evicts a live lock and converges to the bound on release;
- shutdown returns within its timeout, rejects new work, drains to closed, and clears content;
- metrics contain no tested owner or resource identifier;
- malformed and untyped keys are rejected;
- the actual book route preserves owner-separated caching; and
- the FastAPI lifespan owns and closes the injected registry with the configured timeout; and
- a forced shutdown timeout log contains only its aggregate active-operation count.

The real PostgreSQL hosted/parity gate passes 65 tests. The complete backend suite passes 667 tests
with 8 expected skips against PostgreSQL 16 plus pgvector. Local SQLite remains the default.

## Consequences and boundaries

Hosted process state now has a single reviewable key/lifecycle boundary, and all currently enabled
tenant routes participate in it. The metadata cache is deliberately narrow; future cache namespaces
must be added to the closed enum, choose a generation/invalidation contract, and receive cross-owner
tests. Future routes must choose a typed runtime resource in addition to satisfying the LIT-41
endpoint inventory.

This does not provide cross-process exclusion, durable jobs, storage lifecycle, quota enforcement, or
public-hosting readiness. Those remain LIT-43 through LIT-50. LIT-43 is the next ticket and must not
start implicitly.
