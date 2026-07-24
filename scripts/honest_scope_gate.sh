#!/usr/bin/env bash
# Slice B-mutex honest-scope gate. Reads candidate text on stdin; exits 1 if it
# contains language claiming more than the slice delivers. This slice covers
# MUTEX-PARTICIPATING writers only — settings.py update_settings/add_member are
# an accepted, documented residual, and nothing here proves a subprocess stopped.
#
# Factored into a script (rather than inlined in the plan) so the gate can be
# executed against known-bad input and asserted to fail. A gate that is never
# run against something it should reject is not a verified gate.
set -uo pipefail

OVERCLAIM='all writers|every writer|cannot write after|no writes after|proves.*stopped|fully prevents'

if grep -nEi "$OVERCLAIM"; then
  echo "FAIL: overclaiming language introduced — this slice covers MUTEX-PARTICIPATING writers only" >&2
  exit 1
fi
exit 0
