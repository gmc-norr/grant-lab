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
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
CI = REPO / ".github" / "workflows" / "ci.yml"
SPEC = REPO / "frontend" / "e2e" / "match.spec.ts"


def _enforces(node: dict) -> bool:
    """Whether a job or step both RUNS unconditionally and BLOCKS on failure.

    A GitHub `if:` is an expression, and proving one is truthy is not something
    this file can do. So any `if:` at all disqualifies a node from carrying a
    gate — conservative, and the trace-upload step's legitimate `if: failure()`
    shows why the rule cannot simply be "no conditions anywhere in the file":
    it is scoped to the nodes whose commands we depend on.

    `continue-on-error` disqualifies unless it is literally false. A truthy
    string like "true" must not slip through a bare truthiness test.
    """
    if "if" in node:
        return False
    return not ("continue-on-error" in node and node["continue-on-error"] is not False)


def _run_lines() -> list[str]:
    """Every non-comment line of every ENFORCING step's `run:` body.

    Parsing rather than grepping is the whole point. `yaml.safe_load` drops
    YAML-level comments outright, so a commented-out step simply is not here; and
    stripping `#` lines inside a `run: |` block removes the other hiding place.

    THE THIRD HOLE, found by an outside review on 2026-09-05: parsing closed the
    comment loophole but still read command TEXT, so `if: false` on the match-e2e
    job, `if: false` on its smoke step, and `continue-on-error: true` on the job
    each passed every check in this file. Measured — twelve checks, zero
    rejections, against a removed-command control that was correctly rejected.
    Filtering HERE rather than adding parallel assertions means every existing
    check inherits the fix: a command that cannot fail the build is not present.
    """
    workflow = yaml.safe_load(CI.read_text())
    out: list[str] = []
    for job in workflow["jobs"].values():
        if not _enforces(job):
            continue
        for step in job.get("steps", []):
            body = step.get("run")
            if not body or not _enforces(step):
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


