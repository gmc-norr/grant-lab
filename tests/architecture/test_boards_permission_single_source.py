"""Every board route keyed on {board_id} must go through `_board_for_edit`.

Spec §4.3: "Boards reuse that rule exactly, so no second permission model
appears." `_board_for_edit` is a 404-lookup followed by `_profile_for_edit`,
which is the single-source edit rule; `session.get(Board, board_id)` is the
shape that silently replaces it, because it returns the same object and every
happy-path test stays green.

WHY THIS IS STRUCTURAL AND NOT BEHAVIOURAL. Task 6's review measured that
swapping `_board_for_edit` for `session.get` in five of the six new handlers
left all seventeen of that file's tests passing -- any authenticated org user
could then approve onto, reorder, or strip any board. Behavioural cases were
added for two routes; this pins the other seven AND every route a later task
adds, which is the half a per-route test can never cover.

WHAT TRIVIALLY PASSES IT. A gate that only checked "the file mentions
_board_for_edit somewhere" would pass with eight of nine handlers gutted, and
one that counted call sites would pass a handler that called it on a DIFFERENT
id. So this walks each decorated handler's own body and requires the call to
be there, and requires its `board_id=` argument to be the handler's own
`board_id` parameter.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Resolved from __file__, not cwd: the same shape test_edition_surface.py:269
# and test_architecture_map_is_current.py:17 use, so the gate cannot go
# vacuously green just because pytest was invoked from another directory.
_SOURCE = Path(__file__).resolve().parents[2] / "src/grant_lab/web/api/boards.py"
_GATE = "_board_for_edit"


def _routed_handlers() -> list[tuple[str, str, ast.FunctionDef]]:
    """(method, path, node) for every @router.<verb>("<path>") function."""
    tree = ast.parse(_SOURCE.read_text())
    out: list[tuple[str, str, ast.FunctionDef]] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            fn = dec.func
            if not (
                isinstance(fn, ast.Attribute)
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "router"
            ):
                continue
            if not (dec.args and isinstance(dec.args[0], ast.Constant)):
                continue
            out.append((fn.attr, dec.args[0].value, node))
    return out


def _board_id_handlers() -> list[tuple[str, str, ast.FunctionDef]]:
    return [h for h in _routed_handlers() if "{board_id}" in h[1]]


def test_the_scan_finds_the_routes_it_claims_to_scan():
    """A gate over an empty set is green and worthless.

    Both halves matter: if the decorator shape ever changes and `_routed_handlers`
    silently returns nothing, every assertion below passes vacuously.
    """
    routed = _routed_handlers()
    assert len(routed) >= 10, f"only found {len(routed)} routed handlers in {_SOURCE}"
    keyed = _board_id_handlers()
    assert len(keyed) >= 8, f"only found {len(keyed)} handlers keyed on board_id"
    # The two collection-level routes must NOT be in the keyed set.
    assert {p for _m, p, _n in routed} - {p for _m, p, _n in keyed} == {"/boards"}


@pytest.mark.parametrize(
    "method,path,name",
    [(m, p, n.name) for m, p, n in _board_id_handlers()],
    ids=[f"{m.upper()} {p}" for m, p, _n in _board_id_handlers()],
)
def test_every_board_id_route_calls_board_for_edit(method, path, name):
    node = next(n for m, p, n in _board_id_handlers() if n.name == name)
    calls = [
        c
        for c in ast.walk(node)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == _GATE
    ]
    assert calls, (
        f"{method.upper()} {path} ({name}) does not call {_GATE}. §4.3 says boards "
        f"reuse the profile edit rule exactly; session.get(Board, board_id) returns "
        f"the same object and leaves every happy-path test green."
    )
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        arg = kw.get("board_id")
        assert isinstance(arg, ast.Name) and arg.id == "board_id", (
            f"{name} calls {_GATE} with board_id={ast.dump(arg) if arg else None!s}, "
            f"not its own board_id path parameter"
        )
