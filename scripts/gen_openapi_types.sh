#!/usr/bin/env bash
# Generate frontend/src/api/schema.gen.ts from FastAPI's OpenAPI schema WITHOUT a
# running server or a reachable database: create_app().openapi() builds the
# schema from the route definitions, and SQLAlchemy's engine is created lazily so
# no DB connection is attempted. Used to (re)generate the committed types AND by
# the CI drift check. The package.json `gen:api` script is the HTTP equivalent
# for dev when a uvicorn is already running; both emit the byte-identical file
# (same OpenAPI doc -> same openapi-typescript output, pinned version).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

# Dummy-but-valid env: the psycopg3 dialect URL lets create_engine import the
# driver without connecting; SPA_MODE=disabled keeps the schema to the real API.
DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://u:p@localhost:5432/db}" \
  SESSION_SECRET="${SESSION_SECRET:-test-secret-at-least-16-chars}" \
  ALLOWED_EMAIL_DOMAINS="${ALLOWED_EMAIL_DOMAINS:-ki.se}" \
  APP_BASE_URL="${APP_BASE_URL:-http://localhost:8000}" \
  SPA_MODE=disabled \
  uv run python -c "import json,sys; from call_manager.main import create_app; json.dump(create_app().openapi(), sys.stdout, sort_keys=True)" >"$tmp"

mkdir -p "$ROOT/frontend/src/api"
cd "$ROOT/frontend"
pnpm exec openapi-typescript "$tmp" -o src/api/schema.gen.ts
echo "wrote frontend/src/api/schema.gen.ts"
