"""`readable_profile_ids()` must stay the ONLY definition of who may read a profile.

The whole "profiles are personal" plan reduces to one property: every surface
calls the predicate, and nothing reimplements it. That property is not local to
any one file, so no unit test can hold it -- which is why it lives here.

The failure mode is silent and partial. Eleven of the twelve surfaces get the
rule right, the twelfth keeps its own existence-only lookup, and a private
profile leaks through exactly one route. It is not hypothetical: the first draft
of this plan pinned SEVEN surfaces, an outside review found three more, the
controller found an eleventh (`calls.py::get_call_detail` resolves `?profile=`
with its own lookup, at calls.py:273-277), and the final whole-branch review
found a TWELFTH outside this plan's vocabulary entirely --
`web/api/workspaces.py::create_workspace_api`, which validated `profile_id` by
existence alone (PD-9).

TWO HALVES, and each covers a hole the other cannot see:

  POSITIVE -- each pinned function contains a CALL to the predicate. Deliberately
  a call and not an import: an import survives the removal of its call, so an
  import-counting gate stays green while `assemble.py` keeps `readable_profile_ids`
  in its import block and quietly drops the `.where(...)` that used it. And the
  pin is an exact SET, not a floor -- "at least six" over a six-member set
  tolerates one being dropped the day a seventh is added.

  NEGATIVE -- outside a named exempt set, no module may reference
  `Profile.visibility` or `ProfileViewer`, and no module may COMPARE any
  `.visibility` attribute. That is what stops a surface from being "wired" with
  its own second copy of the arms, which would satisfy the positive half and
  still drift. The comparison half is the one the plan's spec got wrong (PD-11):
  it banned `Profile.visibility`, the CLASS attribute, but a second predicate is
  realistically written on the loaded row -- `if p.visibility != "org"` in a
  route that already has the object in hand -- which the class-attribute rule
  never sees.

An EARLIER draft of the negative half banned `ProfileViewer` and
`Profile.visibility` everywhere, which a reviewer showed can never pass:
models.py declares them, migration 0070 backfills, and Task 3's viewer CRUD must
read and write the rows. A gate that is red on correct code gets deleted or
exempted into meaninglessness on day one. Hence the exemptions below, each with
its reason, and each as narrow as it can be -- the profiles.py exemptions are by
FUNCTION NAME, so a renamed function loses its exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "call_manager"

# The two entry points. Either one satisfies a pin: the set-valued callers use
# the subquery, the single-object callers use the wrapper, and the wrapper is
# itself built from the subquery (pinned by
# test_can_read_profile_is_built_from_the_subquery below), so there is still
# exactly one rule.
PREDICATE_NAMES = ("readable_profile_ids", "can_read_profile")

DEFINITION = "web/profile_access.py"

# --------------------------------------------------------------------------
# The pinned surfaces. Exact names, exact set.
# --------------------------------------------------------------------------

# Every surface that resolves, lists or renders a profile for a user.
PREDICATE_PINS: tuple[str, ...] = (
    "web/api/profiles.py::list_profiles",
    "web/api/profiles.py::get_profile",
    "web/api/profiles.py::update_profile",
    "web/dashboard.py::resolve_profile_selection",
    "web/dashboard.py::set_default_profile",
    "web/dashboard.py::build_dashboard_view",
    "web/api/me.py::get_settings",
    "web/api/me.py::update_subscriptions",
    # The eleventh surface, missed by the plan: it resolves ?profile= against
    # is_active ALONE, not through the dashboard resolver.
    "web/api/calls.py::get_call_detail",
    "digest/assemble.py::assemble_for_user",
    # The twelfth, found by the final whole-branch review (PD-9). Not a
    # "profile" surface by name at all: POST /api/workspaces validated its
    # optional profile_id by EXISTENCE, so 400-vs-201 was an oracle, and
    # Phase-0 copies the profile's description into the workspace snapshot the
    # drafting agent reads back.
    "web/api/workspaces.py::create_workspace_api",
    # The sharing guard. Outside review 2026-09-04 (Sol #9) found it unpinned:
    # deleting its `can_read_profile` line left every architecture gate green
    # while turning the four sharing routes into a 403-vs-404 existence oracle.
    # MEASURED: the four behavioural cases in
    # tests/test_profile_access.py::test_sharing_ops_404_for_a_stranger_with_no_existence_oracle
    # did go red, so the hole was in THIS file, not in the coverage.
    "web/api/profiles.py::_profile_for_edit",
)

# Pinned on DELEGATION rather than on the predicate. `set_default` is a
# four-line wrapper whose entire job is to call `dashboard.set_default_profile`
# (which is itself pinned above). Requiring a predicate call here too would
# force a redundant second check into a delegate -- the exact duplication this
# file exists to prevent. What must not happen is the delegate growing its own
# body, so what is pinned is that it still delegates.
DELEGATION_PINS: dict[str, str] = {
    "web/api/me.py::set_default": "set_default_profile",
    # The four sharing operations. Each is exempt from the NEGATIVE half (they
    # manage viewer rows), which previously left them pinned by nothing at all:
    # an exemption is not coverage. They must keep going through the guard.
    "web/api/profiles.py::list_profile_viewers": "_profile_for_edit",
    "web/api/profiles.py::replace_profile_viewers": "_profile_for_edit",
    "web/api/profiles.py::add_profile_viewer": "_profile_for_edit",
    "web/api/profiles.py::set_profile_visibility": "_profile_for_edit",
}

# The pin sets, written out a SECOND time as literals. Deliberate duplication:
# the point is that changing PREDICATE_PINS alone cannot pass. A legitimate new
# surface changes both, in one diff, where a reviewer sees the pair.
EXPECTED_PREDICATE_PINS = frozenset({
    "web/api/profiles.py::list_profiles",
    "web/api/profiles.py::get_profile",
    "web/api/profiles.py::update_profile",
    "web/api/profiles.py::_profile_for_edit",
    "web/dashboard.py::resolve_profile_selection",
    "web/dashboard.py::set_default_profile",
    "web/dashboard.py::build_dashboard_view",
    "web/api/me.py::get_settings",
    "web/api/me.py::update_subscriptions",
    "web/api/calls.py::get_call_detail",
    "digest/assemble.py::assemble_for_user",
    "web/api/workspaces.py::create_workspace_api",
})
EXPECTED_DELEGATION_PINS = frozenset({
    "web/api/me.py::set_default",
    "web/api/profiles.py::list_profile_viewers",
    "web/api/profiles.py::replace_profile_viewers",
    "web/api/profiles.py::add_profile_viewer",
    "web/api/profiles.py::set_profile_visibility",
})


# --------------------------------------------------------------------------
# The negative half's exempt set. Each entry states why.
# --------------------------------------------------------------------------

# Whole modules. (alembic/versions/0070_profile_visibility.py backfills the
# viewer table, but it lives outside src/ and so is never scanned.)
EXEMPT_MODULES: dict[str, str] = {
    "db/models.py": "declares Profile.visibility and the ProfileViewer table",
    DEFINITION: "IS the single definition of the read rule",
}

# Functions inside web/api/profiles.py that Task 3 creates for viewer CRUD.
# They MANAGE the rows; they do not DECIDE access. Exempt by name, so a
# renamed or repurposed function loses the exemption and the gate notices.
EXEMPT_PROFILE_FUNCTIONS: dict[str, str] = {
    "list_profile_viewers": "reads the viewer rows to show them, does not gate on them",
    "replace_profile_viewers": "writes the viewer rows",
    "add_profile_viewer": "writes one viewer row",
    "set_profile_visibility": "writes the visibility column",
}
EXEMPT_FUNCTION_MODULE = "web/api/profiles.py"


# --------------------------------------------------------------------------
# AST helpers
# --------------------------------------------------------------------------


def _modules() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def _parse(rel: str) -> ast.Module:
    path = SRC / rel
    assert path.is_file(), f"pinned module no longer exists: {rel}"
    return ast.parse(path.read_text(), filename=str(path))


def _callee_name(call: ast.Call) -> str | None:
    """The bare name being called: `f()` -> 'f', `mod.f()` -> 'f'."""
    fn = call.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return None


def _resolve(qualname: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """The top-level function `qualname` names, or a hard failure.

    A missing pin must NEVER be a silent skip. Renaming a pinned function is
    indistinguishable, from the gate's point of view, from deleting the surface
    -- and a gate that quietly passes over the name it cannot find is a gate
    that stops covering whatever the rename touched.
    """
    rel, _, func = qualname.partition("::")
    module = _parse(rel)
    for node in module.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == func:
            return node
    raise AssertionError(
        f"pinned function no longer exists: {qualname}. If it was renamed or "
        f"moved, update the pin in {Path(__file__).name} so the surface stays "
        f"covered; do not delete the pin."
    )


def _body_calls(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names called anywhere in the function's BODY.

    Only the body: a decorator or a default-argument call is not the function
    doing the check.
    """
    out: set[str] = set()
    for stmt in fn.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                name = _callee_name(node)
                if name is not None:
                    out.add(name)
    return out