# The browser smoke's scenarios, by exact title.
# Naming them — rather than counting `test(` lines — is what stops empty bodies
# from passing: a deleted scenario is now a named absence, and the titles say what
# release-1 property each one holds. Keep in step with frontend/e2e/match.spec.ts.
#
# Covers edition shape, matching workflows, mobile layouts, and board display.
REQUIRED_SCENARIOS = [
    "the release-1 shell renders, and the dashboard lists calls",
    "clicking a call row opens that call's own page",
    "signing out reaches the logout endpoint and leaves for /login",
    "no rendered link anywhere in release 1 points into the excluded half",
    "every mounted release-1 route renders exactly one main landmark",
    # Renamed 2026-09-07 (three -> four): router.simple.tsx mounts
    # /admin/golden-set in the match edition now. A RENAME, not an addition, so
    # the len(titles) == len(REQUIRED_SCENARIOS) equality below still balances.
    "the admin subnav offers only the four sections release 1 mounts",
    "an excluded route renders the in-app soft 404, not a cockpit",
    "the light ground is actually PAINTED, not merely declared",
    "no critical a11y violations on the release-1 landing page",
    "with no profile the landing page is an invitation, not the archive",
    "the account menu offers Settings as well as Sign out",
    "the owner's sharing card names who a private profile is shared with",
    "moving the sharing control sends the position it was moved TO, both ways",
    "adding a viewer by email POSTs that address and lists the person back",
    "a non-viewer's profile list has no trace of the private profile",
    "profile setup is compact and mobile profile actions fit",
    "detail decisions, scoped sharing and deadline export are usable",
    "a failed decision stays visible with an error",
    "search and deadline order survive the detail round trip",
    "the triage feed renders the rail, its counts and the focused call's card",
    "pressing s saves the focused call, and Saved is where it lands",
    "pressing h hides the focused call, and Hidden keeps it rather than dropping it",
    "pressing u in Hidden returns the call to the inbox, and the rail count moves with it",
    "the detail round trip lands back in the view it left",
    "the rail's profile picker switches profile, and the hidden set changes with it",
    "deciding the last call in the inbox reaches the inbox-zero screen",
    "a criterion chip expands to the funder's own sentence and the model's reasoning",
    "a saved call whose deadline has passed is still in Saved, marked closed",
    "with profiles but none picked the landing page offers the picker, not the feed",
    # The corridor wall at its authored size. An outside review measured the
    # page growing to 2292px at 1920x1080 with realistic funding titles, which
    # puts the whole second row below a screen nobody can scroll.
    "the corridor board fits 1920x1080 with realistic long titles",
    "the portrait board keeps long titles readable beside full-size QR codes",
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


# `expect.soft(` and `expect.poll(` are INVISIBLE to a bare `\bexpect\(` (T9-6):
# either one can replace a real assertion without moving the count, and
# `expect.soft` does not even fail the run at the point it fires. Both spellings
# are counted here, so the pin sees every assertion site there is.
EXPECT_SITE = re.compile(r"\bexpect(\.soft|\.poll)?\(")


def test_the_match_spec_actually_asserts_things():
    """`pnpm run e2e:match` over empty test bodies exits 0. This is the
    count that stops that.

    **148 assertion sites in live code, MEASURED 2026-09-08** by running this
    test's own regex over the comment-stripped spec, and written down as what the
    command printed — not remembered. (An earlier draft of this line said 40 from
    memory and failed on its first run.) It was 34 until Task 8 added the
    no-profile scenario, 38 until Task 9 added the ten triage-feed scenarios.
    `expect.soft(` and `expect.poll(` are both 0 today; they are in the regex so
    that swapping one in for a real `expect(` cannot pass unnoticed.

    EQUALITY, not a floor. The previous version asserted `>= 18` against a real 22,
    and four spare assertions are four assertions anyone can delete without a gate
    noticing — which is the whole failure mode this file exists to catch, one level
    up. The cost of equality is that a legitimate edit changes this number; that is
    the point. Change it IN THE SAME DIFF as the spec, and only after re-running the
    measurement.

    What this count CANNOT see is `expect(true).toBe(true)` in place of a real
    assertion — the site is still a site. That hole is closed one level down, by a
    behavioural mutation per scenario in the app source it drives (Task 9 step 3)."""
    src = "\n".join(_spec_source_lines())
    expects = len(EXPECT_SITE.findall(src))
    assert expects == 148, (
        f"the browser smoke has {expects} expect() calls, this gate pins 148 — "
        "if the change was deliberate, re-measure and update the number here"
    )
    # The two release-1 CRITICALs the spec exists to catch, in live code.
    assert "/api/auth/logout" in src  # sign-out really ends the session
    assert "/cockpit" in src  # no link into the excluded half


# --- pin 3: every async matcher is AWAITED (D17, F-12 #2) -------------------
#
# `frontend/e2e/` is outside tsconfig.json's `include` and is typechecked
# NOWHERE (Phase 2 probed adding it: two playwright-core versions in the
# lockfile disagree about `Page`, which is a dependency problem and not this
# plan's). oxlint has no type information either, so no-floating-promises cannot
# fire. So deleting one `await` in front of a Playwright web-first assertion:
#
#   * keeps the assertion-site count above unchanged,
#   * typechecks nowhere, lints nowhere,
#   * and produces a promise nobody ever settles — an assertion that can never
#     fail, in a file whose entire job is to fail.
#
# This is the text gate that replaces the typechecker for that one property.
# Every Playwright web-first assertion RETRIES, which is why every one of them is
# async; `expect.poll(` is async whatever matcher follows it.
ASYNC_MATCHERS = frozenset(
    {
        "toBeAttached",
        "toBeChecked",
        "toBeDisabled",
        "toBeEditable",
        "toBeEmpty",
        "toBeEnabled",
        "toBeFocused",
        "toBeHidden",
        "toBeInViewport",
        "toBeOK",
        "toBeVisible",
        "toContainText",
        "toHaveAccessibleDescription",
        "toHaveAccessibleName",
        "toHaveAttribute",
        "toHaveCSS",
        "toHaveClass",
        "toHaveCount",
        "toHaveId",
        "toHaveJSProperty",
        "toHaveRole",
        "toHaveScreenshot",
        "toHaveText",
        "toHaveTitle",
        "toHaveURL",
        "toHaveValue",
        "toHaveValues",
        "toPass",
    }
)


def _string_end(src: str, i: int) -> int:
    """Index just past the string literal opening at `src[i]`.

    Needed because `expect(page.getByText("The model's reasoning:"))` has an
    apostrophe inside a double-quoted string and `expect(x.filter((r) => ...))`
    has parentheses inside one — a naive paren count gets both wrong.
    """
    quote = src[i]
    j = i + 1
    while j < len(src):
        if src[j] == "\\":
            j += 2
            continue
        if src[j] == quote:
            return j + 1
        j += 1
    return len(src)


def _matching_paren(src: str, open_idx: int) -> int:
    """Index of the `)` closing the `(` at `open_idx`, or -1."""
    depth = 0
    i = open_idx
    while i < len(src):
        c = src[i]
        if c in "\"'`":
            i = _string_end(src, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


AWAIT_BEFORE = re.compile(r"(?:^|[^A-Za-z0-9_$])await$")
MATCHER_AFTER = re.compile(r"\s*\.\s*(?:not\s*\.\s*)?([A-Za-z]+)")


def unawaited_async_matchers(src: str) -> list[str]:
    """Every `expect(...)` site whose matcher is async and whose `await` is missing.

    Returns human-readable `line N: ...` strings — empty is the pass.
    """
    offenders: list[str] = []
    for m in EXPECT_SITE.finditer(src):
        line = src[: m.start()].count("\n") + 1
        close = _matching_paren(src, m.end() - 1)
        if close == -1:
            # FAIL CLOSED. A parse this scan cannot finish is not a pass: it is a
            # site whose matcher was never read.
            offenders.append(f"line {line}: unparseable expect( — the scan could not find its )")
            continue
        chain = MATCHER_AFTER.match(src[close + 1 :])
        matcher = chain.group(1) if chain else ""
        if m.group(1) != ".poll" and matcher not in ASYNC_MATCHERS:
            continue
        if AWAIT_BEFORE.search(src[: m.start()].rstrip()) is None:
            offenders.append(f"line {line}: expect{m.group(1) or ''}(…).{matcher}")
    return offenders


def test_every_async_matcher_in_the_match_spec_is_awaited():
    src = "\n".join(_spec_source_lines())
    assert unawaited_async_matchers(src) == [], (
        "an un-awaited Playwright matcher is a floating promise that can never "
        "fail; frontend/e2e/ is typechecked nowhere, so this scan is the only gate"
    )


def test_the_await_scan_catches_a_dropped_await():
    """The proof-by-breaking control, on a doctored copy rather than on the file.

    Both halves matter: the good input must be silent (otherwise the gate is red
    on correct code and gets deleted) and the bad input must be named.
    """
    good = (
        "await expect(page.locator('article[data-call-id=\"1\"]')).toBeVisible();\n"
        'await expect(rail).not.toHaveAttribute("aria-current", "page");\n'
        'expect(seen).toContain("POST /api/auth/logout");\n'
        "expect(offenders).toEqual([]);\n"
        'await expect(page.getByText("The model\'s reasoning:")).toBeVisible();\n'
        'expect(mock.requests.filter((r) => r.startsWith("POST "))).toEqual([]);\n'
    )
    assert unawaited_async_matchers(good) == []
    bad = good.replace("await expect(page.locator('article", "expect(page.locator('article")
    assert bad != good
    assert unawaited_async_matchers(bad) == ["line 1: expect(…).toBeVisible"], (
        unawaited_async_matchers(bad)
    )


def test_the_await_scan_does_not_demand_await_of_a_synchronous_matcher():
    """The other direction. A scan that required `await` everywhere would be red on
    `expect(offenders).toEqual([])`, which is correct code — and a gate that is red
    on correct code is a gate someone deletes."""
    assert unawaited_async_matchers("expect(offenders).toEqual([]);") == []
    assert unawaited_async_matchers('expect(seen).toContain("x");') == []
    # ...and `expect.poll(` is async whatever matcher follows it.
    assert unawaited_async_matchers("expect.poll(() => n).toBe(1);") == [
        "line 1: expect.poll(…).toBe"
    ]


# ---------------------------------------------------------------------------
# The third hole's controls.
#
# Every check above reads command text. Until 2026-09-05 that text stayed
# readable when the job or step carrying it could not fail the build, and an
# outside review measured the result: `if: false` on the match-e2e job, `if:
# false` on its smoke step, and `continue-on-error: true` on the job each passed
# all twelve checks, while a removed COMMAND was correctly rejected.
#
# These run the real workflow through the real parser with one field added.
# ---------------------------------------------------------------------------

_SMOKE = "pnpm run e2e:match"


def _workflow_with(mutation: str | None) -> dict:
    """The real ci.yml, optionally with one enforcement field flipped."""
    workflow = yaml.safe_load(CI.read_text())
    job_name, job, step = None, None, None
    for name, candidate in workflow["jobs"].items():
        for candidate_step in candidate.get("steps", []):
            if candidate_step.get("run") and _SMOKE in str(candidate_step["run"]):
                job_name, job, step = name, candidate, candidate_step
    assert step is not None, f"no step runs {_SMOKE!r} — this harness is testing nothing"
    assert job_name == "match-e2e", job_name
    if mutation == "disabled-job":
        job["if"] = False
    elif mutation == "disabled-step":
        step["if"] = False
    elif mutation == "nonblocking-job":
        job["continue-on-error"] = True
    elif mutation == "nonblocking-step":
        step["continue-on-error"] = True
    elif mutation == "remove-command":
        step["run"] = "echo nothing"
    return workflow


def _run_lines_for(tmp_path, monkeypatch, mutation: str | None) -> list[str]:
    path = tmp_path / "ci.yml"
    path.write_text(yaml.safe_dump(_workflow_with(mutation)))
    monkeypatch.setattr(sys.modules[__name__], "CI", path)
    return _run_lines()


def test_the_mutation_harness_preserves_the_command_when_it_changes_nothing(tmp_path, monkeypatch):
    """The positive control. Without it, a harness that mangled the YAML would
    make every rejection below pass for the wrong reason."""
    assert any(_SMOKE in line for line in _run_lines_for(tmp_path, monkeypatch, None))


@pytest.mark.parametrize(
    "mutation",
    ["remove-command", "disabled-job", "disabled-step", "nonblocking-job", "nonblocking-step"],
)
def test_a_smoke_test_that_cannot_fail_the_build_does_not_count_as_wired(
    tmp_path, monkeypatch, mutation
):
    """`remove-command` was already rejected before the fix; the other four were
    not. All five must be now — a gate that cannot fail the build is not a gate.
    """
    lines = _run_lines_for(tmp_path, monkeypatch, mutation)
    assert not any(_SMOKE in line for line in lines), (
        f"{mutation}: the workflow still counts as running {_SMOKE!r}"
    )
