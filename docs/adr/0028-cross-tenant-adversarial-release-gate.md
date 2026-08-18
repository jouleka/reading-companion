# ADR 0028: Cross-tenant isolation is an executable release gate

- Status: Accepted
- Date: 2026-07-19
- Ticket: LIT-48 / SEC-1

## Context

Hosted isolation already existed at several layers: session-derived `OwnerId`, explicit owner SQL
predicates, PostgreSQL RLS, owner-derived object paths, tenant-keyed process state, and claim-fenced
worker mutations. Its evidence, however, was spread across the tickets that introduced each layer.
An HTTP-only endpoint list could not prove that background work and object storage stayed covered, and
future unavailable product surfaces such as search could silently fall outside the adversarial contract.

## Decision

`backend/app/hosted/tenant/endpoints.json` is the checked-in isolation inventory. It exactly matches
every enabled `hosted-library` FastAPI operation and records all required product surface classes:
list, get, mutate, delete, import, export, search, memory, cost, job, and credential. Each enabled or
intentionally unavailable route names a real adversarial test. Search and export remain explicit
non-disclosing `404` surfaces until LIT-56 and LIT-50 implement their owned contracts.

The same inventory now includes background resources. It names claim-fenced worker mutations,
just-in-time credential resolution, filesystem and S3 object lifecycles, and tenant-keyed runtime
caches/locks, with executable evidence for each. The static gate parses the hosted test suite and
fails if named evidence disappears or a required surface/resource class is omitted. The OpenAPI
comparison independently fails when a new tenant route is added without an inventory entry.

Real PostgreSQL fixtures create owners A and B. They exercise B with A's actual book, job, credential,
and source identifiers and compare foreign-resource results with random missing-resource results.
Assertions cover response status/body, owner-only lists, mutation side effects, source bytes, job
state, memory, costs, credentials, provider settings, and limits. The repository suite repeats the
boundary through a migration connection that bypasses RLS, proving explicit query predicates; the
normal HTTP suite uses the exact-grant non-BYPASSRLS role and therefore proves RLS as a second barrier.

The privileged worker is separately attacked by changing a valid A claim to B's real owner and by
attempting to commit to B's real book/incarnation. All lease mutations fail closed and no B receipt is
written. Both object adapters resolve A's real opaque object UUID under B, prove it missing, and prove
that a foreign delete cannot remove A's object.

## Consequences

- Tenant isolation is release-blocking across HTTP, repository, worker, storage, cache, and lock paths.
- Foreign identifiers retain the same public shape as missing identifiers and mutations have no
  cross-owner effect.
- Adding a hosted route without inventory/evidence fails CI; deleting or renaming background evidence
  also fails CI.
- The inventory describes only shipped or deliberately unavailable operations. It does not claim that
  future search/export behavior is implemented, and their tickets must replace the unavailable entries
  with enabled, adversarially tested routes.
- Secret-leak/log redaction evidence remains the separate LIT-49 gate; database backup and migration
  drills remain LIT-50 and related operations work.
