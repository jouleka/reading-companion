# backend

Python 3.12+ / FastAPI service for the spoiler-safe story-memory engine. It is implemented, not a
scaffold: EPUB segmentation, atomic per-chapter extraction, per-book SQLite memory, the global
catalog, model/version pins, the runtime spoiler gate, reader-position derivation, and all companion
view routes live under `app/`.

Key boundaries:

- every fact read passes through `app/memory/dal.py` with `revealed_at <= bookmark` and referential
  closure;
- ingestion performs model and embedding work outside the per-book lock, then commits one complete
  chapter plus its LIT-7 completion receipt in a single transaction;
- view routes clamp requested bookmarks to both the reader high-water and the contiguous durable
  completion frontier;
- generated prose passes deterministic lexical + entity/event-role grounding and the LLM judge;
  structured views do not call an
  LLM.
- process-local Store, segmentation, and recap state is bounded and incarnation-aware; deletion
  closes the idle handle and invalidates cached state while preserving the on-disk book files.

Lifecycle limits are configurable through environment variables (all must be positive integers):

| Variable | Default | Meaning |
| --- | ---: | --- |
| `STORE_MAX_HANDLES` | 16 | Idle per-book SQLite handles retained by the lease-aware LRU |
| `SEGMENTATION_CACHE_MAX_ENTRIES` | 8 | Parsed EPUB results retained across ingestion runs |
| `RECAP_CACHE_MAX_ENTRIES` | 128 | Successful recap payloads retained |
| `RECAP_FAILURE_MAX_ENTRIES` | 128 | Negative recap-gate results retained for their TTL |
| `RECAP_MAX_INFLIGHT` | 8 | Distinct recap keys allowed to synthesize concurrently |
| `EPUB_MAX_UPLOAD_BYTES` | 134,217,728 | Exact EPUB file limit; multipart ingress is capped with 64 KiB overhead |
| `VECTOR_BACKEND` | `vec0` | Production sqlite-vec KNN; `bruteforce` selects the exact reference/fallback |

Runtime diagnostics are available in-process through `Store.stats()`,
`IngestWorker.segmentation_cache_size()`, and `RecapRegistry.stats()`.

Vector search uses sqlite-vec 0.1.9 metadata constraints for `book_id`, both chunk and live-chapter
reveal bounds, transaction-time retraction state, and embedding identity before KNN ranking. Canonical
JSON vectors remain in `chunks` for exact cosine parity, portable backup, and derived-index rebuilds.
A missing, partial, or mismatched configured vec0 schema fails startup rather than silently falling
back. Version 0.1.9 scans within its metadata filter; no sublinear-search claim is made. Index
verification/backfill is O(N) when a book handle opens.

## Hosted PostgreSQL foundation (LIT-38)

`app/hosted/schema/` is a separate, dormant hosted migration stream. It does not alter local
composition: FastAPI still opens the existing SQLite catalog/per-book stores, and no local library is
dual-written or migrated. The PostgreSQL schema carries non-null owner scope through composite
resource keys, bitemporal facts, deferred chapter receipts, jobs/costs, envelope-encrypted credential
fields, reader notes, and audit foundations. Full raw chapters remain local-only; hosted source bytes
belong behind the later object-storage boundary.

The pgvector baseline intentionally has no global ANN index. `search_chunks_prefiltered` materializes
owner, book/incarnation, effective bookmark, live receipt/retraction, and embedding-space eligibility
before exact distance ranking. PostgreSQL RLS reads `app.owner_id` from transaction-local context as a
second barrier; repository queries must still include owner predicates.

Run the committed real-database harness from WSL with Docker available:

```bash
backend/scripts/test_postgres.sh
```

For an operator-managed database, provide the DSN to the process through its environment or secret
manager and run `.venv/bin/python -m app.hosted.migrations`. The command reads `DATABASE_URL` by
default (or the variable named by `--dsn-env`) and never prints its value. See
[`../docs/adr/0018-postgresql-schema-and-pgvector-foundation.md`](../docs/adr/0018-postgresql-schema-and-pgvector-foundation.md).

