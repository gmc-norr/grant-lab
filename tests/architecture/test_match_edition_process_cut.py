"""The match edition must not LOAD the half it does not serve.

`APP_EDITION=match` drops the routes (126 -> 44) but, before this gate, changed
nothing else: the same 239 `grant_lab` modules, the same 142 proposal-assist
modules, and `pycrdt`, `prosemirror` and `claude_agent_sdk` all imported at boot.
A match-only deployment therefore still loaded the drafting and collaborative
editing stack it does not sell.

**This must run in a fresh interpreter.** pytest has already imported the whole
application by the time any test executes, so an in-process `sys.modules` check
would pass while proving nothing — the vacuity this file exists to avoid. Each
case boots a subprocess with a hermetic environment (`.env` disabled exactly as
`conftest._isolate_settings_from_dotenv` does) and reports what it loaded.

The full-edition case is not decoration: without it, deleting the imports
outright would also make the match case pass. The pair pins a DIFFERENCE.

Result of the 2026-09-05 cut: **0** proposal-assist modules in a match boot,
`grant_lab` modules 239 -> 83, and pycrdt/prosemirror/claude_agent_sdk absent.

Getting the last 52 to zero took two moves that deferring could not: the shared
`is_lock_timeout` and `BUSY_ERROR_MESSAGE` — used by the GENERIC OperationalError
handler both editions register — moved to `grant_lab/db/errors.py`, and
`citation_service` is now built only in the full edition (its only consumers are
_FULL_ONLY routers and analysis ticks the scheduler already gates on
`drafting_enabled`, which match forbids).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

_PROBE = """
import json, sys
from grant_lab.config import Settings
Settings.model_config["env_file"] = None
from grant_lab.main import create_app
app = create_app()
print("__PROBE__" + json.dumps({
    "proposal_assist": len([m for m in sys.modules if "proposal_assist" in m]),
    "grant_lab": len([m for m in sys.modules if m.startswith("grant_lab")]),
    "pycrdt": "pycrdt" in sys.modules,
    "claude_agent_sdk": "claude_agent_sdk" in sys.modules,
    "prosemirror": "prosemirror" in sys.modules,
    "routes": len(app.routes),
}))
"""

_BASE_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "HOME": os.environ.get("HOME", ""),
    "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
    "SPA_MODE": "disabled",
    "SESSION_SECRET": "0123456789abcdef0123456789abcdef0123456789abcdef",
    "ALLOWED_EMAIL_DOMAINS": "example.com",
    "APP_BASE_URL": "https://example.com",
}


def _boot(edition: str) -> dict:
    env = dict(_BASE_ENV, APP_EDITION=edition)
    if edition == "match":
        env["DRAFTING_ENABLED"] = "false"
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("__PROBE__"):
            return json.loads(line[len("__PROBE__") :])
    raise AssertionError(
        f"probe never reported for edition={edition}.\n"
        f"exit={proc.returncode}\nstdout tail:\n{proc.stdout[-2000:]}\n"
        f"stderr tail:\n{proc.stderr[-2000:]}"
    )


def test_a_match_boot_loads_none_of_the_heavy_proposal_assist_dependencies():
    """The three libraries only the full edition needs must not be imported at all."""
    got = _boot("match")
    for dep in ("pycrdt", "prosemirror", "claude_agent_sdk"):
        assert got[dep] is False, (
            f"a match boot imported {dep}, which only the full edition needs. probe: {got}"
        )


def test_a_match_boot_imports_no_proposal_assist_module_at_all():
    """Zero, not a budget. Any module-level import of proposal_assist from the
    always-loaded path reddens this, and the fix is to defer it to the caller or
    move the shared thing out of the package (grant_lab/db/errors.py is where
    is_lock_timeout and BUSY_ERROR_MESSAGE went for exactly this reason)."""
    got = _boot("match")
    assert got["proposal_assist"] == 0, (
        f"a match boot imported {got['proposal_assist']} proposal_assist modules. probe: {got}"
    )


def test_the_full_edition_still_loads_all_of_it_so_the_match_case_is_a_difference():
    got = _boot("full")
    assert got["proposal_assist"] > 100, got
    for dep in ("pycrdt", "prosemirror", "claude_agent_sdk"):
        assert got[dep] is True, (
            f"the FULL edition no longer imports {dep} — the feature is gone, not deferred"
        )


def test_the_two_editions_serve_different_route_counts():
    assert _boot("match")["routes"] < _boot("full")["routes"]


# ---------------------------------------------------------------------------
# The same question, asked at the moment that actually matters.
#
# Everything above measures `create_app()`. That is a real surface — routers,
# dependencies and module-level imports all land there — but it is NOT a running
# process. FastAPI's lifespan had SEVEN unguarded proposal-assist imports
# (main.py, the drafting factory / collab room / lifecycle / passes / retirement
# / jobs_retirement / references.embed block), so a match deployment measured
# 0 modules here and loaded 41 of them, plus pycrdt, the moment it was served.
#
# The 2026-09-05 architecture review found it. The gate above was green
# throughout: it asked the right question in a fresh interpreter at the wrong
# MOMENT. Hollow-gate shape: correct isolation, wrong lifecycle point.
#
# These cases boot through the real lifespan against the real test Postgres —
# no stubbing, no fakes — so what they count is what a deployment would load.
# ---------------------------------------------------------------------------

_LIFESPAN_PROBE = """
import asyncio, json, sys
from grant_lab.config import Settings
Settings.model_config["env_file"] = None
from grant_lab.main import create_app, lifespan

