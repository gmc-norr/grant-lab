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

THIS FILE WAS ITSELF A HOLLOW GATE, twice over, and two outside reviewers found
both holes on the same afternoon it was written:

  * it searched the workflow's raw TEXT, so commenting out a whole step while
    leaving the command in a `#` comment kept every assertion green. Reproduced:
    the entire match-e2e run step was disabled and all six tests passed.
  * it required only six `test(` lines and two substrings anywhere in the spec,
    so six empty test bodies plus those strings in a comment passed. Reproduced.

Both are why the checks below parse the YAML and look only at active `run:`
bodies, and why the spec pin names the tests and counts assertions.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
CI = REPO / ".github" / "workflows" / "ci.yml"
SPEC = REPO / "frontend" / "e2e" / "match.spec.ts"


def _run_lines() -> list[str]:
    """Every non-comment line of every step's `run:` body, across every job.

    Parsing rather than grepping is the whole point. `yaml.safe_load` drops
    YAML-level comments outright, so a commented-out step simply is not here; and
    stripping `#` lines inside a `run: |` block removes the other hiding place.
    """
    workflow = yaml.safe_load(CI.read_text())
    out: list[str] = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            body = step.get("run")
            if not body:
                continue
            for line in str(body).splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    out.append(stripped)
    return out


def _runs(fragment: str) -> bool:
    return any(fragment in line for line in _run_lines())


def test_the_run_parser_finds_commands():
    """The positive control. A parser that returned [] would fail every check
    below for the wrong reason — or, if the checks were `not in`, pass them all.
    """
    lines = _run_lines()
    assert len(lines) > 20, lines
    assert any("pnpm install --frozen-lockfile" in ln for ln in lines), lines[:10]


def test_the_parser_ignores_commented_out_steps():
    """The control that pins the actual fix: text present, step disabled."""
    disabled = yaml.safe_load(
        "jobs:\n"
        "  j:\n"
        "    steps:\n"
        "      - name: real\n"
        "        run: echo alive\n"
        "      # - name: disabled\n"
        "      #   run: pnpm run the-thing-we-check-for\n"
    )
    lines = [
        s.strip()
        for job in disabled["jobs"].values()
        for step in job.get("steps", [])
        if step.get("run")
        for s in str(step["run"]).splitlines()
        if s.strip() and not s.strip().startswith("#")
    ]
    assert lines == ["echo alive"], lines


def test_gate_1_the_bundle_check_runs_in_ci():
    # Chained with && on purpose: a build that fails at tsc writes no manifest and
    # a separately-invoked check reads the previous clean one and exits 0.
    assert _runs("pnpm run build:match && pnpm run check:bundle")


def test_the_match_direction_of_the_router_alias_runs_in_ci():
    # The alias is resolved once, at vite config time, from VITE_APP_EDITION, so
    # one vitest process can cover only one direction. Without this second
    # invocation the only test of the branch's load-bearing mechanism asserted the
    # FULL direction and nothing else.
    assert _runs("VITE_APP_EDITION=match pnpm run test src/virtual-router.test.ts")


def test_the_match_browser_smoke_runs_in_ci():
    assert _runs("pnpm run e2e:match")
    assert "match-e2e" in yaml.safe_load(CI.read_text())["jobs"]


def test_the_wheel_build_packages_through_the_script():
    """'Nothing else writes the served tree' is also satisfied by a CI that
    stopped writing it at all — which would ship a wheel with no SPA."""
    assert _runs("bash scripts/package_spa.sh")


def test_the_e2e_match_script_drives_the_match_spec():
    scripts = json.loads((REPO / "frontend" / "package.json").read_text())["scripts"]
    cmd = scripts["e2e:match"]
    assert "E2E_MODE=match" in cmd, cmd
    assert "e2e/match.spec.ts" in cmd, cmd


# The browser smoke's scenarios, by exact title. Naming them — rather than
# counting `test(` lines — is what stops six empty bodies from passing: a deleted
# scenario is now a named absence, and the titles say what release-1 property each
# one holds. Keep in step with frontend/e2e/match.spec.ts.
REQUIRED_SCENARIOS = [
    "the release-1 shell renders, and the dashboard lists calls",
    "clicking a call row opens that call's own page",
    "signing out reaches the logout endpoint and leaves for /login",
    "no rendered link anywhere in release 1 points into the excluded half",
    "every mounted release-1 route renders exactly one main landmark",
    "the admin subnav offers only the three sections release 1 mounts",
    "an excluded route renders the in-app soft 404, not a cockpit",
    "the light ground is actually PAINTED, not merely declared",
    "no critical a11y violations on the release-1 landing page",
    "the account menu offers Settings as well as Sign out",
    "the owner's sharing card names who a private profile is shared with",
    "moving the sharing control sends the position it was moved TO, both ways",
    "adding a viewer by email POSTs that address and lists the person back",
    "a non-viewer's profile list has no trace of the private profile",
]


def _spec_source_lines() -> list[str]:
    """The spec with `//` comment lines removed — the strings this file looks for
    must be in live code, not in prose describing it."""
    return [ln for ln in SPEC.read_text().splitlines() if not ln.strip().startswith("//")]


def test_the_match_spec_declares_every_required_scenario():
    src = "\n".join(_spec_source_lines())
    titles = re.findall(r'^test\("((?:[^"\\]|\\.)*)"', src, re.M)
    missing = [s for s in REQUIRED_SCENARIOS if s not in titles]
    assert missing == [], f"the browser smoke no longer covers: {missing}"
    assert len(titles) == len(REQUIRED_SCENARIOS), (
        f"the spec has {len(titles)} tests, this list names {len(REQUIRED_SCENARIOS)} — "
        "a new scenario must be named here so deleting it is visible"
    )


def test_the_match_spec_actually_asserts_things():
    """`pnpm run e2e:match` over fourteen empty test bodies exits 0. This is the
    count that stops that.

    **34 `expect(` calls in live code, MEASURED 2026-09-04** by running this test's
    own regex over the comment-stripped spec, and written down as what the command
    printed — not remembered. (An earlier draft of this line said 40 from memory
    and failed on its first run.)

    EQUALITY, not a floor. The previous version asserted `>= 18` against a real 22,
    and four spare assertions are four assertions anyone can delete without a gate
    noticing — which is the whole failure mode this file exists to catch, one level
    up. The cost of equality is that a legitimate edit changes this number; that is
    the point. Change it IN THE SAME DIFF as the spec, and only after re-running the
    measurement."""
    src = "\n".join(_spec_source_lines())
    expects = len(re.findall(r"\bexpect\(", src))
    assert expects == 34, (
        f"the browser smoke has {expects} expect() calls, this gate pins 34 — "
        "if the change was deliberate, re-measure and update the number here"
    )
    # The two release-1 CRITICALs the spec exists to catch, in live code.
    assert "/api/auth/logout" in src  # sign-out really ends the session
    assert "/cockpit" in src  # no link into the excluded half
