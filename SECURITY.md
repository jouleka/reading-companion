# Security policy

## Supported versions

Only the latest commit on `main` is considered for security fixes. This project is experimental and
has not received an independent security audit. Hosted mode is not advertised as production-ready.

## Reporting a vulnerability

Do not disclose suspected vulnerabilities, credentials, private book content, databases, private
infrastructure, or exploit details in a public issue.

Use GitHub's **Security > Report a vulnerability** form for this repository. If private vulnerability
reporting is unavailable, contact the maintainer through a private channel listed on their GitHub
profile. Include the affected commit, impact, required preconditions, and a minimal reproduction that
does not contain copyrighted or private book text.

## Security boundaries

- Local mode has no user authentication and must remain bound to loopback or another trusted private
  boundary.
- EPUB files are untrusted active-content containers. Keep the shipped Content Security Policy (or a
  stricter equivalent) when serving the frontend.
- Hosted mode requires explicit trusted hosts, HTTPS OIDC configuration, isolated database roles,
  encrypted object storage, and environment-specific review.
- `.env`, EPUBs, databases, backups, provider keys, and runtime logs must never be committed.
