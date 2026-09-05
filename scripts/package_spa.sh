#!/usr/bin/env bash
# Copy a built SPA into the directory FastAPI serves under SPA_MODE=prod.
#
# SPA_DIR is src/grant_lab/web/spa (web/spa_serving.py:12). `vite build`
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
# THIS IS NOW THE ONLY WRITER OF THE SERVED DIRECTORY, and that is load-bearing.
# There used to be two others, and neither ran a gate:
#   1. .github/workflows/ci.yml — `rm -rf src/grant_lab/web/spa` + `docker cp`
#      of the container build, straight in. frontend/Dockerfile built the FULL
#      edition unconditionally, so the WHEEL always shipped a full SPA, un-gated
#      and with no edition.txt.
#   2. scripts/collab_e2e_run.sh — under E2E_SPA_MODE=prod, `pnpm build` (full)
#      then `rsync -a --delete frontend/dist/ src/grant_lab/web/spa/`. The
#      --delete removed the edition.txt a previous `pnpm package:match` had left,
#      so a local e2e run downgraded a match deployment to full and erased the
#      evidence.
# Both now call this script instead (ci.yml extracts to frontend/dist first, and
# frontend/Dockerfile takes VITE_APP_EDITION as a build ARG). That matters beyond
# tidiness: web/spa_serving.py's boot-time mismatch check refuses only on a marker
# that is PRESENT and DISAGREEING — absent means unknown, which is what kept those
# two paths bootable while they were wrong. With every writer coming through here,
# a served tree always carries a marker the gate produced, so the check governs the
# deployed artefact instead of only the local one.
#
# tests/architecture/test_package_spa.py::test_no_other_writer_of_the_served_tree
# scans scripts/ and .github/workflows/ for a write to that path outside this file
# and fails if one reappears — because "we fixed the two we knew about" is not a
# property, and a third writer is exactly how this regresses.
#
# Pinned by tests/architecture/test_package_spa.py.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/frontend/dist"
SPA="$ROOT/src/grant_lab/web/spa"

# ROOT is derived from BASH_SOURCE, so a symlinked scripts/ dir resolves it to
# the wrong tree. Cheap positive identity check before anything destructive.
[ -f "$ROOT/scripts/check_match_bundle.mjs" ] ||
  { echo "FATAL: $ROOT is not the repo root (no scripts/check_match_bundle.mjs)" >&2; exit 1; }
[ -f "$DIST/index.html" ] || { echo "FATAL: no build at $DIST — run a build first" >&2; exit 1; }

# Ask the ARTEFACT which edition it is, and READ THE EXIT CODE, not just its sign.
# check_match_bundle.mjs returns 0 clean / 1 leaked / 2 could-not-judge, and the
# difference matters: this script used to map every non-zero to `full`, so a MATCH
# build whose manifest failed to write was labelled `edition=full`, packaged at
# exit 0, and stamped into the served tree's edition.txt. The artefact then lied
# about itself, which is the one thing edition.txt exists to prevent. "The gate
# could not run" is not evidence of anything and must stop the packaging.
set +e
node "$ROOT/scripts/check_match_bundle.mjs" "$DIST" >/dev/null 2>&1
GATE_RC=$?
set -e
case "$GATE_RC" in
  0) EDITION=match ;;
  1) EDITION=full ;;
  *)
    node "$ROOT/scripts/check_match_bundle.mjs" "$DIST" >&2 || true
    echo "FATAL: the bundle gate could not judge $DIST (exit $GATE_RC) — refusing to" \
         "package an artefact whose edition is unknown. Rebuild, then retry." >&2
    exit 1
    ;;
esac

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

echo "packaged $(du -sb "$SPA" | cut -f1) bytes into src/grant_lab/web/spa (edition=$EDITION, from the bundle)"
