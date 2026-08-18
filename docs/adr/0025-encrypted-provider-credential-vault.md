# ADR 0025 - Encrypted hosted provider credential vault

**Status:** Accepted (2026-07-19; LIT-45)

## Context

Hosted ingestion jobs deliberately carried only a credential UUID placeholder. Storing a provider key
in a job, settings JSON, response, trace, or ordinary database string would make tenant isolation and
deletion unverifiable. The predeclared PostgreSQL table had ciphertext-shaped columns but no encryption,
API, worker resolution, rotation operation, or leak tests.

## Decision

### Every credential has a random data key

Submission generates a 256-bit data-encryption key (DEK) and two independent 96-bit nonces. AES-256-GCM
encrypts the provider secret with the DEK. A versioned deployer-managed AES-256-GCM master key encrypts
only that DEK. Authenticated additional data binds both layers to the format version, owner UUID,
credential UUID, provider, and key version, so copying an envelope across owners, credentials, providers,
or versions cannot decrypt it.

PostgreSQL stores the provider, a four-character masked suffix, ciphertext, wrapped DEK, algorithm,
master-key version, and secret nonce. Migration constraints require the exact algorithm and envelope
shape. Plaintext, API-key, token, and secret columns do not exist. Live labels are not unique because two
unrelated credentials may legitimately share a suffix.

### The API is write-only for secret material

`POST /api/credentials` accepts one provider and secret through a CSRF-bound authenticated request.
`GET /api/credentials` returns only owned metadata. `PUT /api/credentials/{id}` replaces the secret in
a fresh envelope while retaining the server credential UUID. `DELETE` destroys the live ciphertext and
wrapped DEK before marking the row deleted. Foreign UUIDs have the same 404 response as missing UUIDs.

Secret-bearing JSON is parsed through a narrow non-reflecting path. Invalid fields, types, provider IDs,
lengths, whitespace, and line breaks return a fixed error rather than a framework validation structure
that could echo input. Responses are private/no-store and never contain ciphertext, wrapped keys, nonces,
or submitted values.

### Worker resolution is claim-fenced and just in time

The exact-grant worker role gains SELECT only on `provider_credentials`; the tenant HTTP role gains the
SELECT/INSERT/UPDATE needed by the metadata API. A worker may resolve only the credential attached to its
currently running job while its owner/job/attempt/worker/token-digest lease is still live. Disabled,
deleted, unbound, cross-owner, stale-lease, corrupt, and unknown-key envelopes all produce the same
content-free unavailable error. The caller receives a redacted context-managed buffer and closes it
immediately after provider use. Unavailable credentials become the fixed non-retryable
`provider_rejected` job failure; exception text is never persisted.

### Master-key rotation rewraps without exposing provider secrets

`HOSTED_CREDENTIAL_MASTER_KEY` and `HOSTED_CREDENTIAL_KEY_VERSION` select the active deployer key.
`HOSTED_CREDENTIAL_PREVIOUS_MASTER_KEYS` temporarily supplies the old versioned keys. The
`reading-companion-credential-rewrap` command locks bounded batches, decrypts only each DEK, wraps it with
the active master key, and leaves the provider ciphertext and nonce unchanged. Operators remove an old
master key only after the command reports no remaining envelopes at that version. Provider-secret
replacement is separate and always creates a new DEK.

## Verification

Pure tests prove randomized envelopes, round trips, owner/AAD tamper rejection, redacted representations,
bounded inputs, deterministic buffer closing, active/previous key configuration, and DEK-only rewrap.
Real PostgreSQL tests cover strict envelope constraints, persistent rewrap, metadata-only create/list/
replace/delete APIs, cross-tenant missing equivalence, destructive deletion, exact tenant/worker grants,
claim-fenced just-in-time resolution, and disabled-credential refusal. Canary values are searched across
responses, exceptions, representations, and stored envelope bytes.

The current checkout collects 708 backend tests. On Windows, the compatible gate passes 642 with 62
PostgreSQL/environment skips and four pre-existing POSIX-only filesystem/lock tests deselected; Ruff and
JSON/diff checks are clean. The committed PostgreSQL workflow remains the authoritative real-database
gate.

## Consequences and boundaries

The hosted service can now accept and retain per-owner provider credentials without plaintext
persistence or read-back. Master-key material remains deployment secret state and must be backed up and
rotated independently of the database. Logical deletion destroys the live envelope but cannot promise
physical erasure from PostgreSQL MVCC pages or pre-existing backups; backup retention and account erasure
remain lifecycle policy.

LIT-46 still owns provider/model capability routing and the concrete ingestion handler that attaches a
credential ID to a job and performs provider calls. No provider was contacted, no real credential was
used, local/community mode was unchanged, and this is not yet a public-launch claim.
