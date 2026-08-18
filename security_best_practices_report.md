# Security best-practices review

Review date: 2026-08-18

Scope: the publishable `main` tree, with emphasis on the FastAPI boundary, hosted authentication and
tenant isolation, provider egress, untrusted EPUB rendering, PWA storage, dependency posture, secret
hygiene, and GitHub automation. This is a maintainer-assisted code review, not an independent audit or
penetration test.

## Executive summary

The local single-user application is suitable for publication as experimental self-hosted software
after the remediations below. No known plaintext credential, private book, runtime database, or
private infrastructure path remains in the publishable tree. The hosted mode is intentionally marked
incomplete and should not be presented as a production service.

The highest-risk issue was the absence of the Content Security Policy required by the vendored EPUB
renderer. EPUB markup is an active-content container rendered from same-origin blob URLs; without a
CSP, a malicious book could run script in the application boundary. The integrated server, Vite dev
server, and preview server now block inline/blob scripts and send complementary browser headers.

Automated verification completed with backend lint, 681 local tests, 218 frontend tests, a production
frontend build, Python and npm dependency audits, a high-severity Bandit scan, and the real
PostgreSQL/pgvector hosted plus parity suites.

## Findings

### RC-SEC-001 — High — Missing CSP around untrusted EPUB content — fixed

- Location: `backend/app/main.py:36-79`, `backend/app/main.py:395-396`,
  `frontend/vite.config.ts:4-42`.
- Impact: scripted EPUB content could have executed within the reader's same-origin rendering
  boundary, potentially reaching authenticated APIs or browser-stored reading data.
- Remediation: added a restrictive CSP (`script-src 'self'`, `script-src-attr 'none'`, explicit blob
  frame/worker allowances) plus `nosniff`, referrer, framing, and permissions headers to API, static,
  development, preview, and error responses. Regression tests assert the production headers.
- Residual note: any reverse proxy or alternate static host must preserve this policy or a stricter
  equivalent.

### RC-SEC-002 — Medium — Host-header and browser hardening were not enforced — fixed

- Location: `backend/app/config.py:35-40`, `backend/app/config.py:94-101`,
  `backend/app/main.py:395-396`.
- Impact: deployments relied on outer infrastructure to reject unexpected Host values and omitted
  standard response hardening.
- Remediation: added a configurable `TRUSTED_HOSTS` allowlist, rejected wildcard hosted
  configuration, installed `TrustedHostMiddleware`, and covered rejection plus security headers in
  tests.
- Deployment note: hosted operators must replace the loopback defaults with the exact public host.

### RC-SEC-003 — Medium — Cited answer grounding could accept an invented event — fixed

- Location: `backend/app/ask.py:105-141` and `backend/tests/test_ask.py:68-82`.
- Impact: names present in a citation could inflate lexical overlap enough for an uncited invented
  event to pass the deterministic gate, undermining the spoiler-safe answer contract.
- Remediation: visible proper-name tokens are now derived from raw cited passages and excluded from
  event traceability scoring. The previously failing invented-murder regression now passes.

### RC-SEC-004 — Medium — Hosted full-text GIN index is cross-tenant infrastructure — accepted risk

- Location: `backend/app/hosted/schema/0016_owner_scoped_book_search.sql:21-22` and the explicit
  review exception in `backend/tests/hosted/test_migrations.py:418-425`.
- Impact: PostgreSQL's built-in `tsvector` GIN index spans tenants. Explicit owner/book/incarnation
  predicates and forced row-level security prevent row disclosure, but the shared index can still
  create cross-tenant resource and coarse timing coupling.
- Mitigation: hosted mode remains non-production. Before a hosted launch, evaluate owner partitioning
  or a reviewed multicolumn `btree_gin` design, then repeat adversarial tenant tests and load testing.
- False-positive note: this is not a direct row-access bypass in the current query/RLS design.

### RC-SEC-005 — Low — Offline reading stores sensitive content in browser storage — accepted behavior

- Location: `frontend/public/sw.js:1-106` and `frontend/src/pwa/offline.ts:42-188`.
- Impact: an explicitly saved EPUB, marks, preferences, and queued changes remain available to the
  local browser profile for offline use. Anyone with access to that profile may read them.
