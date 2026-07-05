#!/usr/bin/env bash
# One-command collab two-browser e2e runner (Slice 3b-5d.D0; reused by D4).
#
#   scripts/collab_e2e_run.sh [extra playwright args...]
#
# Boots scripts/collab_e2e_seed.py (testcontainers PG + uvicorn:8000, SPA_MODE=dev),
# parses the printed E2E_* vars, starts Vite dev (:5173), waits for both ports,
# then runs frontend/e2e/collab.spec.ts. Tears everything down on exit.
#
# Requirements: `uv` (the seed runs via `uv run`, no pre-activated venv needed),
# frontend deps installed (pnpm i), and a Chromium usable by Playwright. On a fresh
# machine the browser OS libs may be missing:
#   cd frontend && pnpm exec playwright install --with-deps chromium   (needs sudo)
# Chromium itself is cached at ~/.cache/ms-playwright/chromium-1223.
set -euo pipefail
# Job control ON: each `&` job gets its OWN process group whose leader PID == $!,
# so `kill -SIG -"$PGID"` signals the whole subtree (uv->python, pnpm->esbuild/node).
# NB: do NOT use `setsid` here — it forks, so $! would be a short-lived parent, not
# the detached child, and cleanup would miss the real process (orphaning port 8000).
set -m

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SEED_LOG="$(mktemp)"
VITE_LOG="$ROOT/.e2e-vite.log"
SEED_PGID=""
VITE_PGID=""

cleanup() {
  # SIGINT the seed's process GROUP so the seed's KeyboardInterrupt handler runs
  # (it sets server.should_exit and lets the `with PostgresContainer` context stop
  # the docker container). Plain SIGTERM would skip that handler and orphan the PG
  # container (Ryuk reaps it eventually, but slowly). Give it a moment, then hard-kill.
  if [ -n "$SEED_PGID" ]; then
    kill -INT -"$SEED_PGID" 2>/dev/null || true
    sleep 2
    kill -KILL -"$SEED_PGID" 2>/dev/null || true
  fi
  # Vite spawns esbuild/node children; kill the whole group (its leader is VITE_PGID).
  [ -n "$VITE_PGID" ] && kill -TERM -"$VITE_PGID" 2>/dev/null || true
}
trap cleanup EXIT

echo "[run] starting seed (testcontainers PG + uvicorn:8000)…"
# `uv run python -u` → unbuffered stdout so the printed E2E_* vars reach $SEED_LOG
# immediately, and no pre-activated venv required.
env EMBEDDING_MODEL=bge-m3 uv run python -u scripts/collab_e2e_seed.py >"$SEED_LOG" 2>&1 &
SEED_PGID=$!

# Wait up to ~150s for the seed to print the workspace id (PG image pull + migrate).
for _ in $(seq 1 300); do
  grep -q '^E2E_COLLAB_WS_ID=' "$SEED_LOG" && break
  kill -0 "$SEED_PGID" 2>/dev/null || { echo "[run] seed exited early:"; cat "$SEED_LOG"; exit 1; }
  sleep 0.5
done
grep -q '^E2E_COLLAB_WS_ID=' "$SEED_LOG" || { echo "[run] seed never printed vars:"; tail -40 "$SEED_LOG"; exit 1; }

get() { grep "^$1=" "$SEED_LOG" | head -1 | cut -d= -f2-; }
export E2E_COLLAB_WS_ID; E2E_COLLAB_WS_ID="$(get E2E_COLLAB_WS_ID)"
export E2E_COOKIE_OWNER; E2E_COOKIE_OWNER="$(get E2E_COOKIE_OWNER)"
export E2E_COOKIE_MEMBER; E2E_COOKIE_MEMBER="$(get E2E_COOKIE_MEMBER)"
export E2E_SESSION_COOKIE; E2E_SESSION_COOKIE="$(get E2E_SESSION_COOKIE)"
export E2E_OWNER_LABEL; E2E_OWNER_LABEL="$(get E2E_OWNER_LABEL)"
export E2E_MEMBER_LABEL; E2E_MEMBER_LABEL="$(get E2E_MEMBER_LABEL)"
echo "[run] workspace=$E2E_COLLAB_WS_ID owner=$E2E_OWNER_LABEL member=$E2E_MEMBER_LABEL"

echo "[run] starting Vite dev (:5173)…"
( cd frontend && exec pnpm dev ) >"$VITE_LOG" 2>&1 &
VITE_PGID=$!

echo "[run] waiting for uvicorn:8000 + vite:5173…"
# curl WITHOUT -f on purpose: a 401 from /api/me (unauthenticated) still proves the
# port is up and serving. -f would treat that 401 as failure and never go ready.
ready=0
for _ in $(seq 1 120); do
  if curl -s -o /dev/null "http://127.0.0.1:8000/api/me" \
     && curl -s -o /dev/null "http://localhost:5173/"; then
    ready=1
    break
  fi
  sleep 0.5
done
if [ "$ready" -ne 1 ]; then
  echo "[run] servers not ready after ~60s — aborting."
  echo "--- seed log (tail) ---"; tail -30 "$SEED_LOG"
  echo "--- vite log (tail) ---"; tail -30 "$VITE_LOG"
  exit 1
fi

echo "[run] running collab e2e…"
cd frontend
pnpm exec playwright test e2e/collab.spec.ts "$@"