# Callee names whose ARGUMENT position turns the predicate from a value into a
# filter: `Profile.id.in_(readable_profile_ids(user))`. MEASURED 2026-09-04 --
# across the twelve pinned surfaces the predicate appears in exactly two shapes,
# an `if` test (8 sites) and an `in_()` argument (4 sites), plus one site
# (me.py::update_subscriptions) that binds the result to a name and passes THAT
# to `in_()`.
FILTER_CALLEES = frozenset({"in_", "where", "filter", "any_"})


def _parents(fn: ast.AST) -> dict[ast.AST, ast.AST]:
    out: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(fn):
        for child in ast.iter_child_nodes(node):
            out[child] = node
    return out


def _guarding_nodes(fn: ast.AST) -> set[ast.AST]:
    """Every node sitting inside a branch TEST or inside a filter-call argument.

    These are the two positions in which a value actually decides something. A
    node anywhere else is a value the function computed and may ignore.
    """
    out: set[ast.AST] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.If | ast.IfExp | ast.While):
            out.update(ast.walk(node.test))
        if isinstance(node, ast.Call) and _callee_name(node) in FILTER_CALLEES:
            for arg in (*node.args, *(kw.value for kw in node.keywords)):
                out.update(ast.walk(arg))
    return out


def _predicate_is_load_bearing(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """The predicate is CALLED *and its result decides something*.

    Asserting only that the name is called is the hollow shape outside review
    found (grok #2, Sol #7/#8) and that I then reproduced: with
    `_unused = readable_profile_ids(user)` left in `assemble.py` and the
    `.where(...)` deleted, all 29 tests in this file passed. That is the access
    rule reverted with the call left behind.
    """
    guarding = _guarding_nodes(fn)
    parent = _parents(fn)
    calls = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and _callee_name(node) in PREDICATE_NAMES
    ]
    if any(call in guarding for call in calls):
        return True
    # ...or bound to a name that is itself used in a guarding position.
    bound: set[str] = set()
    for call in calls:
        stmt = parent.get(call)
        if isinstance(stmt, ast.Assign):
            bound.update(tgt.id for tgt in stmt.targets if isinstance(tgt, ast.Name))
    return any(
        isinstance(node, ast.Name) and node.id in bound and node in guarding
        for node in ast.walk(fn)
    )


