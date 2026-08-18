#!/usr/bin/env bash
set -euo pipefail

container="reading-companion-lit38-postgres"
port="${TEST_POSTGRES_PORT:-55439}"
image="pgvector/pgvector:pg16@sha256:1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb"

cleanup() {
  docker stop "${container}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run --rm --detach --name "${container}" \
  --env POSTGRES_HOST_AUTH_METHOD=trust \
  --publish "127.0.0.1:${port}:5432" \
  "${image}" >/dev/null

for _ in $(seq 1 30); do
  if docker exec "${container}" pg_isready --username postgres >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "${container}" pg_isready --username postgres >/dev/null

cd "$(dirname "${BASH_SOURCE[0]}")/.."
TEST_POSTGRES_DSN="postgresql://postgres@127.0.0.1:${port}/postgres" \
  .venv/bin/python -m pytest tests/hosted tests/parity -q -p no:cacheprovider