- Existing mitigation: caches and mutations are owner-namespaced; owner changes, logout, invalid
  sessions, and expiry purge owner data. Authentication endpoints are never cached.
- Recommendation: do not use offline saving in a shared browser profile; add an explicit UI warning
  before a future hosted production release.

### RC-SEC-006 — Low — Local mode is intentionally unauthenticated — accepted behavior

- Location: local routing in `backend/app/main.py:384-394` and loopback launch instructions in
  `README.md`.
- Impact: binding local mode to a public interface would expose the library and write endpoints with
  no login boundary.
- Existing mitigation: documented loopback-only commands and separate hosted authentication mode.
- Recommendation: never expose local mode directly to a LAN or the internet.

### RC-SEC-007 — Informational — Non-security MD5 triggered a static-analysis alert — fixed

- Location: `backend/app/llm/client.py:461-469`.
- Impact: none; MD5 is used only for deterministic feature bucketing, not authentication or
  integrity.
- Remediation: marked the call `usedforsecurity=False` and documented the purpose. The high-severity
  Bandit gate now passes.

### RC-SEC-008 — High — Local book IDs reached filesystem paths without local validation — fixed

- Location: `backend/app/ingest/manifest.py` and `backend/app/api/books.py`.
- Impact: normal imports create content-derived IDs and reads first require a matching catalog row,
  so an HTTP client could not normally choose an arbitrary stored path. However, the filesystem
  boundary relied on that upstream invariant and CodeQL correctly identified the missing local
  validation.
- Remediation: every manifest and source-EPUB path now requires a bounded lowercase alphanumeric
  book ID before constructing a path. Traversal, separators, uppercase IDs, empty IDs, and malformed
  prefixes have regression coverage.

### RC-SEC-009 — Informational — Vendored PDF.js filename normalization alert — false positive

- Location: `frontend/src/vendor/foliate-js/vendor/pdfjs/pdf.worker.mjs`.
- Triage: the flagged replacement order deliberately normalizes PDF escape sequences, after which
  PDF.js applies `stripPath` before returning the serializable attachment filename. Current upstream
  [documents that the order is intentional](https://github.com/mozilla/pdf.js/blob/master/src/core/file_spec.js).
- Disposition: dismissed as a CodeQL false positive rather than modifying generated third-party
  code. Continue updating the vendored Foliate/PDF.js bundle as an atomic upstream dependency.

## Additional controls reviewed

- Hosted cookies are `Secure`, `SameSite=Lax`, and HttpOnly where appropriate; unsafe requests use a
  session-bound double-submit CSRF token (`backend/app/hosted/auth/api.py:52-70` and `:201-214`).
- OIDC uses state, an independent browser binding, PKCE S256, nonce, pinned signed algorithms, exact
  issuer/audience checks, bounded documents, and no redirect following.
- User-selected provider URLs are restricted to configured HTTPS origins and validation requests do
  not follow redirects (`backend/app/hosted/provider_settings.py:80-123`).
- Upload and credential request bodies are bounded before parsing; EPUB ZIP/XML processing has
  adversarial regression coverage.
- API documentation is disabled by default, hosted API responses are private/no-store, credentials
  use envelope encryption, and provider errors are reduced to fixed public classes.
- Secret scanning, CodeQL, dependency review, Dependabot, pinned Actions, Python lint/tests/audit,
  and frontend tests/build/audit are configured under `.github/`.

## Static-analysis triage

Bandit medium-confidence `B608` findings were manually sampled. They build SQL from closed internal
table/column maps, fixed predicate fragments, or parameter-placeholder counts while values remain
bound parameters. They are not treated as confirmed injection paths. The release workflow fails on
high-severity Bandit findings; dynamic SQL should continue to receive focused review when modified.

## Release conclusion

The cleaned tree is ready to be public as an experimental local-first project. Keep hosted mode
labeled incomplete until RC-SEC-004, production operations, monitoring, backup/restore drills, and an
independent security assessment are addressed.