def _enclosing_functions(module: ast.Module) -> dict[ast.AST, str]:
    """Map every node to the name of the OUTERMOST function containing it.

    Outermost, not nearest, because the profiles.py exemptions are stated in
    terms of the route function; a helper closure defined inside one is part of
    that function's implementation.
    """
    owner: dict[ast.AST, str] = {}

    def walk(node: ast.AST, current: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            name = current
            if current is None and isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = child.name
            if name is not None:
                owner[child] = name
            walk(child, name)

    walk(module, None)
    return owner


_ORM_SYMBOLS = ("Profile", "ProfileViewer")


def _bound_names(module: ast.Module) -> dict[str, str]:
    """Local name -> ORM symbol, for the two symbols the negative half bans.

    `from ...models import ProfileViewer as PV` binds `PV`; without this map a
    one-line alias would walk the whole gate. `import ...models as m` needs no
    entry -- `m.ProfileViewer` is caught by the attribute rule below.
    """
    bound = {name: name for name in _ORM_SYMBOLS}
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _ORM_SYMBOLS:
                    bound[alias.asname or alias.name] = alias.name
    return bound


def _is_visibility_load(node: ast.AST | None) -> bool:
    """`x.visibility`, or `getattr(x, "visibility")` -- the same read.

    `getattr` is here because it is the one-character way around an
    attribute-shaped rule, not because anyone has written it.
    """
    if isinstance(node, ast.Attribute) and node.attr == "visibility":
        return True
    return (
        isinstance(node, ast.Call)
        and _callee_name(node) == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "visibility"
    )


def _reads_visibility(node: ast.AST | None) -> bool:
    """Does this expression read `.visibility` ANYWHERE inside it?

    Wider than `_is_visibility_load` on purpose: the assigned value may be
    `p.visibility if p else "org"` or `p.visibility or "org"`. MEASURED --
    a first version of this rule matched only a bare Attribute, and that exact
    ternary mutant survived it (39 passed) while the plain
    `v = p.visibility` mutant was caught.
    """
    return node is not None and any(_is_visibility_load(n) for n in ast.walk(node))


def _visibility_compare_operands(module: ast.Module) -> set[ast.AST]:
    """Every `<anything>.visibility` used as an operand of a comparison.

    The class-attribute rule below sees `select(...).where(Profile.visibility ==
    "org")`. It does NOT see `if p.visibility != "org"`, and that is the shape a
    second predicate actually takes: on the INSTANCE, inside a route that has
    already loaded it. PD-11 / final review I1.

    COMPARISON only, deliberately. The two legitimate uses both stay green
    because neither compares: `{"visibility": p.visibility}` in get_profile is a
    dict value, and `p.visibility = body.visibility` in set_profile_visibility is
    an assignment. A rule over every `.visibility` reference would be red on both
    and would be exempted into meaninglessness by the end of the week.

    Not narrowed by the OTHER operand: `.visibility` occurs five times under
    src/call_manager (measured 2026-09-04) -- the predicate's own
    `Profile.visibility == "org"` plus its docstring, and three lines in
    profiles.py -- so no second model carries a `visibility` attribute for this
    to collide with. Narrow it the day one does.
    """
    # One hop of indirection, because `v = p.visibility` then `v == "org"` is
    # the same second predicate with a line break in it, and the operand-only
    # rule could not see it (outside review 2026-09-04: grok #5, Sol #10).
    aliases: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Assign) and _reads_visibility(node.value):
            aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif (
            isinstance(node, ast.AnnAssign | ast.AugAssign)
            and _reads_visibility(node.value)
            and isinstance(node.target, ast.Name)
        ):
            aliases.add(node.target.id)

    out: set[ast.AST] = set()
    for node in ast.walk(module):
        if not isinstance(node, ast.Compare):
            continue
        for operand in (node.left, *node.comparators):
            if _is_visibility_load(operand) or (
                isinstance(operand, ast.Name) and operand.id in aliases
            ):
                out.add(operand)
    return out


