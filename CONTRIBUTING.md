# Contributing

Thanks for helping improve Reading Companion.

## Before opening a change

- Use synthetic text or a clearly public-domain fixture. Never commit private/copyrighted books,
  provider keys, databases, backups, logs, or private infrastructure details.
- Preserve the no-look-ahead boundary: future book content must never influence an earlier reading
  position, retrieval result, or generated view.
- Keep the EPUB renderer's Content Security Policy intact.
- Add a regression test for changed security, spoiler-boundary, tenant-isolation, or reader behavior.
- Keep changes focused and document any hosted/local behavior difference.

## Development checks

```bash
cd backend
uv sync --extra dev --frozen
uv run ruff check .
uv run pytest
uv run pip-audit
uv run bandit -q -lll -r app

cd ../frontend
npm ci
npm test
npm run build
npm audit --audit-level=high
```

PostgreSQL/pgvector changes must also pass `backend/scripts/test_postgres.sh` or the equivalent
`PostgreSQL schema` GitHub Actions job.

## Security reports

Do not open a public issue for a suspected vulnerability. Follow [`SECURITY.md`](SECURITY.md).
