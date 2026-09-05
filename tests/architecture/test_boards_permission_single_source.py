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
import re
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


_BASELINE = Path(__file__).with_name("full_edition_routes.txt")
_DECORATOR_LINE = re.compile(r"^@router\.(get|post|put|delete|patch)\(", re.MULTILINE)


def test_the_scan_sees_every_handler_and_every_registered_path():
    """The anti-vacuity half, and it must be EXACT rather than a floor.

    A first version of this file asserted `len(routed) >= 10` against an actual
    11. A review measured that one handler could then leave the AST scan with
    every assertion still green -- give one decorator a non-literal path, or
    define one inside a factory so it is not in `tree.body` -- and the gate
    would silently stop protecting it. `PUT /boards/{board_id}/publish` is the
    sole handler on its path, so dropping it passed all three assertions.

    Two independent derivations close that, and neither needs the app booted:

    1. A plain TEXT count of `@router.<verb>(` lines. The AST scan is the only
       thing that can lose a handler to a non-literal path or a nested
       definition; the regex still sees the decorator. They must agree.
    2. The scanned paths against `full_edition_routes.txt`, which is committed,
       generated from the real app, and whose own count literal
       `test_edition_surface.py` makes a hand-maintained act. A route MOVED to
       a sibling module leaves both this file and the AST scan, but its path
       stays in the baseline -- so it fails here.
    """
    source = _SOURCE.read_text()
    text_count = len(_DECORATOR_LINE.findall(source))
    scanned = _routed_handlers()
    assert len(scanned) == text_count, (
        f"the AST scan found {len(scanned)} handlers but there are {text_count} "
        f"`@router.<verb>(` lines in {_SOURCE.name}. A handler that leaves the "
        f"scan -- a non-literal decorator path, or a definition nested inside a "
        f"factory -- is silently unprotected by every assertion below."
    )
    scanned_paths = {"/api" + p for _m, p, _n in scanned}
    baseline_paths = {
        line for line in _BASELINE.read_text().split() if line.startswith("/api/boards")
    }
    assert scanned_paths == baseline_paths, (
        f"the scan and the committed route baseline disagree.\n"
        f"  in the baseline but NOT scanned (unprotected): "
        f"{sorted(baseline_paths - scanned_paths)}\n"
        f"  scanned but NOT in the baseline (stale baseline?): "
        f"{sorted(scanned_paths - baseline_paths)}"
    )


def test_only_the_collection_routes_are_exempt_from_the_board_id_rule():
    """Which routes this gate deliberately does NOT cover.

    `GET /boards` and `POST /boards` are collection-level: there is no board to
    authorize against yet, so they use `readable_profile_ids` and
    `_profile_for_edit` on the target profile directly. Every other route in
    this module is keyed on a board and must go through `_board_for_edit`.

    NOTE FOR A LATER TASK. This makes every future `{board_id}` route in this
    module require EDIT permission. A legitimately read-only board route keyed
    on `board_id` would fail this gate -- and should, until someone decides
    deliberately that it is right and records why. The userless PUBLIC board
    endpoint does not apply: it is keyed on the slug, lives in its own module,
    and is authorized by publication rather than by a user.
    """
    routed = {p for _m, p, _n in _routed_handlers()}
    keyed = {p for _m, p, _n in _board_id_handlers()}
    assert routed - keyed == {"/boards"}


def _gate_calls_in_own_body(node) -> list[ast.Call]:
    """`_board_for_edit` calls the handler actually REACHES, unconditionally.

    `ast.walk` was the first version and it admits three shapes that ship the
    exact bug this gate exists to stop: a call inside `if False:`, a call in a
    nested `def` nobody invokes, and -- the realistic one --

        try:
            _board_for_edit(session, board_id=board_id, user=user)
        except HTTPException:
            pass
        board = session.get(Board, board_id)

    which swallows the refusal. That idiom is not hypothetical here: one
    handler already calls `_board_for_edit` purely for effect and discards the
    result, so a reviewer would not blink at the shape.

    So: descend only through statement bodies the handler always enters -- its
    own top level and `with` blocks -- and never into `Try`, `If`, loops, or a
    nested function.
    """
    found: list[ast.Call] = []

    def scan(body) -> None:
        for stmt in body:
            if isinstance(stmt, ast.With | ast.AsyncWith):
                scan(stmt.body)
                continue
            if isinstance(stmt, ast.Expr | ast.Assign | ast.AnnAssign | ast.Return):
                for c in ast.walk(stmt):
                    if (
                        isinstance(c, ast.Call)
                        and isinstance(c.func, ast.Name)
                        and c.func.id == _GATE
                    ):
                        found.append(c)

    scan(node.body)
    return found


@pytest.mark.parametrize(
    "method,path,name",
    [(m, p, n.name) for m, p, n in _board_id_handlers()],
    ids=[f"{m.upper()} {p}" for m, p, _n in _board_id_handlers()],
)
def test_every_board_id_route_calls_board_for_edit(method, path, name):
    node = next(n for m, p, n in _board_id_handlers() if n.name == name)
    calls = _gate_calls_in_own_body(node)
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