The same real-database command also runs the LIT-39 spoiler-parity cutover gate. Its shared corpus is
loaded into production SQLite/sqlite-vec and migrated PostgreSQL/pgvector, then all structured
surfaces and retrieval are compared at every bookmark. Corrections, receipts, resets, cache
invalidation, future references, and a prefilter-before-top-k canary are included. Reviewed physical
differences are machine-checked; spoiler-semantic differences are never allowed. See
[`../docs/adr/0019-sqlite-postgresql-spoiler-parity.md`](../docs/adr/0019-sqlite-postgresql-spoiler-parity.md).

## Hosted OIDC and sessions (LIT-40)

`DEPLOYMENT_MODE=hosted` authenticates OIDC users and exposes a deliberately small owner-scoped
library/read/reset/cost surface. Upload, source streaming, deletion, export, ingestion, and arbitrary
position advance remain unavailable until their storage/lifecycle/worker tickets. Local mode and its
SQLite data path are unchanged.

Hosted startup requires these secret-manager/environment values:

| Variable | Meaning |
| --- | --- |
| `HOSTED_AUTH_DSN` | DSN for the restricted, non-superuser BYPASSRLS authentication role |
| `HOSTED_TENANT_DSN` | Separate exact-grant, non-superuser/non-BYPASSRLS repository role |
| `OIDC_ISSUER` | Exact HTTPS issuer, without a trailing slash |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | Confidential OIDC client credentials |
| `OIDC_REDIRECT_URI` | Exact registered HTTPS `/api/auth/callback` URI |

The database role is provisioned outside migrations so no password enters source control. It receives
only the per-table grants recorded in ADR 0020; startup rejects broader DML access. Authorization Code
uses S256 PKCE, one-time browser-bound state, signed ID-token validation, and `(issuer, subject)` to
internal UUID resolution. Session and CSRF cookies use the `__Host-` prefix, Secure, Path=/, and
SameSite=Lax; the session is HttpOnly and both raw tokens are stored only as digests. Run the same
real-database harness above to exercise the signed OIDC flow, rotation, expiry, CSRF, and logout.
See [`../docs/adr/0020-oidc-users-and-secure-sessions.md`](../docs/adr/0020-oidc-users-and-secure-sessions.md).

Every tenant repository method requires the session-derived `OwnerId`, every SQL predicate carries
that owner, and every transaction independently sets the RLS owner. Valid foreign identifiers return
the same `404` shape as missing identifiers; list and cost surfaces cannot contain another owner.
`app/hosted/tenant/endpoints.json` is checked against the actual FastAPI routes and names the
cross-tenant evidence for each one. It also inventories required unavailable product surfaces plus
worker, credential-resolution, filesystem/S3 storage, cache, and lock paths. The real PostgreSQL
harness uses another owner's actual identifiers and also runs the repository as a superuser to prove
explicit predicates still isolate when RLS is bypassed. See
[`../docs/adr/0021-owner-scoped-hosted-repositories.md`](../docs/adr/0021-owner-scoped-hosted-repositories.md).
The release-gate matrix is recorded in
[`../docs/adr/0028-cross-tenant-adversarial-release-gate.md`](../docs/adr/0028-cross-tenant-adversarial-release-gate.md).

Sensitive hosted transitions write content-free audit rows with a closed actor/action/target/result
vocabulary and optional bounded reason code. Runtime auth, tenant, and worker roles have INSERT only;
they cannot read or erase history. Provision a separate non-superuser BYPASSRLS audit role with
schema usage and only `SELECT, DELETE` on `audit_events`, expose its DSN as `HOSTED_AUDIT_DSN`, and
keep it out of the web/worker environments. There is no audit HTTP endpoint.

```bash
python -m app.hosted.audit show --owner 00000000-0000-0000-0000-000000000000 --limit 100
python -m app.hosted.audit purge --before 2026-04-20T00:00:00Z
```

Run the purge daily with a timezone-aware cutoff; production defaults to 90 days. Suspend it only for
a documented incident/legal hold. Output contains opaque IDs and closed codes, never content,
filenames, headers, prompts, provider bodies, or credentials. See
[`../docs/adr/0029-content-free-security-audit-and-redaction.md`](../docs/adr/0029-content-free-security-audit-and-redaction.md).