def _profile_visibility_refs(node: ast.AST, bound: dict[str, str]) -> bool:
    """`Profile.visibility` -- and `models.Profile.visibility`, and
    `P.visibility` after `import Profile as P`: the same thing."""
    if not isinstance(node, ast.Attribute) or node.attr != "visibility":
        return False
    base = node.value
    if isinstance(base, ast.Name):
        return bound.get(base.id) == "Profile"
    if isinstance(base, ast.Attribute):
        return base.attr == "Profile"
    return False


def _profile_viewer_ref(node: ast.AST, bound: dict[str, str]) -> bool:
    """A use of the ORM class: a bare or aliased name, or `models.ProfileViewer`.
    An `import` binds an ast.alias, not an ast.Name, so importing the model is
    not itself a violation -- using it is. Detected at the same strength as
    `Profile.visibility` above (review 2026-09-04 I2: the first version matched
    only a bare ast.Name)."""
    if isinstance(node, ast.Name):
        return bound.get(node.id) == "ProfileViewer"
    return isinstance(node, ast.Attribute) and node.attr == "ProfileViewer"


# --------------------------------------------------------------------------
# Positive half
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "qualname",
    [pytest.param(q, id=q) for q in (*PREDICATE_PINS, *DELEGATION_PINS)],
)
def test_pinned_function_still_exists(qualname: str) -> None:
    """Every pin resolves to a real top-level function.

    Separate from the call assertion ON PURPOSE. The two failures say different
    things: "this surface stopped checking" and "this surface is gone". A rename
    that took the predicate call with it would otherwise report only the first,
    and the fix -- add the call back -- would be the wrong one.
    """
    _resolve(qualname)


