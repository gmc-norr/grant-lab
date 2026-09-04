"""Every Call Match gate must be INVOKED by CI, not merely exist.

The branch this came from shipped gate 1 (the match-bundle module check) as a
`pnpm` script that no workflow ran: it fired only when a human remembered, which
is not a gate. Two more have the same shape — the router-alias test can only ever
assert ONE direction per vitest process, so the match direction needs its own
explicitly-match invocation; and the browser smoke needs its own job.

These assertions are deliberately about the WIRING. What each gate proves is
pinned elsewhere (frontend/src/checkMatchBundle.test.ts, virtual-router.test.ts,
e2e/match.spec.ts, tests/architecture/test_package_spa.py). What nothing else
pins is that CI still calls them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CI = REPO / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_is_readable():
    """A scan of an empty string passes every substring check below."""
    assert CI.is_file()
    assert len(CI.read_text()) > 1000


def test_gate_1_the_bundle_check_runs_in_ci():
    # Chained with && on purpose: a build that fails at tsc writes no manifest and
    # a separately-invoked check reads the previous clean one and exits 0.
    assert "pnpm run build:match && pnpm run check:bundle" in CI.read_text()


def test_the_match_direction_of_the_router_alias_runs_in_ci():
    # The alias is resolved once, at vite config time, from VITE_APP_EDITION, so
    # one vitest process can cover only one direction. Without this second
    # invocation the only test of the branch's load-bearing mechanism asserted the
    # FULL direction and nothing else.
    assert "VITE_APP_EDITION=match pnpm run test src/virtual-router.test.ts" in CI.read_text()


def test_the_match_browser_smoke_runs_in_ci():
    ci = CI.read_text()
    assert "pnpm run e2e:match" in ci
    assert "match-e2e:" in ci


def test_the_e2e_match_script_drives_the_match_spec():
    scripts = json.loads((REPO / "frontend" / "package.json").read_text())["scripts"]
    cmd = scripts["e2e:match"]
    assert "E2E_MODE=match" in cmd, cmd
    assert "e2e/match.spec.ts" in cmd, cmd


def test_the_match_spec_is_not_hollow():
    """`pnpm run e2e:match` on an empty spec file exits 0. Pin that it still asks
    something: the count, and the two release-1 CRITICALs it exists to catch."""
    spec = (REPO / "frontend" / "e2e" / "match.spec.ts").read_text()
    assert len(re.findall(r"^test\(", spec, re.M)) >= 6
    assert "/api/auth/logout" in spec  # sign-out really ends the session
    assert "/cockpit" in spec  # no link into the excluded half
