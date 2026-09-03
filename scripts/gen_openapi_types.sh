#!/usr/bin/env bash
# Generate frontend/src/api/schema.gen.ts from FastAPI's OpenAPI schema WITHOUT a
# running server or a reachable database: create_app().openapi() builds the
# schema from the route definitions, and SQLAlchemy's engine is created lazily so
# no DB connection is attempted. Used to (re)generate the committed types AND by
# the CI drift check. The package.json `gen:api` script DELEGATES to this script
# (bash ../scripts/gen_openapi_types.sh), so dev regeneration and CI run the one
# canonical generator — same SPA_MODE=disabled, same sort_keys ordering — and can
# never drift (an earlier HTTP form scraped a live server and diverged by route
# order + a dev-only /events/ping path, breaking the gate).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

# Dummy-but-valid env: the psycopg3 dialect URL lets create_engine import the
# driver without connecting; SPA_MODE=disabled keeps the schema to the real API.
#
# EMBEDDING_MODEL is PINNED to bge-m3 (overriding any value in a local .env)
# because create_app() logs a calibration WARNING to STDOUT when the configured
# model isn't calibrated (e.g. the repo .env's EMBEDDING_MODEL=mxbai-embed-large).
# That WARNING line is captured into "$tmp" alongside the JSON and corrupts it,
# producing a garbage schema.gen.ts on local `pnpm gen:api`. Pinning here makes
# generation hermetic and identical to CI (which has no .env), so local and CI
# always agree regardless of the developer's .env.
#
# `--python 3.12` pins the INTERPRETER for the same reason. FastAPI takes its
# default response descriptions from http.client.responses, and CPython 3.13
# renamed two of them (413 "Request Entity Too Large" -> "Content Too Large",
# 422 "Unprocessable Entity" -> "Unprocessable Content"). requires-python is
# ">=3.12", so a developer on 3.13+ silently rewrote ~346 lines of schema.gen.ts
# that CI (uv python install 3.12) could never reproduce — the drift check then
# fails for everyone with no source change. This is the project's MINIMUM
# supported version and must move together with CI's pin.
DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://u:p@localhost:5432/db}" \
  SESSION_SECRET="${SESSION_SECRET:-test-secret-at-least-16-chars}" \
  ALLOWED_EMAIL_DOMAINS="${ALLOWED_EMAIL_DOMAINS:-umu.se}" \
  APP_BASE_URL="${APP_BASE_URL:-http://localhost:8000}" \
  EMBEDDING_MODEL=bge-m3 \
  SPA_MODE=disabled \
  uv run --python 3.12 python -c "import json,sys; from call_manager.main import create_app; json.dump(create_app().openapi(), sys.stdout, sort_keys=True)" >"$tmp"

mkdir -p "$ROOT/frontend/src/api"
cd "$ROOT/frontend"
pnpm exec openapi-typescript "$tmp" -o src/api/schema.gen.ts
echo "wrote frontend/src/api/schema.gen.ts"
