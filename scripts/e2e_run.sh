#!/usr/bin/env bash
# One-command collab two-browser e2e runner (Slice 3b-5d.D0; reused by D4).
#
#   scripts/collab_e2e_run.sh [extra playwright args...]
#
# Two modes, selected by E2E_SPA_MODE (default "dev" — unchanged, byte-identical
# behavior from D0):
#   dev  (default) — boots scripts/collab_e2e_seed.py (testcontainers PG +
#                     uvicorn:8000, SPA_MODE=dev), starts Vite dev (:5173),
#                     waits for both ports; browser talks to :5173 under the
#                     dev CSP's broad ws: token.
#   prod (E2E_SPA_MODE=prod) — builds the SPA (`pnpm build`), syncs
#                     frontend/dist/ -> src/grant_lab/web/spa/ (gitignored),
#                     boots the seed with SPA_MODE=prod so uvicorn:8000 serves
#                     the built bundle directly under the STRICT CSP (Task 10's
#                     same-origin ws token derived from APP_BASE_URL) — the
#                     3b-5d.D4 flip gate. No Vite, no :5173.
# Either way, parses the seed's printed E2E_* vars then runs
# frontend/e2e/collab.spec.ts (playwright.config.ts's E2E_MODE switch, reused
# from the Plan-3 dashboard e2e, picks the matching baseURL). Tears everything
# down on exit.
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

E2E_SPA_MODE="${E2E_SPA_MODE:-dev}"
case "$E2E_SPA_MODE" in
  dev|prod) ;;
  *)
    echo "[run] E2E_SPA_MODE must be 'dev' or 'prod' (got '$E2E_SPA_MODE')"
    exit 1
    ;;
esac

SEED_LOG="$(mktemp)"
VITE_LOG="$ROOT/.e2e-vite.log"
SEED_PGID=""
VITE_PGID=""

