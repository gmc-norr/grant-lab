#!/usr/bin/env bash
# Copy a built SPA into the directory FastAPI serves under SPA_MODE=prod.
#
# SPA_DIR is src/call_manager/web/spa (web/spa_serving.py:12). `vite build`
# writes to frontend/dist and NOTHING copies it across — so a smoke test run
# straight after a build serves the PREVIOUS bundle. That is how a match
# deployment ships a full-edition cockpit with every gate green.
#
# Refuses to package a build that has not passed the bundle gate, so the copy
# cannot become the step where the guarantee is lost.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/frontend/dist"
SPA="$ROOT/src/call_manager/web/spa"

[ -f "$DIST/index.html" ] || { echo "FATAL: no build at $DIST — run a build first" >&2; exit 1; }

# Match builds must be gate-clean before they are allowed near the served tree.
if [ "${VITE_APP_EDITION:-full}" = "match" ]; then
  node "$ROOT/scripts/check_match_bundle.mjs" "$DIST" || {
    echo "FATAL: refusing to package a match build that fails the bundle gate" >&2
    exit 1
  }
fi

rm -rf "$SPA"
mkdir -p "$SPA"
cp -R "$DIST"/. "$SPA"/
# A build artefact, not an asset — it lists source paths and serves no purpose
# to a browser (Task 6, Step 7).
rm -f "$SPA/module-manifest.json"

echo "packaged $(du -sh "$SPA" | cut -f1) into src/call_manager/web/spa (edition=${VITE_APP_EDITION:-full})"