@pytest.mark.parametrize(
    "qualname", [pytest.param(q, id=q) for q in PREDICATE_PINS]
)
def test_pinned_surface_calls_the_single_source(qualname: str) -> None:
    fn = _resolve(qualname)
    called = _body_calls(fn)
    assert called & set(PREDICATE_NAMES), (
        f"{qualname} does not CALL {' or '.join(PREDICATE_NAMES)}. Every surface "
        f"that resolves a profile for a user must go through "
        f"src/call_manager/{DEFINITION}; an import alone is not enough."
    )
    assert _predicate_is_load_bearing(fn), (
        f"{qualname} CALLS {' or '.join(PREDICATE_NAMES)} but does not USE the "
        f"result to decide anything: it reaches no `if` test and no query "
        f"filter. A call whose value is discarded is the access rule reverted "
        f"with the call left behind, which an earlier version of this gate "
        f"could not tell from the real thing."
    )


@pytest.mark.parametrize(
    ("qualname", "callee"),
    [pytest.param(q, c, id=q) for q, c in DELEGATION_PINS.items()],
)
def test_pinned_delegate_still_delegates(qualname: str, callee: str) -> None:
    fn = _resolve(qualname)
    assert callee in _body_calls(fn), (
        f"{qualname} no longer calls {callee}(). It is a delegate: if it grows "
        f"its own body it needs its own profile-read check, and then there are "
        f"two rules again."
    )


def test_can_read_profile_is_built_from_the_subquery() -> None:
    """The wrapper must COMPOSE the subquery, not restate the arms beside it."""
    fn = _resolve(f"{DEFINITION}::can_read_profile")
    assert "readable_profile_ids" in _body_calls(fn), (
        "can_read_profile must be implemented in terms of readable_profile_ids; "
        "a second hand-written arm list is exactly the drift this file bans."
    )


def test_readable_profile_ids_is_defined_exactly_once() -> None:
    """No second definition anywhere under src/."""
    definitions: dict[str, list[str]] = {name: [] for name in PREDICATE_NAMES}
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if node.name in definitions:
                definitions[node.name].append(_rel(path))
    assert definitions == {name: [DEFINITION] for name in PREDICATE_NAMES}, definitions


# --------------------------------------------------------------------------
# Negative half
# --------------------------------------------------------------------------


def test_no_module_builds_a_second_read_predicate() -> None:
    """Outside the exempt set, nothing touches the access MECHANISM.

    Three shapes are banned: a `Profile.visibility` reference, a `ProfileViewer`
    reference, and ANY `.visibility` attribute compared against anything. The
    third was added after the final review (PD-11) -- the first two are written
    in terms of the ORM CLASS, and the realistic second predicate is written on
    a loaded ROW, which the class rule cannot see however it is aliased.

    Stricter than "inside a function body" on purpose: a module-level constant
    holding `select(ProfileViewer.profile_id).where(...)` is a second predicate
    just as much as an inline one, and it is the shape a well-meaning refactor
    would reach for.
    """
    violations: list[str] = []
    for path in _modules():
        rel = _rel(path)
        if rel in EXEMPT_MODULES:
            continue
        module = ast.parse(path.read_text(), filename=str(path))
        owner = _enclosing_functions(module)
        bound = _bound_names(module)
        compared_visibility = _visibility_compare_operands(module)
        for node in ast.walk(module):
            if _profile_visibility_refs(node, bound):
                what = "Profile.visibility"
            elif node in compared_visibility:
                what = "a `.visibility` comparison"
            elif _profile_viewer_ref(node, bound):
                what = "ProfileViewer"
            else:
                continue
            enclosing = owner.get(node)
            if rel == EXEMPT_FUNCTION_MODULE and enclosing in EXEMPT_PROFILE_FUNCTIONS:
                continue
            where = f"{rel}::{enclosing}" if enclosing else f"{rel} (module level)"
            violations.append(f"{where} line {node.lineno}: {what}")
    assert not violations, (
        "The profile read rule has exactly one definition, "
        f"src/call_manager/{DEFINITION}. These references build a second one "
        "(a `.visibility` COMPARISON is one whatever it is compared to and "
        "whatever object it is read from -- reading the column to display it or "
        "writing it is not):\n  "
        + "\n  ".join(violations)
        + f"\nCall readable_profile_ids()/can_read_profile() instead. The only "
        f"exemptions are {sorted(EXEMPT_MODULES)} and, in "
        f"{EXEMPT_FUNCTION_MODULE}, the viewer-CRUD functions "
        f"{sorted(EXEMPT_PROFILE_FUNCTIONS)} -- which manage the rows and do "
        f"not decide access."
    )


