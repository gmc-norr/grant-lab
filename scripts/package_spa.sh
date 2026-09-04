#!/usr/bin/env bash
# Copy a built SPA into the directory FastAPI serves under SPA_MODE=prod.
#
# SPA_DIR is src/call_manager/web/spa (web/spa_serving.py:12). `vite build`
# writes to frontend/dist and NOTHING copies it across — so a smoke test run
# straight after a build serves the PREVIOUS bundle. That is how a match
# deployment ships a full-edition cockpit with every gate green.
#
# THE EDITION IS READ FROM THE BUNDLE, NOT FROM THE INVOCATION. The first
# version of this script keyed the gate off ${VITE_APP_EDITION:-full}, which had
# two reachable silent failures: `./scripts/package_spa.sh` after a full build —
# one keystroke from the documented `pnpm package:match` — skipped the gate
# entirely and packaged a cockpit into the served tree at exit 0; and a
# legitimately .env-configured match build (vite.config.ts reads loadEnv, not
# process.env) has no shell variable set, so it too skipped the gate and printed
# `edition=full` over a match bundle. dist/module-manifest.json is the repo's
# trusted edition oracle and it was sitting right here unread.
#
# NOTE there are TWO OTHER writers of the served directory, and neither calls
# this script or runs any bundle gate:
#   1. .github/workflows/ci.yml:146-147 — `rm -rf src/call_manager/web/spa` +
#      `docker cp` for the wheel, and frontend/Dockerfile builds the FULL
#      edition. (The earlier ":118" here was stale; 118 is the OSV-Scanner step.)
#   2. scripts/collab_e2e_run.sh:110-113 — under E2E_SPA_MODE=prod it runs
#      `pnpm build` (FULL: no VITE_APP_EDITION) and then
#      `rsync -a --delete frontend/dist/ src/call_manager/web/spa/`. So ANY local
#      SPA_MODE=prod e2e run leaves a full build in the served tree even if
#      `pnpm package:match` ran first — and it writes no edition.txt, so the
#      marker from the previous packaging is deleted with the rest of the tree.
# This script governs the local packaging path only. Keep all three in mind
# together — and note that web/spa_serving.py:mount_spa's edition check does NOT
# rescue either of them: it refuses to start only on a marker that is PRESENT and
# DISAGREEING, and neither writer leaves one (writer 1 never creates it; writer
# 2's `rsync --delete` removes the one this script left, since edition.txt is not
# in frontend/dist). Absent means unknown, which is what keeps the CI/Docker
# deployment bootable. So after either of them, re-run `pnpm package:match`
# before serving a match deployment; nothing will tell you if you do not.
#
# Pinned by tests/architecture/test_package_spa.py.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/frontend/dist"
SPA="$ROOT/src/call_manager/web/spa"

# ROOT is derived from BASH_SOURCE, so a symlinked scripts/ dir resolves it to
# the wrong tree. Cheap positive identity check before anything destructive.
[ -f "$ROOT/scripts/check_match_bundle.mjs" ] ||
  { echo "FATAL: $ROOT is not the repo root (no scripts/check_match_bundle.mjs)" >&2; exit 1; }
[ -f "$DIST/index.html" ] || { echo "FATAL: no build at $DIST — run a build first" >&2; exit 1; }

# Ask the ARTEFACT which edition it is. Clean under the excluded-path list means
# a match bundle; anything else is a full one.
if node "$ROOT/scripts/check_match_bundle.mjs" "$DIST" >/dev/null 2>&1; then
  EDITION=match
else
  EDITION=full
fi

# A caller that ASKED for match and got a full bundle is the failure this script
# exists to stop. Print the gate's real output so the offending modules are named.
if [ "${VITE_APP_EDITION:-}" = "match" ] && [ "$EDITION" != "match" ]; then
  node "$ROOT/scripts/check_match_bundle.mjs" "$DIST" >&2 || true
  echo "FATAL: refusing to package a match build that fails the bundle gate" >&2
  exit 1
fi

# Everything above this line is validation. Nothing destructive may move above
# it: a script that deletes before it validates destroys the served tree on the
# way to failing. tests/architecture/test_package_spa.py mutates exactly that.
rm -rf "$SPA"
mkdir -p "$SPA"
cp -R "$DIST"/. "$SPA"/
# A build artefact, not an asset (Task 6, Step 7). Only /assets is mounted
# static (web/spa_serving.py), so it was never browser-reachable anyway — but
# removing it leaves the served tree auditable by chunk-name grep alone, the
# technique check_match_bundle.mjs's header documents as defeated. edition.txt
# below is the replacement: small, and it records what the manifest proved.
rm -f "$SPA/module-manifest.json"
printf 'edition=%s\npackaged=%s\n' "$EDITION" "$(date -Is)" > "$SPA/edition.txt"

echo "packaged $(du -sb "$SPA" | cut -f1) bytes into src/call_manager/web/spa (edition=$EDITION, from the bundle)"
