"""Every board route keyed on {board_id} must go through `_board_for_edit`.

Spec §4.3: "Boards reuse that rule exactly, so no second permission model
appears." `_board_for_edit` is a 404-lookup followed by `_profile_for_edit`,
which is the single-source edit rule. The shape that silently replaces it is
`session.get(Board, board_id)` -- it returns the same object, so every
happy-path test stays green while any authenticated org user can approve onto,
reorder, or strip any board. That was MEASURED, not imagined: swapping the call
in five of six handlers left all seventeen tests of that day passing.

TWO HALVES, because a positive half alone drifts -- the same argument
`test_profile_access_single_source.py:25-38` makes for its own gate:

  positive: each {board_id} handler CALLS `_board_for_edit`, unconditionally,
            on its own `board_id`, before it writes.
  negative: no {board_id} handler mentions `session.get(Board, ...)` at all.

WHAT TRIVIALLY PASSES A WEAKER VERSION OF THIS, all measured on copies:

  * A gate over a set the scan lost. The first version used FLOORS
    (`len(routed) >= 10` against 11) and one handler could leave silently. The
    second replaced them with a regex text count -- but both derivations read
    the same file and an indented decorator drops BOTH in lockstep, so six of
    the nine keyed handlers could still vanish. Only an exact hand-written PIN
    leaves the file, which is why `_EXPECTED_ROUTES` below is written out.
  * `ast.walk` over the handler. It admits a call inside `if False:`, in a
    nested `def` nobody invokes, or wrapped in `try/except HTTPException: pass`
    with `session.get` doing the real work -- and the last is not hypothetical,
    because one handler already calls `_board_for_edit` purely for effect.
  * Statement-level filtering that then walks inside the statement. It
    re-admits `_prefetched(...) or _board_for_edit(...)`, a ternary, a lambda,
    and a zero-iteration comprehension. A plausible perf refactor, not
    adversarial code.
  * A call placed AFTER the write and the commit, which refuses a request that
    already persisted.

WHY THE NEGATIVE HALF IS CHEAP AND STRONG. `session.get(Board, ...)` appears
exactly once in the module -- inside `_board_for_edit` itself -- so banning it
from handler bodies is green today and kills the ternary, `or`, lambda,
comprehension and after-the-write bypasses by construction, without this file
having to model Python's evaluation order.
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
_BASELINE = Path(__file__).with_name("full_edition_routes.txt")
_GATE = "_board_for_edit"
_BYPASS = "session.get(Board"

# The EXACT route set this module registers, pinned by hand.
#
# This is the only derivation that does not read `boards.py`, and it is the one
# that makes the gate trustworthy: a handler that leaves the AST scan -- moved
# to a sibling module, nested inside a factory, given a non-literal decorator
# path -- still has its pin here, and the equality below goes red. Every
# derivation that reads the file loses the handler and the pin together.
#
# Adding an entry is a product decision, like `_ALWAYS_ROUTERS` in
# test_edition_surface.py: it means a new board route exists, and if it is
# keyed on {board_id} it must satisfy both halves of this gate.
_EXPECTED_ROUTES = frozenset(
    {
        # Collection level -- no board to authorize against yet. These two use
        # `readable_profile_ids` and `_profile_for_edit` on the target profile
        # directly, and are the only routes exempt from the {board_id} rule.
        ("get", "/boards"),
        ("post", "/boards"),
        # Keyed on a board. All nine must call `_board_for_edit`.
        ("get", "/boards/{board_id}"),
        ("put", "/boards/{board_id}"),
        ("delete", "/boards/{board_id}"),
        ("get", "/boards/{board_id}/queue"),
        ("get", "/boards/{board_id}/items"),
        ("post", "/boards/{board_id}/items"),
        ("put", "/boards/{board_id}/items"),
        ("delete", "/boards/{board_id}/items/{call_id}"),
        ("put", "/boards/{board_id}/publish"),
    }
)

# Any attribute on `router`, indented or not. `^@router\.` was the earlier
# form and it missed an indented decorator, which is exactly the nesting shape
# that let a handler leave the scan; `(get|post|put|delete|patch)` would have
# gone falsely red on a future `@router.websocket(`.
_DECORATOR_LINE = re.compile(r"^[ \t]*@router\.[a-z_]+\(", re.MULTILINE)


def _routed_handlers() -> list[tuple[str, str, ast.FunctionDef]]:
    """(method, path, node) for every top-level `@router.<verb>("<path>")` function."""
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


def _always_entered(body: list[ast.stmt]) -> list[ast.stmt]:
    """Statements the handler reaches on every call, flattened.

    Descends `with` blocks, and `try` blocks whose handlers all re-raise -- the
    live idiom in this package (`profiles.py:380-394`, `annotations.py:99`,
    `admin_golden_set.py:87-97`) is a `try` inside `with sf() as session:`, and
    a gate that goes red on correct code "gets deleted or exempted into
    meaninglessness on day one" (test_profile_access_single_source.py:38-40).
    A `try` with a SWALLOWING handler is not descended, which is what keeps the
    `except HTTPException: pass` bypass red.

    Never descends `if`, loops, or a nested function.
    """
    out: list[ast.stmt] = []
    for stmt in body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            # A nested def is a definition, not a call. Keeping it would let
            # `def _unused(): return _board_for_edit(...)` satisfy the positive
            # half -- measured: without this the negative half was the only
            # thing catching that mutation, so a bypass that avoided
            # `session.get(Board, ...)` would have passed both.
            continue
        if isinstance(stmt, ast.With | ast.AsyncWith):
            out.extend(_always_entered(stmt.body))
        elif isinstance(stmt, ast.Try):
            if all(_reraises(h) for h in stmt.handlers):
                out.extend(_always_entered(stmt.body))
                out.extend(_always_entered(stmt.finalbody))
        else:
            out.append(stmt)
    return out


def _reraises(handler: ast.ExceptHandler) -> bool:
    """True when this except-branch cannot swallow: every path raises."""
    return any(isinstance(n, ast.Raise) for n in ast.walk(handler))


_CONDITIONAL = (
    ast.IfExp,
    ast.BoolOp,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


def _unconditional_calls(stmt: ast.stmt, name: str) -> list[ast.Call]:
    """Calls to `name` in `stmt` that are NOT under a conditional expression.

    Filtering at the statement level and then walking inside it re-admits
    `_prefetched(...) or _board_for_edit(...)`, a ternary, a lambda and a
    zero-iteration comprehension -- all measured green against an earlier
    version of this gate, and all plausible refactors rather than adversarial
    code. So a candidate is rejected if any ancestor within the statement is
    one of those.
    """
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(stmt):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    found: list[ast.Call] = []
    for node in ast.walk(stmt):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != name:
            continue
        cur: ast.AST | None = node
        conditional = False
        while cur is not None and cur is not stmt:
            cur = parents.get(id(cur))
            if isinstance(cur, _CONDITIONAL):
                conditional = True
                break
        if not conditional:
            found.append(node)
    return found


def _gate_calls(node) -> list[ast.Call]:
    """`_board_for_edit` calls the handler always reaches, unconditionally."""
    calls: list[ast.Call] = []
    for stmt in _always_entered(node.body):
        # `If.test` is evaluated on every call, so a walrus guard
        # `if (board := _board_for_edit(...)):` counts; `If.body` does not.
        targets = [stmt.test] if isinstance(stmt, ast.If) else [stmt]
        for target in targets:
            calls.extend(_unconditional_calls(target, _GATE))
    return calls


def test_the_scan_sees_exactly_the_pinned_route_set():
    """The anti-vacuity half, and it must be an exact SET, not a floor.

    Two earlier versions failed here. The first used floors and one handler
    could leave the scan silently. The second cross-checked the AST count
    against a regex text count -- but both read the same file and move
    together, so an indented or relocated handler dropped both; measured, six
    of the nine keyed handlers could vanish with the gate green. The route
    baseline was the only external derivation, and it holds unique PATHS, not
    operations (`test_edition_surface.py:135-141`), so it covers only the three
    keyed handlers that are the sole occupants of their path.

    `_EXPECTED_ROUTES` is written by hand and does not read `boards.py`, so a
    handler that leaves the file leaves the scan but keeps its pin.
    """
    scanned = {(m, p) for m, p, _n in _routed_handlers()}
    assert scanned == set(_EXPECTED_ROUTES), (
        f"the scan and the pinned route set disagree.\n"
        f"  pinned but NOT scanned (silently unprotected): "
        f"{sorted(set(_EXPECTED_ROUTES) - scanned)}\n"
        f"  scanned but NOT pinned (a new route -- is it {{board_id}}-keyed?): "
        f"{sorted(scanned - set(_EXPECTED_ROUTES))}"
    )


def test_the_pin_still_matches_the_committed_route_baseline():
    """A second, independent tie, so the pin itself cannot rot.

    `full_edition_routes.txt` is generated from the real app and its count
    literal in `test_edition_surface.py` is hand-maintained, so a pin that
    drifts from the routes the app actually serves fails here. It is
    path-granular, which is why it is a second check and not the primary one.

    The `/api/boards` filter is deliberate: Task 7's userless public endpoint
    is keyed on the slug, lives in `web/api/public_boards.py`, and is
    authorized by publication rather than by a user, so it neither enters nor
    should enter this comparison.
    """
    pinned_paths = {"/api" + p for _m, p in _EXPECTED_ROUTES}
    baseline_paths = {
        line for line in _BASELINE.read_text().split() if line.startswith("/api/boards")
    }
    assert pinned_paths == baseline_paths, (
        f"the pin and the committed route baseline disagree.\n"
        f"  in the baseline but NOT pinned: {sorted(baseline_paths - pinned_paths)}\n"
        f"  pinned but NOT in the baseline: {sorted(pinned_paths - baseline_paths)}"
    )


def test_the_decorator_count_matches_the_scan():
    """A third tie, against a handler the AST scan alone would lose.

    A non-literal decorator path (`@router.put(_PUB)`) drops the handler from
    the AST scan while the decorator line is still there. This is the one shape
    the text count genuinely adds; it does NOT cover nesting or a module move,
    which is what the pin above is for.
    """
    text_count = len(_DECORATOR_LINE.findall(_SOURCE.read_text()))
    scanned = _routed_handlers()
    assert len(scanned) == text_count, (
        f"the AST scan found {len(scanned)} handlers but there are {text_count} "
        f"`@router.<verb>(` lines in {_SOURCE.name} -- a non-literal decorator "
        f"path leaves a handler out of the scan."
    )


def test_only_the_collection_routes_are_exempt_from_the_board_id_rule():
    """Which routes this gate deliberately does NOT cover.

    `GET /boards` and `POST /boards` are collection-level: there is no board to
    authorize against yet, so they use `readable_profile_ids` and
    `_profile_for_edit` on the target profile directly.

    NOTE FOR A LATER TASK. This makes every future `{board_id}` route in this
    module require EDIT permission, and it denies by default. A legitimately
    read-only board route keyed on `board_id` WILL go red at
    `test_every_board_id_route_calls_board_for_edit` below -- and should, until
    someone decides deliberately that it is right. Record that decision the way
    `test_profile_access_single_source.py` does: a named exempt set with a
    reason per entry, so the exemption is reviewable rather than a deleted
    assertion.
    """
    routed = {p for _m, p, _n in _routed_handlers()}
    keyed = {p for _m, p, _n in _board_id_handlers()}
    assert routed - keyed == {"/boards"}


@pytest.mark.parametrize(
    "method,path,name",
    [(m, p, n.name) for m, p, n in _board_id_handlers()],
    ids=[f"{m.upper()} {p}" for m, p, _n in _board_id_handlers()],
)
def test_every_board_id_route_calls_board_for_edit(method, path, name):
    """The positive half: the call is there, on this board_id, before the write."""
    node = next(n for m, p, n in _board_id_handlers() if n.name == name)
    calls = _gate_calls(node)
    assert calls, (
        f"{method.upper()} {path} ({name}) does not unconditionally call {_GATE}. "
        f"§4.3 says boards reuse the profile edit rule exactly; "
        f"session.get(Board, board_id) returns the same object and leaves every "
        f"happy-path test green."
    )
    for call in calls:
        arg = {k.arg: k.value for k in call.keywords}.get("board_id")
        assert isinstance(arg, ast.Name) and arg.id == "board_id", (
            f"{name} calls {_GATE} with a board_id that is not its own path "
            f"parameter, so it authorizes one board and acts on another"
        )

    # Ordering: a refusal issued after the commit refuses a request that has
    # already persisted. Cheapest sound form -- the gate call must come before
    # every commit in the handler.
    commits = [
        n.lineno
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "commit"
    ]
    if commits:
        assert min(c.lineno for c in calls) < min(commits), (
            f"{name} calls {_GATE} at line {min(c.lineno for c in calls)}, after a "
            f"commit at line {min(commits)} -- the refusal would come too late to "
            f"stop the write."
        )


@pytest.mark.parametrize(
    "method,path,name",
    [(m, p, n.name) for m, p, n in _board_id_handlers()],
    ids=[f"{m.upper()} {p}" for m, p, _n in _board_id_handlers()],
)
def test_no_board_id_route_looks_a_board_up_directly(method, path, name):
    """The negative half: the replacement shape must not appear at all.

    `session.get(Board, ...)` occurs exactly once in this module -- inside
    `_board_for_edit` itself -- so this is green today and stays cheap. It kills
    by construction every bypass that satisfies the positive half while doing
    the real lookup elsewhere: a ternary, an `or` fast path, a lambda, a
    zero-iteration comprehension, and a gate call placed after the write.

    `session.get(BoardItem, ...)` is fine and is used; only `Board` is banned.
    """
    node = next(n for m, p, n in _board_id_handlers() if n.name == name)
    offenders = [
        n.lineno
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "get"
        and n.args
        and isinstance(n.args[0], ast.Name)
        and n.args[0].id == "Board"
    ]
    assert not offenders, (
        f"{method.upper()} {path} ({name}) looks the board up directly at line(s) "
        f"{offenders}. That is the shape that silently replaces {_GATE}: it "
        f"returns the same object, so every happy-path test stays green while any "
        f"authenticated org user can act on any board."
    )
