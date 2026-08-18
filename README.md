# Reading Companion

Reading Companion is a self-hosted EPUB reader that builds a structured memory of the story as you
read. It helps you remember characters, relationships, events, and where you stopped without showing
material beyond your saved reading frontier.

The interface is called **Litlet**. The repository contains the FastAPI service, React reader, local
SQLite story-memory engine, and an experimental hosted multi-user foundation.

> **Project status:** the local, single-user application is usable and covered by automated tests.
> The hosted mode is an incomplete engineering foundation, not a ready-to-deploy public service.
> Backups, restore drills, deployment configuration, monitoring, and an independent security review
> remain operator responsibilities.

## Features

- Import and read DRM-free EPUB files you are allowed to use.
- Maintain a chapter-bounded, bitemporal story memory.
- Catch up with an evolving recap and spoiler-bounded chapter views.
- Browse name cards, relationships, highlights, notes, and bookmarks.
- Ask questions and run selection assistance against completed reading only.
- Read aloud with browser speech synthesis.
- Install the reader as a PWA and queue reading-position changes while offline.
- Use SQLite and `sqlite-vec` locally; exercise PostgreSQL/pgvector parity in the hosted test suite.

## Spoiler-safety model

Derived facts are stamped with the chapter that revealed them. Reader-facing queries are constrained
to the server-authoritative bookmark, and the retrieval/evaluation suites contain adversarial
no-look-ahead checks. This is a defense-in-depth design, not a promise that model output can never be
wrong; keep the deterministic gates and regression tests enabled when changing ingestion or views.

The original design is in
[`docs/spec/2026-06-25-reading-companion-design.md`](docs/spec/2026-06-25-reading-companion-design.md),
and accepted architecture decisions are recorded under [`docs/adr`](docs/adr).

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- Node.js 20+ and npm
- A modern browser
- Optional: an OpenAI-compatible or Anthropic API key
- Optional: PostgreSQL 16 with pgvector for the hosted/parity suite

## Quick start

Clone the repository, create a local configuration, and install locked dependencies:

```bash
git clone https://github.com/jouleka/reading-companion.git
cd reading-companion
cp .env.example .env

cd backend
uv sync --extra dev --frozen

cd ../frontend
npm ci
```

Start the API in one terminal:

```bash
cd backend
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Start the CSP-enabled development frontend in another:

```bash
cd frontend
npm run dev
```

Open <http://localhost:5173>. The example configuration uses the deterministic offline stub, so you
can explore without a provider key. Add your own key to the ignored `.env` file and set
`ALLOW_STUB=false` when you want real extraction. Never commit that file.

### Single-process local build

```bash
cd frontend
npm run build

cd ../backend
FRONTEND_DIST_DIR="$(pwd)/../frontend/dist" \
  uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 5173 \
  --no-proxy-headers --no-server-header
```

The application sets a restrictive Content Security Policy because EPUB markup is untrusted. Do not
serve the built frontend through a different proxy or static host unless it preserves equivalent
security headers.

## Verification

```bash
cd backend
uv run ruff check .
uv run pytest

cd ../frontend
npm test
npm run build
npm audit --audit-level=high
```

The PostgreSQL workflow applies the real migrations and runs the hosted plus SQLite/PostgreSQL parity
suites against a pinned pgvector image.

## Repository layout

```text
backend/    FastAPI API, local memory engine, hosted foundation, and tests
frontend/   React/TypeScript reader, PWA support, and vendored Foliate renderer
docs/       Product design, ADRs, accessibility notes, and public runbooks
scripts/    Optional loopback-only local service helpers
spikes/     Reproducible research experiments and their synthetic/public fixtures
```

## Privacy and security

- Imported books, databases, `.env`, and generated data directories are ignored by Git.
- API documentation is disabled by default.
- The integrated server and Vite development server send browser hardening headers, including the
  CSP required by the vendored EPUB renderer.
- Hosted sessions are opaque, server-side, secure-cookie sessions with CSRF protection; hosted mode
  still requires environment-specific deployment review.
- Report suspected vulnerabilities privately as described in [`SECURITY.md`](SECURITY.md).

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Public bug reports must use synthetic or public-domain
fixtures and must not include books, credentials, databases, or private infrastructure details.

## License

Original project code is available under the [MIT License](LICENSE). Vendored components keep their
own licenses; see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and the license files beside
those components.