Hosted process state is owned by the FastAPI lifespan. Every cache and lock key includes the typed
session owner, a closed resource kind, and a resource UUID; book metadata cache keys also include a
closed namespace and generation. The cache and idle-lock registries are bounded LRUs. Active lock
holders/waiters are never evicted, so temporary all-active overflow converges only after release.
Shutdown clears cached content, rejects new work, and waits for a bounded drain; diagnostics contain
aggregate counts only.

| Variable | Default | Meaning |
| --- | ---: | --- |
| `HOSTED_RUNTIME_CACHE_MAX_ENTRIES` | 256 | Successful owned-book metadata entries retained |
| `HOSTED_RUNTIME_LOCK_MAX_ENTRIES` | 256 | Idle owner/resource lock entries retained |
| `HOSTED_RUNTIME_SHUTDOWN_TIMEOUT_SECONDS` | 5.0 | Maximum lifespan drain wait in seconds |

See [`../docs/adr/0022-tenant-scoped-runtime-lifecycle.md`](../docs/adr/0022-tenant-scoped-runtime-lifecycle.md).

Hosted source EPUBs use the same server-only `OwnerId` + opaque object UUID contract across encrypted
filesystem and S3-compatible adapters. Clients submit or receive only owned book UUIDs. Upload/read/
delete recheck owner-scoped PostgreSQL metadata; content type, the configured upload bound, SHA-256,
and encryption are mandatory. The filesystem envelope is AES-256-GCM. S3 requires confirmed `AES256`
or exact-key `aws:kms` server-side encryption and validates the object-store checksum on read.

Choose one hosted storage backend:

| Variable | Meaning |
| --- | --- |
| `HOSTED_STORAGE_BACKEND` | Required `filesystem` or `s3` |
| `HOSTED_STORAGE_FILESYSTEM_ROOT` | Dedicated filesystem object root |
| `HOSTED_STORAGE_FILESYSTEM_KEY` | Secret base64 encoding of exactly 32 bytes |
| `HOSTED_S3_BUCKET` / `HOSTED_S3_REGION` | S3-compatible bucket and region |
| `HOSTED_S3_ENDPOINT_URL` | Optional custom endpoint; HTTPS required by default |
| `HOSTED_S3_ALLOW_INSECURE_HTTP` | Explicit development-only HTTP endpoint opt-in |
| `HOSTED_S3_ACCESS_KEY_ID` / `HOSTED_S3_SECRET_ACCESS_KEY` | Optional paired static credentials; omit for workload roles |
| `HOSTED_S3_SSE_ALGORITHM` | `AES256` (default) or `aws:kms` |
| `HOSTED_S3_KMS_KEY_ID` | Required for `aws:kms`, forbidden for `AES256` |
| `HOSTED_S3_EXPECTED_BUCKET_OWNER` | Optional AWS account-id guard |

See [`../docs/adr/0023-filesystem-s3-source-object-storage.md`](../docs/adr/0023-filesystem-s3-source-object-storage.md).

## Hosted provider configuration (LIT-45/LIT-46)

Hosted credential APIs are write-only for secret material and return masked metadata. Each owner then
selects a provider, model, credential, and HTTPS base URL independently for extraction, synthesis,
embedding, and spoiler review. Recommendations are display-only; an upload waits in
`waiting_configuration` until that owner validates an extraction selection. Validation checks provider
model availability without generating tokens and returns only fixed, secret-free outcome codes.

| Variable | Default | Meaning |
| --- | --- | --- |
| `HOSTED_CREDENTIAL_MASTER_KEY` | required in hosted mode | Base64 AES-256-GCM key that wraps per-credential data keys |
| `HOSTED_CREDENTIAL_KEY_VERSION` | required in hosted mode | Active master-key version label |
| `HOSTED_CREDENTIAL_PREVIOUS_MASTER_KEYS` | empty | Temporary version-to-key map used during DEK rewrap |
| `HOSTED_PROVIDER_ALLOWED_ORIGINS` | OpenAI and Anthropic HTTPS origins | Comma-separated HTTPS origin allow-list for provider validation |
| `HOSTED_PROVIDER_VALIDATION_TIMEOUT_SECONDS` | `5.0` | Bounded model-availability request timeout (maximum 15 seconds) |