def test_pin_set_has_not_shrunk() -> None:
    """Exact names, exact set -- literally, not just the right number of them.

    This docstring said "exact names, exact set" for a day while the body
    asserted `len(PREDICATE_PINS) == 11` and nothing else (outside review
    2026-09-04, Sol #12). Swapping one pinned surface for a different function
    that happens to call the predicate passed. A gate whose docstring claims
    more than its assertion is worse than no gate: it is the reason the next
    reader does not write the real one.


    The plan's own history is the reason: its first draft pinned seven surfaces
    and outside review found three more; the controller found an eleventh, and
    the final whole-branch review a twelfth (`create_workspace_api`, PD-9). A
    tuple that can quietly lose an entry is the same failure in reverse. 11 and
    1 are the counts MEASURED after that twelfth pin landed (2026-09-04); the
    11 was 10 until `web/api/workspaces.py::create_workspace_api` was added. A
    task that legitimately adds or removes a surface changes these numbers in
    the same diff, where a reviewer sees it.
    """
    assert set(PREDICATE_PINS) == EXPECTED_PREDICATE_PINS, {
        "unexpectedly added": sorted(set(PREDICATE_PINS) - EXPECTED_PREDICATE_PINS),
        "unexpectedly removed": sorted(EXPECTED_PREDICATE_PINS - set(PREDICATE_PINS)),
    }
    assert set(DELEGATION_PINS) == EXPECTED_DELEGATION_PINS, {
        "unexpectedly added": sorted(set(DELEGATION_PINS) - EXPECTED_DELEGATION_PINS),
        "unexpectedly removed": sorted(EXPECTED_DELEGATION_PINS - set(DELEGATION_PINS)),
    }
    # Length too, so a pin duplicated in the tuple cannot hide a deleted one.
    assert len(PREDICATE_PINS) == len(EXPECTED_PREDICATE_PINS), sorted(PREDICATE_PINS)


def test_exempt_set_is_documented_and_minimal() -> None:
    """Each exemption carries a reason, and none of them is stale.

    An exemption whose target no longer exists is how an exempt list rots into
    a wildcard. Every one of the four functions exists now (Task 3 created
    them), so each name must RESOLVE -- a reason-only check would leave a
    renamed or deleted function's exemption sitting in the list, silently
    covering whatever function later took its name.
    """
    for rel, reason in EXEMPT_MODULES.items():
        assert (SRC / rel).is_file(), f"exempt module no longer exists: {rel}"
        assert reason.strip(), rel
    assert (SRC / EXEMPT_FUNCTION_MODULE).is_file()
    defined = {
        node.name
        for node in _parse(EXEMPT_FUNCTION_MODULE).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    for name, reason in EXEMPT_PROFILE_FUNCTIONS.items():
        assert reason.strip(), name
        assert name in defined, (
            f"exempt function no longer exists: {EXEMPT_FUNCTION_MODULE}::{name}. "
            f"An exemption for a function nobody can find is a wildcard waiting "
            f"for a future function to be given that name; delete it, or fix the "
            f"name if the function was renamed. Top-level functions there: "
            f"{sorted(defined)}"
        )
