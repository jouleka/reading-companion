# ADR 0023 — Filesystem and S3-compatible source-object storage

**Status:** Accepted (2026-07-17; LIT-43)

## Context

The hosted schema has carried `source_objects` metadata since LIT-38, but LIT-41 intentionally kept
upload, EPUB streaming, and deletion unavailable. Direct filesystem paths cannot become a hosted API:
they would expose deployment layout, make owner scope optional, and prevent production object storage.
The source EPUB also contains the entire unread book, so a valid reader may receive it but another
tenant must not be able to address it by guessing either a book UUID or an object key.

The existing local/community app and accepted Karamazov library use the established
`data/books/<book_id>/source.epub` layout. This increment does not rewrite or migrate that library. The
new encrypted filesystem adapter is the development/single-host implementation of the hosted storage
interface; S3-compatible storage is the production implementation.

## Decision

### Clients address books, never objects

`SourceObjectRef` contains the verified session's typed `OwnerId` and a server-generated random UUID.
No HTTP path, query, body, or response accepts or returns the object UUID, storage key, bucket, or
filesystem path. PostgreSQL stores only the random UUID's 32-character hex form as `storage_key`.

Each adapter derives its physical address from both identities:

- filesystem: `<configured-root>/<owner-uuid>/<object-uuid>.epub.enc`;
- S3: `owners/<owner-uuid>/epubs/<object-uuid>.epub`.

Possession of another tenant's opaque object UUID is therefore insufficient: resolving it with the
caller's owner produces a different physical address. Repository lookup is independently owner-
predicated and RLS-backed before storage is called.

### One bounded policy applies to both adapters

Only non-empty `application/epub+zip` objects at or below `EPUB_MAX_UPLOAD_BYTES` are accepted. The
hosted HTTP body limit runs before multipart parsing; the route rechecks the exact file length and
media type, then the hardened existing EPUB parser rejects corrupt/DRM input before publication.

The route computes SHA-256. Both adapters recompute and compare it before writing. PostgreSQL records
the same checksum, media type, byte size, provider, encryption identity, and verification timestamp in
the same transaction that creates the owned book and zeroed reading state. A read must match both the
adapter's protected metadata and the PostgreSQL record before bytes are streamed to the reader.

The configured 128 MiB default is a deliberate whole-object verification bound. Reads authenticate
and checksum the complete bounded object before emitting the `StreamingResponse`, preventing a corrupt
tail from being discovered only after partial bytes have already reached the reader.

### Filesystem objects use authenticated encryption

The hosted filesystem adapter requires an operator-managed base64-encoded 256-bit key. Each object
uses a fresh 96-bit nonce and AES-256-GCM; media type, size, checksum, nonce, and encryption version are
authenticated as envelope metadata. Root/owner directories are mode `0700`, object envelopes are
created mode `0600`, writes fsync a same-directory temporary file and publish it without clobbering an
existing object identity. Authentication, metadata, size, and checksum failures fail closed.

### S3 writes require checksum and server-side encryption confirmation

The S3-compatible adapter uses Signature V4 and path-style addressing for custom endpoints. `PutObject`
includes content length/type, Content-MD5, SHA-256, private opaque metadata, `If-None-Match: *`, and
mandatory SSE. Supported modes are SSE-S3 (`AES256`, the default) and SSE-KMS (`aws:kms` with a required
key id). The response must confirm the configured algorithm and, for KMS, the exact key. Reads request
checksum mode, require SSE/content/size/checksum metadata, and recompute SHA-256. `ExpectedBucketOwner`
is available for AWS S3. Custom HTTP endpoints require an explicit development-only opt-in; HTTPS is
the default policy.

Static access credentials are optional so workload roles remain possible. If configured, access id
and secret are an all-or-nothing pair held as secret settings and never logged or persisted.

### Upload and delete coordinate database, storage, and runtime state

Upload serializes on the owner's library runtime key, writes the opaque object first, then atomically
creates `books`, `reading_state`, and `source_objects`. A metadata failure triggers idempotent object
compensation. The object identity is random, so a failed compensation leaves no client-addressable
path; an aggregate-only error is logged.

Source read and delete serialize on owner plus book UUID. Delete is CSRF-bound, deletes the physical
object idempotently, soft-deletes source/book metadata in one owner-scoped transaction, and invalidates
book metadata cache state. If the database step fails after physical deletion, the still-live metadata
makes retry possible; the next idempotent delete completes the soft deletion.

The enabled hosted additions are:

| Method and path | Behavior |
| --- | --- |
| `POST /api/books` | CSRF-bound validated upload; object key remains server-only |
| `GET /api/books/{book_id}/epub` | authenticated original EPUB stream for the owning reader |
| `DELETE /api/books/{book_id}` | CSRF-bound physical delete plus metadata/cache lifecycle |

Hosted export remains absent for LIT-50. Upload performs deterministic segmentation/profile detection
only; it does not counterfeit the durable ingestion job/attempt/lease model owned by LIT-44.

## Configuration

All hosted deployments require `HOSTED_STORAGE_BACKEND=filesystem` or `s3` at startup unless a test
injects an adapter.

Filesystem:

- `HOSTED_STORAGE_FILESYSTEM_ROOT` — dedicated object root;
- `HOSTED_STORAGE_FILESYSTEM_KEY` — secret base64 encoding of exactly 32 random bytes.

S3-compatible:

- `HOSTED_S3_BUCKET` and optional `HOSTED_S3_REGION`;
- optional `HOSTED_S3_ENDPOINT_URL` and development-only `HOSTED_S3_ALLOW_INSECURE_HTTP`;
- optional paired `HOSTED_S3_ACCESS_KEY_ID` / `HOSTED_S3_SECRET_ACCESS_KEY`;
- `HOSTED_S3_SSE_ALGORITHM=AES256` or `aws:kms`;
- `HOSTED_S3_KMS_KEY_ID` when using KMS; and
- optional `HOSTED_S3_EXPECTED_BUCKET_OWNER`.

## Verification

The executable contract proves:

- filesystem bytes are not plaintext, modes are restrictive, tampering fails authentication, limits
  and media/checksum policy fail closed, and deletion/existence are idempotent;
- identical opaque UUIDs resolve to distinct owner paths on filesystem and S3;
- S3 requests carry the required checksum/no-clobber/SSE fields, KMS uses the exact configured key,
  corrupt responses fail, and the requests satisfy botocore's real service model;
- PostgreSQL rejects client-shaped keys, wrong provider/media type, empty, unencrypted, unverified, or
  duplicate-live source metadata;
- owner A can upload/read/delete while owner B receives the same 404 as a missing UUID and cannot
  remove A's bytes;
- CSRF, content type, body size, database metadata, cache invalidation, physical cleanup, and
  soft-deletion are exercised through the real FastAPI/PostgreSQL composition; and
- configuration fails loud without a backend/key and settings representations mask secrets.

The real PostgreSQL 16 + pgvector hosted/parity gate passes 73 tests. The complete backend suite
passes 675 tests with 8 expected skips. The frontend remains 157 tests with a clean production build.

## Consequences and boundaries

Hosted source bytes now have one owner-safe interface and the previously blocked lifecycle routes are
usable. Local/community mode and its permanent files remain unchanged. The adapter deliberately uses
single-object operations under the existing 128 MiB policy; multipart upload is unnecessary at this
limit.

This does not add durable ingestion, cross-process job leases, quota accounting, provider keys, or a
public-launch claim. Operators must still provision and validate real bucket IAM, TLS, encryption/KMS,
versioning, retention, and recovery policy for their chosen S3-compatible service. LIT-44 is next and
must not start implicitly.