Keep the origin allow-list narrow: it is the SSRF boundary and the deployment's explicit trust decision
for OpenAI-compatible endpoints. Provider validation disables redirects and environment proxy use. See
[`../docs/adr/0025-encrypted-provider-credential-vault.md`](../docs/adr/0025-encrypted-provider-credential-vault.md)
and [`../docs/adr/0026-owner-provider-and-model-configuration.md`](../docs/adr/0026-owner-provider-and-model-configuration.md).

## Hosted owner limits (LIT-47)

PostgreSQL is the shared authority for per-owner upload bytes, live library bytes/books, active jobs,
fixed-window requests, worker/provider concurrency, and optional USD spend. The web and worker paths
serialize last-unit decisions on an owner-keyed PostgreSQL transaction lock, so multiple replicas
cannot each admit the same capacity. `GET /api/limits` returns numeric policy and aggregate usage only. Retryable limits return
`429` with `Retry-After`; persistent quotas return structured actions for deleting owned resources or
contacting an operator.

Inspect aggregate policy/usage or change reviewed fields with a privileged operator DSN (the value is
read from the environment and is never printed):

```bash
DATABASE_URL='postgresql://...' .venv/bin/python -m app.hosted.limits show
DATABASE_URL='postgresql://...' .venv/bin/python -m app.hosted.limits show --owner OWNER_UUID
DATABASE_URL='postgresql://...' .venv/bin/python -m app.hosted.limits set OWNER_UUID \
  --max-books 200 --max-library-bytes 10737418240 --max-provider-concurrency 3
DATABASE_URL='postgresql://...' .venv/bin/python -m app.hosted.limits set OWNER_UUID \
  --max-spend-usd 25.00
```

`--clear-spend-limit` restores the optional unlimited-spend policy. Open spend reservations remain
conservatively counted after a crash until an operator deliberately reconciles the provider outcome.
The tool selects no book content, profile fields, prompts, or credential material. See
[`../docs/adr/0027-atomic-owner-limits.md`](../docs/adr/0027-atomic-owner-limits.md).

## Entity split and re-merge corrections (LIT-10)

Identity corrections are trusted DAL operations, not reader-facing routes. Always inspect the
bookmark-bounded dependency inventory and take a backup before applying one. A split requires an
explicit decision for every active alias, relationship, and event participation; use `[]` only when
dropping that dependency is intentional.

```python
with store.book(book_id) as mem:
    inventory = mem.entity_correction_inventory([source_id], effective_at=8)
    result = mem.split_entity(
        source_id,
        effective_at=8,
        replacements=[
            {"canonical_name": "Alexander", "type": "character", "state": None},
            {"canonical_name": "Alexandra", "type": "character", "state": None},
        ],
        alias_assignments={alias_id: [0]},
        edge_assignments={edge_id: [0]},
        event_assignments={event_id: [1]},
        reason="chapter 8 reveals two distinct people",
    )
```

`merge_entities(...)` unions same-type identities into one new identity at the supplied bookmark,
deduplicating relationships and requiring `event_roles={event_id: role}` when source roles conflict.
Both operations are atomic; earlier bookmarks keep the old identity model and correction audit rows
are included in exact and portable backups. See
[`../docs/adr/0013-bookmark-effective-entity-corrections.md`](../docs/adr/0013-bookmark-effective-entity-corrections.md).

## Adversarial EPUB ingress (LIT-11)

EPUB import validates the ZIP before segmentation: an allocation-safe 8 MiB central-directory
preflight, 4,096 entries, 32 MiB per member, 512 MiB total declared expansion, and a 200:1 ratio limit
for members of at least 1 MiB; parsed metadata has a tighter 4 MiB cap. Paths must be canonical and collision-free; stored/deflate are the only
compression methods; encrypted ZIP members, split/ZIP64 containers, symlinks,
missing spine documents, ambiguous manifest ids, and truncated reads fail closed. DRM encryption
metadata is rejected with an explicit client message. Standard IDPF/Adobe font obfuscation remains
allowed when it targets existing font files. See
[`../docs/adr/0011-adversarial-epub-ingress.md`](../docs/adr/0011-adversarial-epub-ingress.md).

## Cost ceilings and huge chapters (LIT-21)