async def main():
    app = create_app()
    at_construction = len([m for m in sys.modules if "proposal_assist" in m])
    async with lifespan(app):
        print("__PROBE__" + json.dumps({
            "at_construction": at_construction,
            "proposal_assist": len([m for m in sys.modules if "proposal_assist" in m]),
            "grant_lab": len([m for m in sys.modules if m.startswith("grant_lab")]),
            "pycrdt": "pycrdt" in sys.modules,
            "claude_agent_sdk": "claude_agent_sdk" in sys.modules,
            "prosemirror": "prosemirror" in sys.modules,
            "scheduler_jobs": sorted(j.id for j in app.state.scheduler.get_jobs()),
        }))

asyncio.run(main())
"""


def _boot_through_lifespan(edition: str, db_url: str, workspaces_dir: str) -> dict:
    env = dict(_BASE_ENV, APP_EDITION=edition)
    env["DATABASE_URL"] = db_url
    env["WORKSPACES_DIR"] = workspaces_dir
    if edition == "match":
        env["DRAFTING_ENABLED"] = "false"
    proc = subprocess.run(
        [sys.executable, "-c", _LIFESPAN_PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("__PROBE__"):
            return json.loads(line[len("__PROBE__") :])
    raise AssertionError(
        f"lifespan probe never reported for edition={edition}.\n"
        f"exit={proc.returncode}\nstdout tail:\n{proc.stdout[-3000:]}\n"
        f"stderr tail:\n{proc.stderr[-3000:]}"
    )


def test_a_match_boot_imports_no_proposal_assist_module_THROUGH_THE_LIFESPAN(
    migrated_engine, pg_url, tmp_path
):
    """Zero after startup, not just after construction.

    What trivially passes this: measuring before the lifespan runs — which is
    exactly what the create_app() cases above do, and exactly how this leak
    survived. If you ever need to weaken this, the honest move is to delete it,
    not to move the measurement earlier.
    """
    got = _boot_through_lifespan("match", pg_url, str(tmp_path / "ws"))
    assert got["at_construction"] == 0, got
    assert got["proposal_assist"] == 0, (
        f"a match boot loaded {got['proposal_assist']} proposal_assist modules once the "
        f"lifespan ran, having loaded {got['at_construction']} at construction. probe: {got}"
    )
    for dep in ("pycrdt", "prosemirror", "claude_agent_sdk"):
        assert got[dep] is False, f"a match boot imported {dep} during startup. probe: {got}"


def test_the_full_edition_still_loads_it_through_the_lifespan_so_this_pins_a_difference(
    migrated_engine, pg_url, tmp_path
):
    """Without this the whole block would pass if the feature were simply deleted."""
    got = _boot_through_lifespan("full", pg_url, str(tmp_path / "ws"))
    assert got["proposal_assist"] > 100, got
    assert got["pycrdt"] is True, got


def test_the_match_scheduler_registers_no_drafting_jobs(migrated_engine, pg_url, tmp_path):
    """`drafting_idle_sweep` was registered in BOTH editions and fired on every
    interval against the drafting registry a match deployment does not serve. It
    was the only one of the eight proposal ticks with no drafting_enabled guard."""
    match = _boot_through_lifespan("match", pg_url, str(tmp_path / "m"))
    full = _boot_through_lifespan("full", pg_url, str(tmp_path / "f"))
    assert "drafting_idle_sweep" not in match["scheduler_jobs"], match["scheduler_jobs"]
    assert "drafting_idle_sweep" in full["scheduler_jobs"], full["scheduler_jobs"]
