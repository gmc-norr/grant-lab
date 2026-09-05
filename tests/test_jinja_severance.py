"""Regression guard for the Jinja teardown (G1).

The React cockpit's kept backend — the JSON ``web/api`` package and the 7
``proposal_assist/web`` JSON modules — must import NOTHING from the doomed Jinja
UI modules (deleted in G2). If this fails, a kept module re-coupled to a module
scheduled for deletion; move the shared symbol to a neutral home instead (see
proposal_assist/web/_deps.py, workspace_meta.py, _file_uploads.py,
references/uploads.py).
"""

from __future__ import annotations

import pathlib
import re

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src" / "grant_lab"

_DOOMED = ("workspaces", "files", "references", "sections", "citations", "annotations")

# Kept: the whole web/api package (routers + support files) ...
_KEPT_API = sorted((_SRC / "web" / "api").glob("*.py"))
# ... plus the 7 live JSON modules under proposal_assist/web.
_KEPT_JSON = [
    _SRC / "proposal_assist" / "web" / f"{m}.py"
    for m in (
        "proposals",
        "activity",
        "problems",
        "coherence",
        "evaluator",
        "review",
        "uncited_claims",
    )
]
_KEPT = _KEPT_API + _KEPT_JSON

# Absolute import of a doomed proposal_assist.web module (unambiguous).
_ABS = re.compile(
    r"from\s+grant_lab\.proposal_assist\.web\.(" + "|".join(_DOOMED) + r")\s+import"
)
# Same-package relative import (only meaningful inside proposal_assist/web,
# where `.workspaces` IS the doomed module). NOT applied to web/api, whose
# own `.workspaces` is the KEPT web/api/workspaces.py.
_REL = re.compile(r"from\s+\.(" + "|".join(_DOOMED) + r")\s+import")


def test_kept_modules_do_not_import_from_doomed_jinja_modules():
    offenders = []
    for path in _KEPT:
        for m in _ABS.finditer(path.read_text(encoding="utf-8")):
            offenders.append(
                f"{path.relative_to(_REPO_ROOT)} -> proposal_assist.web.{m.group(1)} (absolute)"
            )
    for path in _KEPT_JSON:
        for m in _REL.finditer(path.read_text(encoding="utf-8")):
            offenders.append(
                f"{path.relative_to(_REPO_ROOT)} -> .{m.group(1)} (relative, same package)"
            )
    assert not offenders, "kept modules re-coupled to doomed Jinja modules:\n" + "\n".join(
        offenders
    )


def test_guard_kept_set_is_real_not_vacuous():
    # If the globs/lists silently resolved to nothing, the guard above would
    # pass vacuously. Pin the kept set so that can't happen unnoticed.
    for p in _KEPT:
        assert p.exists(), f"kept module missing: {p}"
    assert len(_KEPT_API) >= 10, "web/api package glob resolved too few modules"
    assert {p.stem for p in _KEPT_JSON} == {
        "proposals",
        "activity",
        "problems",
        "coherence",
        "evaluator",
        "review",
        "uncited_claims",
    }
    # web/api/workspaces.py (the KEPT /api module) is in the scanned set — proof
    # the guard does not confuse it with the doomed proposal_assist/web/workspaces.py.
    assert (_SRC / "web" / "api" / "workspaces.py") in _KEPT_API
