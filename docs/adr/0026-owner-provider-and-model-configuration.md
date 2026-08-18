# ADR 0026 - Owner-scoped provider and model configuration

**Status:** Accepted (2026-07-19; LIT-46)

## Context

The hosted credential vault stores secrets safely, but a credential alone does not say which provider
or model should perform extraction, synthesis, embedding, or spoiler review. Applying service defaults
implicitly would make costs surprising and could silently replace an owner's choice. Uploading before a
usable extraction route exists must also avoid creating a claimable job that can only fail.

## Decision

### One explicit selection exists per owner and capability

`provider_model_settings` now has one owner-scoped row for each of `extraction`, `synthesis`,
`embedding`, and `judge`. A row selects a closed provider, bounded model identifier, owner-composite
credential reference, and approved HTTPS base URL. Anthropic embeddings and malformed offline rows are
database-rejected. The general `settings` JSON remains empty so secrets or unreviewed provider options
cannot become an escape hatch.

Recommended values are returned as public capability metadata only. They are never inserted and an
existing row is changed only by its owner's CSRF-bound `PUT`. The browser exposes every selection,
credential metadata only, validation state, offline behavior, and the fact that the credential owner's
provider account bears usage charges.

### Validation is zero-token, classified, and fail-closed

Validation uses the provider's model-list/read endpoint and performs no completion or embedding. The
base URL must have an HTTPS origin on the deployment allow-list; redirects and environment proxies are
disabled. Responses collapse into fixed outcomes: `invalid_credentials`, `unavailable_model`,
`network_error`, or `service_error`. Provider response bodies, exception text, and submitted secrets are
not persisted or returned.

Changing a setting makes it `unchecked`. Replacing a selected credential does the same; destructive
credential deletion disables linked settings. A successful check marks the row `ready`; choosing the
explicit offline marker records `offline` without a network request.

### Ingestion waits for a validated extraction selection

Hosted upload creates its durable job as `waiting_configuration` unless the owner already has a ready
extraction setting. Saving or invalidating extraction configuration moves pending work back to that
state and removes its credential binding. Successful extraction validation atomically attaches the
selected credential to waiting jobs and makes them claimable. Cancellation and source deletion cover
both waiting and pending work.

The worker resolves a credential only when the currently claim-fenced job still matches an enabled,
ready extraction setting. Provider/model execution remains behind the injected durable-job handler;
the job record contains only the selected credential UUID, never a secret or arbitrary settings blob.

## Verification

Pure policy tests cover explicit non-persisted recommendations, provider/capability rules, HTTPS origin
pinning, offline no-network behavior, and all fixed validation classes. Browser tests cover saving and
validating every capability, credential write-only behavior, deletion, explanatory copy, CSRF, and an
axe accessibility scan. Real PostgreSQL/pgvector tests cover the migration, strict constraints, exact
runtime grants, owner/missing equivalence, ready-job activation, invalid credential/model/network
classification, and claim-bound worker resolution.

The completed checkout collects 713 backend tests: 705 pass and eight environment-specific tests skip
with the real PostgreSQL 16 + pgvector gate enabled. The frontend has 164 passing tests and a clean
TypeScript production build; Ruff and diff checks are clean.

## Consequences and boundaries

New hosted books can be stored and remain readable while their AI work waits for deliberate owner
configuration. No real provider or credential was used during implementation. Deployments that permit
another OpenAI-compatible endpoint must explicitly add its HTTPS origin with
`HOSTED_PROVIDER_ALLOWED_ORIGINS`; allowing an origin delegates provider trust to that deployment.

This increment does not make the hosted service public-ready. LIT-47 quotas/concurrency/rate limits,
the adversarial endpoint matrix, audit/leak coverage, and migration work remain launch blockers. Local
SQLite/community behavior and the accepted permanent library are unchanged.
