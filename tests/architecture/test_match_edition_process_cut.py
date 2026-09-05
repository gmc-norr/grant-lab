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

Result of the 2026-09-05 cut: 142 -> 52 proposal-assist modules, 239 -> 135
`grant_lab` modules, and pycrdt/prosemirror/claude_agent_sdk all absent. The
remaining 52 are NOT reachable by deferring an import; they need two decisions:
`drafting.locking` exports `is_lock_timeout` and the error messages used by the
GENERIC OperationalError handler that both editions need, and it is undecided
whether a match deployment needs `citation_service` at all (call ingestion uses
it for metadata extraction). Both are package moves, not import moves.
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


# Measured on the match boot after the 2026-09-05 cut, and a RATCHET, not a target:
# any new module-level import of proposal_assist from the always-loaded path pushes
# it up and reddens this. Lowering it is welcome — see the module docstring for the
# two things standing between 52 and 0.
MATCH_PROPOSAL_ASSIST_CEILING = 52


def test_a_match_boot_loads_none_of_the_heavy_proposal_assist_dependencies():
    """The three libraries only the full edition needs must not be imported at all.

    This is the assertion that matters: `pycrdt` (collaborative editing),
    `prosemirror` (the document model) and `claude_agent_sdk` (drafting) are the
    weight, and a match deployment neither serves nor sells any of them.
    """
    got = _boot("match")
    for dep in ("pycrdt", "prosemirror", "claude_agent_sdk"):
        assert got[dep] is False, (
            f"a match boot imported {dep}, which only the full edition needs. "
            f"probe: {got}"
        )


def test_a_match_boot_does_not_grow_its_proposal_assist_footprint():
    got = _boot("match")
    assert got["proposal_assist"] <= MATCH_PROPOSAL_ASSIST_CEILING, (
        f"a match boot imported {got['proposal_assist']} proposal_assist modules, "
        f"above the {MATCH_PROPOSAL_ASSIST_CEILING} ratchet — something was added to "
        "the always-loaded import path. Defer it, or lower the ceiling if you cut one."
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
