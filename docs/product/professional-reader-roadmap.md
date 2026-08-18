# Professional reader roadmap

**Accepted:** 2026-07-16

**Historical epic IDs:** LIT-35 hosted multi-user platform;
LIT-36 professional reader experience

**Architecture gate:** [ADR 0017](../adr/0017-hosted-multi-user-architecture.md)

## Product standard

Litlet should earn its place as the reader for books that are hard to return to. The dependable reader
comes first: import, navigation, position, typography, mobile use, offline recovery, and annotations.
The companion is the differentiator: it should reduce confusion at the exact point the reader asks
“where am I, who is this, and what did I miss?” without revealing the future.

The professional bar is not feature count. A reader must be able to trust that the app keeps their
place, their books, their notes, their provider credentials, their privacy, and the spoiler boundary.

## Ordered delivery

### Gate 0 — Make hosting safe

This is release-blocking infrastructure, not optional polish.

| Order | Ticket | Outcome |
| ---: | --- | --- |
| 1 | LIT-37 / PLAT-1 | Accepted architecture and ownership contract |
| 2 | LIT-38 / PLAT-2 | PostgreSQL/pgvector schema and migrations |
| 3 | LIT-39 / PLAT-3 | SQLite/PostgreSQL spoiler parity |
| 4 | LIT-40 / AUTH-1 | OIDC users and secure sessions |
| 5 | LIT-41 / TENANT-1 | Owner-scoped repositories and APIs |
| 6 | LIT-42 / TENANT-2 | Tenant-scoped caches, locks, and lifecycle |
| 7 | LIT-43 / STORE-1 | Local/S3 EPUB storage boundary |
| 8 | LIT-44 / JOBS-1 | Durable leased ingestion worker |
| 9 | LIT-45 / BYOK-1 | Encrypted per-user credential vault |
| 10 | LIT-46 / BYOK-2 | Provider/model setup and validation |
| 11 | LIT-47 / LIMIT-1 | Atomic per-user limits |
| 12 | LIT-48 / SEC-1 | Cross-tenant adversarial release gate |
| 13 | LIT-49 / SEC-2 | Audit, redaction, and secret-leak gate |
| 14 | LIT-50 / MIGRATE-1 | Verified local-to-hosted migration |

Development can overlap where dependencies allow, but the public launch gate stays closed until this
whole slice is green. In particular, login does not precede the owner-scoped persistence contract in
production, and raw keys never pass through the durable job payload.

### Reader wave 1 — Trust the reading surface

1. LIT-53 / SYNC-1: cross-device position with explicit
   rewind and deterministic conflict handling.
2. LIT-52 / UX-2: professional typography, themes,
   measure, and accessible controls.
3. LIT-56 / NAV-1: hierarchical TOC, in-book search,
   and back/forward history.
4. LIT-51 / UX-1: mobile reading shell with a companion
   bottom sheet that never covers or loses the place.

This wave makes Litlet credible as a primary reader even when AI is disabled.

### Reader wave 2 — Keep and use what matters

1. LIT-55 / NOTE-1: durable highlights, notes, and
   bookmarks with portable anchors and export.
2. LIT-54 / PWA-1: installable offline reading, queued
   progress/annotation sync, and complete tenant purge on logout.

This wave targets the phone and intermittent-connectivity reality without pretending a web app can
import DRM-locked Kindle content.

### Reader wave 3 — Make the companion meaningfully better

1. LIT-57 / AI-1: spoiler-safe “Ask the Book” answers
   with navigable citations and visible provider cost.
2. LIT-58 / AI-2: explain/define/translate selection
   actions and a useful chapter closeout.
3. LIT-59 / QUALITY-1: reader-visible correction of
   mistaken memory with provenance and spoiler-safe history.
4. LIT-60 / TTS-1: synchronized, accessible TTS with
   honest provider/cost behavior.

AI features depend on stable owner state, BYOK, cited retrieval, and PostgreSQL spoiler parity. They do
not block the trustworthy-reader waves.

## Experience measures

Release reviews use observable outcomes rather than page views alone:

- **Resume trust:** zero unexplained position regressions in conflict/offline tests; return readers can
  resume the correct passage in one action.
- **Reading comfort:** no horizontal overflow at supported phone widths; WCAG AA contrast, keyboard,
  screen-reader, zoom, focus, and reduced-motion checks pass.
- **Navigation:** a reader can reach a TOC location, search result, annotation, cited passage, and their
  prior location without losing navigation history.
- **Import usefulness:** supported DRM-free EPUBs either become readable with truthful progress or fail
  with an actionable, non-destructive explanation.
- **Companion quality:** answers and generated chapter closeouts cite revealed text; insufficient
  evidence is preferred to invention; spoiler escapes remain zero in the blocking harness.
- **Privacy and tenancy:** cross-tenant reads/writes and plaintext credential persistence remain zero in
  the adversarial suites.
- **Cost clarity:** any provider-backed action states who pays, which provider/model runs, and the
  estimated or measured cost available to the user.

## Later, only after usage evidence

Organization libraries, social reading, public/shared annotations, hosted subscription model usage,
native mobile clients, Kindle highlight sync, and collaboration can be explored later. They add new
privacy, moderation, billing, or platform constraints and should not delay a secure personal reading
product.
