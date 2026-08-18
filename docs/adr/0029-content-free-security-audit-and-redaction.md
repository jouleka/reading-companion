# ADR 0029: Security evidence uses a closed, content-free audit contract

- Status: Accepted
- Date: 2026-07-19
- Ticket: LIT-49 / SEC-2

## Context

Hosted operators need durable evidence for authentication, credential, import, deletion, provider,
and worker incidents. Ordinary logs and arbitrary JSON are unsafe evidence stores: provider errors can
contain keys, parser exceptions can contain EPUB text, HTTP diagnostics can contain authorization
headers, and model failures can contain complete prompts or responses. The schema had an
`audit_events` foundation but no write path, vocabulary, access contract, or retention procedure.

## Decision

Security-sensitive repository transitions write `audit_events` in the same database transaction as
the affected state. Events cover session login/logout, credential create/replace/delete, provider
setting update/validation, book import/delete, job cancellation, worker claim/start/fail/succeed, and
chapter commit. Every row has an opaque owner/actor boundary, a dotted action, target class and opaque
UUID where available, a closed result, and a database timestamp. Worker and tenant roles receive
INSERT only; auth receives INSERT through its already isolated BYPASSRLS bootstrap role. No runtime
role can read, update, or delete audit history.

The database and application accept only:

- actor kind `owner`, `worker`, or `system`;
- result `succeeded`, `denied`, or `failed`;
- bounded lowercase action/target identifiers; and
- optional metadata containing only one bounded `reason_code`.

There is no free-form audit field for email, display name, filename, title, text, request headers,
provider body, prompt, model response, exception, object key, credential label, or secret. Audit target
IDs are opaque UUIDs. Error and job persistence continue to use fixed reviewed failure codes.

Application logs have a machine-checked allow-list of two static operational messages. Adding a log
site or exception/structured-extra logging fails the static gate until the security policy is reviewed.
Hosted code never logs request bodies, headers, filenames, provider/parser exception strings, prompts,
model payloads, or source text. This structural exclusion is preferred to attempting to recognize and
mask arbitrary book prose after it has entered a logger.

Automated canaries submit a provider key, authorization header, EPUB chapter text, provider error
detail, and worker model payload. Tests scan captured logs, non-source API/error responses, job
payload/error rows, audit rows, and operator projections. Database constraints reject unreviewed audit
metadata. Explicit owned EPUB streaming remains a product response and is not treated as logging.

## Access and retention

There is no tenant or general admin HTTP audit endpoint. Production provisions a dedicated
non-superuser BYPASSRLS audit-operator role with schema usage and only `SELECT, DELETE` on
`audit_events`. `python -m app.hosted.audit show` projects only opaque IDs and the closed vocabulary.
The same tool's `purge --before` performs retention deletion without reading content tables.

The default operational retention is 90 days, with a daily purge using an explicit timezone-aware
cutoff. Operators suspend purge for a documented incident/legal hold and record the hold outside the
application database. Account deletion still cascades that owner's events as part of the privacy
deletion contract. Deployments with a different legal requirement must set and document a bounded
period before launch; indefinite retention is not the default.

## Consequences

- Useful incident evidence is durable and transactionally aligned with sensitive state changes.
- Audit storage cannot become a secondary reading-content, prompt, or credential database.
- Runtime compromise of a tenant/worker credential cannot read or erase audit history through its
  reviewed grants.
- Operator access is deliberately out of the product API and exposes no reading content or secrets.
- Client IP/user-agent collection, external SIEM export, and cryptographic append-only sealing are not
  claimed. They require separate privacy/threat-model decisions if introduced.