Every completion or embedding batch reserves its worst-case spend in `catalog.db` before provider I/O. Ledger usage and
outstanding reservations are checked atomically, provider output tokens are capped, and successful
usage replaces the estimate. A huge chapter is split at paragraph/newline/word boundaries into
bounded provider requests, then merged back into one chapter extraction and one LIT-7 receipt; no
chapter atom or spoiler stamp is split. Token estimates use UTF-8 bytes as a conservative,
tokenizer-independent upper bound and include structured-output schema/re-ask overhead.

| Variable | Default | Meaning |
| --- | ---: | --- |
| `COST_MAX_INPUT_TOKENS_PER_CALL` | 60,000 | Maximum estimated input for one provider request |
| `COST_MAX_OUTPUT_TOKENS_PER_CALL` | 4,096 | Provider-enforced output cap per request |
| `COST_MAX_INPUT_TOKENS_PER_BOOK` | 2,000,000 | Durable ledger + active-reservation input ceiling |
| `COST_MAX_OUTPUT_TOKENS_PER_BOOK` | 500,000 | Durable ledger + active-reservation output ceiling |
| `COST_MAX_USD_PER_BOOK` | 5.00 | Known-model USD ceiling; token ceilings still protect unknown prices |

A process death deliberately leaves an in-flight reservation in place because its billed usage is
unknown. Inspect it while the service is stopped, then explicitly convert the conservative reserved
amount into a ledger entry:

```bash
cd backend
.venv/bin/python -m app.cost status --data-dir /absolute/path/to/data --book-id bk0123456789ab
.venv/bin/python -m app.cost reconcile --data-dir /absolute/path/to/data --book-id bk0123456789ab
```

Backup refuses a book with outstanding reservations. USD pricing is advisory and currently recognized
for the configured GPT-4o family; an unknown model is still hard-bounded by input/output tokens. See
[`../docs/adr/0010-cost-ceilings-and-huge-chapters.md`](../docs/adr/0010-cost-ceilings-and-huge-chapters.md).

## Backup, verify, and restore (LIT-24)

Lifecycle commands are explicit about every path and never read or rewrite credentials:

```bash
cd backend

# Online backup: safe while the service has WAL databases open.
.venv/bin/python -m app.lifecycle backup \
  --data-dir /absolute/path/to/data \
  --book-id bk0123456789ab \
  --output /absolute/path/to/book.rcbackup

.venv/bin/python -m app.lifecycle verify /absolute/path/to/book.rcbackup

# Restore to a NEW directory; verify/smoke there before switching DATA_DIR.
.venv/bin/python -m app.lifecycle restore /absolute/path/to/book.rcbackup \
  --target /absolute/path/to/restored-data

# Exercise the portable JSON representation rather than the SQLite snapshots.
.venv/bin/python -m app.lifecycle restore /absolute/path/to/book.rcbackup \
  --target /absolute/path/to/portable-restored-data --portable
```

Restore publishes only after staging, member-hash checks, SQLite integrity/foreign-key checks, and
catalog/source/atom/receipt consistency checks pass. An existing target fails unless `--replace` is
explicit; replacement is limited to an empty or same-book one-book directory and retains the old
directory as a `.rollback-*` sibling. Stop the service before restore/replacement: the service and
restore share a cross-process `DATA_DIR` lock. A per-book archive never replaces a multi-book library.

`.rcbackup` files and retained rollback directories contain the full source EPUB and raw chapter text.
The CLI creates archives owner-only on POSIX, but they are not application-encrypted; keep them on
owner-controlled encrypted storage and delete them deliberately after the retention window. See
[`../docs/adr/0009-backup-export-and-recovery.md`](../docs/adr/0009-backup-export-and-recovery.md).

Setup and verification from the repository root:

```bash
cd backend
uv sync --extra dev --frozen
.venv/bin/python -m pytest tests -q -p no:cacheprovider
.venv/bin/python -m ruff check app tests/eval tests/api tests/llm
```

Run the service after making the existing root `.env` available to the process:

```bash
backend/.venv/bin/python -m uvicorn app.main:create_app --factory --app-dir backend \
  --host 127.0.0.1 --port 8000
```

Legacy stores created before LIT-7 do not have trustworthy completion receipts and intentionally clamp
to zero. Do not promote those rows manually. Preserve the source and existing data, then rebuild into
a fresh `DATA_DIR` before switching the running service.
