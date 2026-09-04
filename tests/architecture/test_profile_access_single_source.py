"""`readable_profile_ids()` must stay the ONLY definition of who may read a profile.

The whole "profiles are personal" plan reduces to one property: every surface
calls the predicate, and nothing reimplements it. That property is not local to
any one file, so no unit test can hold it -- which is why it lives here.

The failure mode is silent and partial. Ten of the eleven surfaces get the rule
right, the eleventh keeps its own is_active-only lookup, and a private profile
leaks through exactly one route. It is not hypothetical: the first draft of this
plan pinned SEVEN surfaces, an outside review found three more, and the
controller found an eleventh (`calls.py::get_call_detail` resolves `?profile=`
with its own lookup, at calls.py:273-277).

TWO HALVES, and each covers a hole the other cannot see:

  POSITIVE -- each pinned function contains a CALL to the predicate. Deliberately
  a call and not an import: an import survives the removal of its call, so an
  import-counting gate stays green while `assemble.py` keeps `readable_profile_ids`
  in its import block and quietly drops the `.where(...)` that used it. And the
  pin is an exact SET, not a floor -- "at least six" over a six-member set
  tolerates one being dropped the day a seventh is added.

  NEGATIVE -- outside a named exempt set, no module may reference
  `Profile.visibility` or `ProfileViewer`. That is what stops a surface from
  being "wired" with its own second copy of the arms, which would satisfy the
  positive half and still drift.

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
)

# Pinned on DELEGATION rather than on the predicate. `set_default` is a
# four-line wrapper whose entire job is to call `dashboard.set_default_profile`
# (which is itself pinned above). Requiring a predicate call here too would
# force a redundant second check into a delegate -- the exact duplication this
# file exists to prevent. What must not happen is the delegate growing its own
# body, so what is pinned is that it still delegates.
DELEGATION_PINS: dict[str, str] = {
    "web/api/me.py::set_default": "set_default_profile",
}

# Wired by later tasks; see the module docstring of the plan. Marked
# xfail(strict=True), NOT skipped and NOT exempted: the moment Task 3/4/5 wires
# a function the case XPASSes, strict turns that into a FAILURE, and the task
# cannot go green without deleting its own line here. A self-removing scaffold.
# THIS DICT MUST BE EMPTY (and deleted) when Task 5 lands.
_NOT_YET_WIRED: dict[str, str] = {
    "web/dashboard.py::resolve_profile_selection": "Task 4",
    "web/dashboard.py::set_default_profile": "Task 4",
    "web/dashboard.py::build_dashboard_view": "Task 4",
    "web/api/calls.py::get_call_detail": "Task 4",
    "web/api/me.py::get_settings": "Task 5",
    "web/api/me.py::update_subscriptions": "Task 5",
    "digest/assemble.py::assemble_for_user": "Task 5",
}

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


def _pin_params() -> list[pytest.param]:
    params = []
    for qualname in PREDICATE_PINS:
        marks = []
        task = _NOT_YET_WIRED.get(qualname)
        if task is not None:
            marks.append(pytest.mark.xfail(strict=True, reason=f"wired in {task}"))
        params.append(pytest.param(qualname, id=qualname, marks=marks))
    return params


@pytest.mark.parametrize(
    "qualname",
    [pytest.param(q, id=q) for q in (*PREDICATE_PINS, *DELEGATION_PINS)],
)
def test_pinned_function_still_exists(qualname: str) -> None:
    """Every pin resolves to a real top-level function.

    Separate from the call assertion ON PURPOSE. The call assertions carry
    xfail markers while Tasks 3-5 are outstanding, and a missing function
    inside an xfail'd case reads as an expected failure -- a rename would hide
    behind the scaffold. This test is never xfail'd, so a rename is loud today.
    """
    _resolve(qualname)


@pytest.mark.parametrize("qualname", _pin_params())
def test_pinned_surface_calls_the_single_source(qualname: str) -> None:
    fn = _resolve(qualname)
    called = _body_calls(fn)
    assert called & set(PREDICATE_NAMES), (
        f"{qualname} does not CALL {' or '.join(PREDICATE_NAMES)}. Every surface "
        f"that resolves a profile for a user must go through "
        f"src/call_manager/{DEFINITION}; an import alone is not enough."
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
        for node in ast.walk(module):
            if _profile_visibility_refs(node, bound):
                what = "Profile.visibility"
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
        f"src/call_manager/{DEFINITION}. These references build a second one:\n  "
        + "\n  ".join(violations)
        + f"\nCall readable_profile_ids()/can_read_profile() instead. The only "
        f"exemptions are {sorted(EXEMPT_MODULES)} and, in "
        f"{EXEMPT_FUNCTION_MODULE}, the viewer-CRUD functions "
        f"{sorted(EXEMPT_PROFILE_FUNCTIONS)} -- which manage the rows and do "
        f"not decide access."
    )


def test_pin_set_has_not_shrunk() -> None:
    """Exact names, exact set -- and an exact COUNT, so deleting a pin is red.

    The plan's own history is the reason: its first draft pinned seven surfaces
    and outside review found three more; the controller found an eleventh. A
    tuple that can quietly lose an entry is the same failure in reverse. 10 and
    1 are the counts MEASURED when this gate was written (2026-09-04); a task
    that legitimately adds or removes a surface changes these numbers in the
    same diff, where a reviewer sees it.
    """
    assert len(PREDICATE_PINS) == 10, sorted(PREDICATE_PINS)
    assert len(DELEGATION_PINS) == 1, sorted(DELEGATION_PINS)


def test_not_yet_wired_names_are_real_pins() -> None:
    """The scaffold can only cover pins that exist.

    A typo'd key applies no marker, so the case is simply red today -- loud, and
    fail-safe. The opposite, a leftover key for a pin that has been renamed
    away, is silent: it would sit here forever looking like an exemption for
    something. So the dict is checked against the pin list, and it must be empty
    (and deleted, with this test) once Task 5 lands.
    """
    unknown = sorted(set(_NOT_YET_WIRED) - set(PREDICATE_PINS))
    assert not unknown, f"_NOT_YET_WIRED names something that is not a pin: {unknown}"


def test_exempt_set_is_documented_and_minimal() -> None:
    """Each exemption carries a reason, and none of them is stale.

    An exemption whose target no longer exists is how an exempt list rots into
    a wildcard. The profiles.py functions are Task 3's, so they are allowed to
    be absent today -- the module is not.
    """
    for rel, reason in EXEMPT_MODULES.items():
        assert (SRC / rel).is_file(), f"exempt module no longer exists: {rel}"
        assert reason.strip(), rel
    assert (SRC / EXEMPT_FUNCTION_MODULE).is_file()
    for name, reason in EXEMPT_PROFILE_FUNCTIONS.items():
        assert reason.strip(), name
