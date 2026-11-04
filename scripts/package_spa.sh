#!/usr/bin/env bash
# Copy a built SPA into the directory FastAPI serves under SPA_MODE=prod.
#
# SPA_DIR is src/grant_lab/web/spa (web/spa_serving.py). `vite build` writes to
# frontend/dist and NOTHING copies it across, so a smoke test run straight after a
# build serves the PREVIOUS bundle unless this script runs.
#
# THIS IS THE ONLY WRITER OF THE SERVED DIRECTORY, and that is load-bearing:
# ci.yml (the wheel) and scripts/e2e_run.sh (prod-mode e2e) both call it instead of
# copying on their own. tests/architecture/test_package_spa.py::
# test_no_other_writer_of_the_served_tree scans scripts/ and .github/workflows/
# for a write to that path outside this file and fails if one reappears.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/frontend/dist"
SPA="$ROOT/src/grant_lab/web/spa"

# ROOT is derived from BASH_SOURCE, so a symlinked scripts/ dir resolves it to the
# wrong tree. Cheap positive identity check before anything destructive.
[ -d "$ROOT/src/grant_lab/web" ] ||
  { echo "FATAL: $ROOT is not the repo root (no src/grant_lab/web)" >&2; exit 1; }
[ -f "$DIST/index.html" ] || { echo "FATAL: no build at $DIST — run a build first" >&2; exit 1; }

# Everything above this line is validation. Nothing destructive may move above it:
# a script that deletes before it validates destroys the served tree on the way to
# failing. tests/architecture/test_package_spa.py mutates exactly that.
rm -rf "$SPA"
mkdir -p "$SPA"
cp -R "$DIST"/. "$SPA"/

echo "packaged $(du -sb "$SPA" | cut -f1) bytes into src/grant_lab/web/spa"