cleanup() {
  # SIGINT the seed's process GROUP so the seed's KeyboardInterrupt handler runs
  # (it sets server.should_exit and lets the `with PostgresContainer` context stop
  # the docker container). Plain SIGTERM would skip that handler and orphan the PG
  # container (Ryuk reaps it eventually, but slowly).
  if [ -n "$SEED_PGID" ]; then
    kill -INT -"$SEED_PGID" 2>/dev/null || true
    # PostgresContainer.__exit__ (stop + remove the docker container) can take a
    # while — wait up to ~20s for the group to exit BEFORE hard-killing, so a fixed
    # timeout doesn't SIGKILL mid-cleanup and leak the container.
    for _ in $(seq 1 40); do
      kill -0 -"$SEED_PGID" 2>/dev/null || break
      sleep 0.5
    done
    kill -KILL -"$SEED_PGID" 2>/dev/null || true
  fi
  # Vite spawns esbuild/node children; TERM the whole group, wait briefly, then KILL.
  if [ -n "$VITE_PGID" ]; then
    kill -TERM -"$VITE_PGID" 2>/dev/null || true
    for _ in $(seq 1 10); do kill -0 -"$VITE_PGID" 2>/dev/null || break; sleep 0.3; done
    kill -KILL -"$VITE_PGID" 2>/dev/null || true
  fi
  # Nothing was launched, so there is nothing of ours to back-stop and the ports
  # belong to somebody else. The trap is installed above the preflight, so
  # WITHOUT this the preflight's own `exit 1` ran the loop below and SIGKILLed
  # the very server it had just refused to run alongside — as did a failed
  # `pnpm build` in prod mode, and the check-ignore refusal.
  if [ -z "$SEED_PGID" ] && [ -z "$VITE_PGID" ]; then
    return 0
  fi
  # Backstop: the Vite node can escape the group-kill (esbuild service detaches),
  # leaving :5173 held. Free the ports by whoever still listens so the NEXT run's
  # preflight isn't tripped by our own leak. Safe only because the preflight
  # above already proved both ports were free before we started anything.
  for port in 5173 8000; do
    local pid
    pid="$(ss -ltnp "sport = :$port" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
    [ -n "${pid:-}" ] && kill -KILL "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT

# Preflight: refuse to run if a port we're about to use is already in use.
# Otherwise the readiness probe could validate a STALE server/DB from a previous
# (orphaned) run while our fresh workspace is unreachable (Codex High). prod mode
# never starts Vite, so it only needs :8000 free.
PREFLIGHT_PORTS=(8000)
[ "$E2E_SPA_MODE" = "dev" ] && PREFLIGHT_PORTS+=(5173)
for port in "${PREFLIGHT_PORTS[@]}"; do
  if ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN; then
    echo "[run] port $port already in use — aborting to avoid validating a stale server."
    ss -ltnp "sport = :$port" 2>/dev/null || true
    exit 1
  fi
done

if [ "$E2E_SPA_MODE" = "prod" ]; then
  # Safety net BEFORE touching the tree: refuse to sync the built bundle into
  # src/grant_lab/web/spa/ unless it's gitignored (the trailing slash makes
  # check-ignore evaluate the directory-only .gitignore pattern even before the
  # directory exists — without it, a nonexistent path spuriously reports "not
  # ignored"). If this ever fires, someone un-ignored the path — STOP, don't sync.
  if ! git check-ignore -q "src/grant_lab/web/spa/"; then
    echo "[run] src/grant_lab/web/spa/ is NOT gitignored — refusing to sync the" \
         "built bundle there (would risk committing it). Aborting."
    exit 1
  fi
  echo "[run] E2E_SPA_MODE=prod — building the SPA (pnpm build)…"
  ( cd frontend && pnpm build )
  # Was `rsync -a --delete frontend/dist/ src/grant_lab/web/spa/`. That wrote
  # the served tree with no bundle gate and no edition.txt — and --delete removed
  # the marker a previous `pnpm package:match` had left, so an e2e run silently
  # downgraded a match deployment to full with nothing to detect it afterwards.
  # package_spa.sh does the same copy, asks dist/module-manifest.json which
  # edition it actually is, and records the answer.
  echo "[run] packaging frontend/dist/ -> src/grant_lab/web/spa/ (gate + edition marker)…"
  bash "$ROOT/scripts/package_spa.sh"
fi

echo "[run] starting seed (testcontainers PG + uvicorn:8000, SPA_MODE=$E2E_SPA_MODE)…"
# `uv run python -u` → unbuffered stdout so the printed E2E_* vars reach $SEED_LOG
# immediately, and no pre-activated venv required.
env EMBEDDING_MODEL=bge-m3 E2E_SPA_MODE="$E2E_SPA_MODE" \
  uv run python -u scripts/collab_e2e_seed.py >"$SEED_LOG" 2>&1 &
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

if [ "$E2E_SPA_MODE" = "dev" ]; then
  echo "[run] starting Vite dev (:5173)…"
  # --strictPort: fail fast rather than silently moving to :5174 (which the readiness
  # probe below would miss, validating a stale :5173). Preflight already frees the port;
  # this guards the launch race without changing the committed vite.config.ts default.
  ( cd frontend && exec pnpm exec vite --strictPort ) >"$VITE_LOG" 2>&1 &
  VITE_PGID=$!
fi

echo "[run] waiting for uvicorn:8000$([ "$E2E_SPA_MODE" = "dev" ] && echo ' + vite:5173')…"
# curl WITHOUT -f on purpose: a 401 from /api/me (unauthenticated) still proves the
# port is up and serving. -f would treat that 401 as failure and never go ready.
ready=0
for _ in $(seq 1 120); do
  if curl -s -o /dev/null "http://127.0.0.1:8000/api/me" \
     && { [ "$E2E_SPA_MODE" = "prod" ] || curl -s -o /dev/null "http://localhost:5173/"; }; then
    ready=1
    break
  fi
  sleep 0.5
done
if [ "$ready" -ne 1 ]; then
  echo "[run] servers not ready after ~60s — aborting."
  echo "--- seed log (tail) ---"; tail -30 "$SEED_LOG"
  [ "$E2E_SPA_MODE" = "dev" ] && { echo "--- vite log (tail) ---"; tail -30 "$VITE_LOG"; }
  exit 1
fi

echo "[run] running collab e2e (E2E_SPA_MODE=$E2E_SPA_MODE)…"
cd frontend
# E2E_MODE drives playwright.config.ts's baseURL switch (shared with the Plan-3
# dashboard e2e): "prod" -> http://localhost:8000 (no proxy), else :5173.
# Which specs to run. Default is the collab round-trip (what this script was
# written for); E2E_SPECS lets CI — or a local run — ask for the dashboard spec
# too, now that the seeder creates its password user and a call (review
# 2026-09-02 A6 finding 6: dashboard.spec.ts was unrunnable and wired nowhere).
E2E_SPECS="${E2E_SPECS:-e2e/collab.spec.ts}"
# shellcheck disable=SC2086  # deliberate word-splitting: E2E_SPECS is a spec list
E2E_MODE="$E2E_SPA_MODE" pnpm exec playwright test $E2E_SPECS "$@"
