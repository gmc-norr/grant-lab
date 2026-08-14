"""Slice C2's bound-through-COMMIT rule, enforced STRUCTURALLY. DB-free, milliseconds.

The rule (spec section 8, gate in section 12)
---------------------------------------------
Each of the six mutex-held owner writers wraps its post-mutex work in
`row_locks.bounded_lock_waits`, and that `with` block must ENCLOSE the
transaction's `session.commit()` — not merely the row acquisitions.

TRANSITIONAL, for the duration of the workspace_write consolidation
(docs/superpowers/specs/2026-08-14-workspace-write-consolidation-design.md §5).
Each route must be exactly one of two shapes — legacy `bounded_lock_waits` with
its commit inside, or migrated `workspace_write` with no route-level commit —
because the routes migrate one commit at a time and this gate has to stay green
throughout. The legacy arm is deleted in the slice's final commit, after which
re-introducing a hand-rolled bound-plus-commit in these two modules fails.

The reason is exact. `bounded_lock_waits` issues `SET LOCAL lock_timeout` and
deliberately leaves it in force when the `with` exits (see `row_locks.py`'s
module docstring: the bound "must extend past the acquisitions to the commit"),
but its `55P03 -> RowLockTimeout` translation only covers its own body. A wrap
that stops short of the commit therefore leaves the flush at COMMIT *bounded yet
UNtranslated*: a contended row makes Postgres raise 55P03 there, it escapes as a
raw `sqlalchemy.exc.OperationalError`, and the caller gets a 500 where this
slice contracts for 429 + `Retry-After: 2`. That defect was found and fixed once
already on this branch (4e173d8).

Why this is a static check and not another interleaving test
-----------------------------------------------------------
It was a runtime test, twice: `test_activate_maps_a_post_job_lock_row_timeout_to_429_not_500`
and the `hold_workspace_row` family in
`tests/proposal_assist/test_api_writers_mutex_straddle.py` park a holder on the
`workspaces` row, let the route win the mutex and block, and demand 429 rather
than 500.

Those gates went dead for `update_rubric` and `activate` — silently — when the
stale-read fix (8817c33) added `session.refresh(ws, with_for_update=True)` as
the FIRST statement inside the bound. The route now takes the `workspaces` row
EARLY, so a holder's 55P03 fires at the refresh, which is inside any plausible
narrow wrap, instead of at COMMIT. Dedenting `session.commit()` out of the
`with` in either rubric route left all fifty tests in those two files green.
The same dedent still fails loudly on `settings.py` and on the pre-fix parent
commit, which is what confirms the loss was specific to the refresh.

(That runtime hole has since been closed for those two routes as well, by the
`hold_user_row` pair described in the next section — a different holder, not a
different mechanism. This gate is not redundant with them: they cover the two
routes whose commit-time wait is a `users` FK, whereas this one covers all six
regardless of which row any of them happens to contend for.)

That is the honest argument for a static check: the rule IS structural — "this
statement is lexically inside that block" — so an AST assertion is its direct
encoding rather than a proxy for it, and it survives exactly the change that
disarmed the runtime gates, because it does not care WHERE contention happens to
fire.

Is the runtime hazard still real for the two rubric routes?
-----------------------------------------------------------
Yes, though narrowed. Holding the `workspaces` row from the refresh through
COMMIT does remove the DESIGNED slow holder — the four pass prefixes, which take
that row `FOR UPDATE` holding no mutex and keep it across context assembly — from
those two routes' commit-time waits. What remains is the `WorkspaceEvent` insert
that `_apply_rubric` (and `activate`'s `workspace_activated`) flushes at COMMIT:
its `actor_id` FK takes `FOR KEY SHARE` on a `users` row, and `FOR KEY SHARE`
conflicts with `FOR UPDATE`, which `admin_users.py`'s `_lock_active_admin_count`
takes over every active admin row while it counts. Measured, and committed as a
test rather than left as an assertion: with that `users` row held and the commit
dedented out of the bound, `PUT /api/workspaces/{id}/rubric` raises
`OperationalError (psycopg.errors.LockNotAvailable)` naming
`while locking tuple in relation "users"` — a 500; with the commit inside the
bound it answers 429. The interleaving is
`test_update_rubric_maps_a_commit_time_users_fk_wait_to_429_not_500` and
`test_activate_maps_a_commit_time_users_fk_wait_to_429_not_500` in
`tests/proposal_assist/test_api_writers_mutex_straddle.py`. So the wrap is still
load-bearing for these two routes — against a short holder rather than a slow
one — and fully load-bearing for the three `settings.py` routes, whose entire
write reaches a contended row only at COMMIT.

`retry_setup` is the exception in the other direction, and it is why this file
exists. It takes its `WorkspaceSetupJob` row `.with_for_update()` at the TOP of
its bound and writes no `WorkspaceEvent`, so its COMMIT has no FK check and no
reachable contention: no runtime interleaving can distinguish the commit being
inside the bound from outside it. Measured — dedenting its `session.commit()`
out of the bound leaves every runtime gate green and fails only this file's
`[rubric-retry_setup]` case. For that route this static check is not a second
opinion, it is the only enforcement there is.

Placement
---------
Beside `test_row_lock_ceilings.py` rather than inside it. Both are DB-free
import-only guards over the same slice, but that file's subject is the numeric
lock-timeout CEILINGS and the arithmetic that derives them; this file's subject
is the lexical SHAPE of two route modules. They fail for unrelated reasons and
their docstrings are the main thing a future reader takes from either, so a
merged file would force two arguments into one preamble.

No regex, no `in` over source text
----------------------------------
This repo has already shipped a source-text assertion that was a tautology
(`src.index(f"def {name}(")` matches inside `"async def ..."`), so the matching
below is done on parsed AST nodes only.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from call_manager.web.api import rubric as rubric_api
from call_manager.web.api import settings as settings_api

# The six routes, named explicitly and keyed by the module that holds them.
# Deliberately NOT derived from the modules: a list read out of the source would
# keep passing after a route was deleted or renamed, covering five and reporting
# six. `test_the_six_bounded_writers_are_all_present` asserts this set against
# what those two modules actually contain, in both directions, so a rename fails
# here and a NEW bounded writer added TO ONE OF THESE TWO MODULES without a line
# in this table fails too.
#
# The table is also the gate's SCOPE, which is narrower than that sentence alone
# suggests: the parametrize iterates these keys, so these two modules are all the
# check ever reads. A `bounded_lock_waits` block added anywhere else in the tree —
# `web/api/workspaces.py`, say — is invisible to it and may put its commit outside
# the bound with this file still green. That is measured, not assumed. The limit
# is deliberate: §8's bound rule is normative for these six routes and explicitly
# not a property of the codebase (§10.8 records the wider gap, whose root cause is
# `acquire_commit_mutex`'s restore-to-DEFAULT at every call site). If the rule is
# ever extended to another module, extending this gate means adding that module
# as a key here — not widening the walk.
EXPECTED_BOUNDED_WRITERS: dict[str, frozenset[str]] = {
    "call_manager.web.api.settings": frozenset(
        {"update_settings", "add_member", "remove_member"}
    ),
    "call_manager.web.api.rubric": frozenset({"update_rubric", "activate", "retry_setup"}),
}

_MODULES = {
    "call_manager.web.api.settings": settings_api,
    "call_manager.web.api.rubric": rubric_api,
}


def _parse(module_name: str) -> ast.Module:
    """Parse the module's REAL source, located through the imported module object
    so this file cannot drift from a moved or renamed source path."""
    path = Path(_MODULES[module_name].__file__)
    return ast.parse(path.read_text(), filename=str(path))


def _module_level_functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    # Module body only: a nested helper must not shadow a route of the same name.
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _calls(item: ast.withitem, name: str) -> bool:
    call = item.context_expr
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    # Matches both `name(...)` and `module.name(...)`.
    if isinstance(func, ast.Name):
        return func.id == name
    if isinstance(func, ast.Attribute):
        return func.attr == name
    return False


def _blocks_calling(fn: ast.AST, name: str) -> list[ast.With | ast.AsyncWith]:
    return [
        node
        for node in ast.walk(fn)
        if isinstance(node, (ast.With, ast.AsyncWith))
        and any(_calls(item, name) for item in node.items)
    ]


def _bound_blocks(fn: ast.AST) -> list[ast.With | ast.AsyncWith]:
    return _blocks_calling(fn, "bounded_lock_waits")


def _managed_blocks(fn: ast.AST) -> list[ast.With | ast.AsyncWith]:
    return _blocks_calling(fn, "workspace_write")


def _session_commit_calls(node: ast.AST) -> list[ast.Call]:
    """Every `session.commit()` call node in this subtree.

    Matched on the AST: a Call whose func is an Attribute named `commit` on a
    Name `session` — the one spelling all six routes use.
    """
    return [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "commit"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "session"
    ]


def _discovered_bounded_writers(module_name: str) -> set[str]:
    """Routes taking EITHER shape: a legacy `bounded_lock_waits` bound, or a
    migrated `workspace_write` scope. During the migration a module holds a mix,
    and a route that has neither must still be reported as missing."""
    return {
        name
        for name, fn in _module_level_functions(_parse(module_name)).items()
        if _bound_blocks(fn) or _managed_blocks(fn)
    }


@pytest.mark.parametrize("module_name", sorted(EXPECTED_BOUNDED_WRITERS))
def test_the_six_bounded_writers_are_all_present(module_name: str):
    """The named routes still exist and still take a bound — and nothing else in
    the module took one without being named here.

    Without this, deleting or renaming a route would make the per-route gate
    below quietly cover five of six.
    """
    expected = EXPECTED_BOUNDED_WRITERS[module_name]
    discovered = _discovered_bounded_writers(module_name)
    missing = sorted(expected - discovered)
    unlisted = sorted(discovered - expected)
    assert not missing, (
        f"{module_name}: no module-level function named {missing} wraps its work in "
        f"bounded_lock_waits or workspace_write any more. If the route was renamed or moved, update "
        f"EXPECTED_BOUNDED_WRITERS in this file so it stays covered; if its bound was "
        f"removed, that is Slice C2's bound-through-COMMIT rule being dropped — see "
        f"this file's docstring."
    )
    assert not unlisted, (
        f"{module_name}: {unlisted} take a bounded_lock_waits or workspace_write bound but are not listed "
        f"in EXPECTED_BOUNDED_WRITERS. A new mutex-held writer is subject to the same "
        f"bound-through-COMMIT rule — add it to the table so it is checked."
    )


@pytest.mark.parametrize(
    ("module_name", "route"),
    sorted(
        (module_name, route)
        for module_name, routes in EXPECTED_BOUNDED_WRITERS.items()
        for route in routes
    ),
)
def test_each_route_is_exactly_one_of_legacy_or_migrated(module_name: str, route: str):
    """TRANSITIONAL (see this file's docstring). Each route must be exactly one of:

      legacy:   one `with bounded_lock_waits(...)` block with `session.commit()`
                lexically inside it and no commit outside it;
      migrated: one `with workspace_write(...)` block and NO `session.commit()`
                anywhere in the route.

    Both arms enforce the same rule — the transaction is committed inside the
    bound — the migrated arm by making the alternative unwriteable. Anything
    else fails: a half-migrated route holding both constructs, a route with
    neither, or a migrated route that kept a stray commit.

    The legacy arm is deleted once the last route migrates.
    """
    fn = _module_level_functions(_parse(module_name)).get(route)
    assert fn is not None, (
        f"{module_name}.{route} is not a module-level function any more — neither "
        f"arm of this rule can be checked for it. See "
        f"test_the_six_bounded_writers_are_all_present."
    )

    legacy = _bound_blocks(fn)
    migrated = _managed_blocks(fn)
    commits = _session_commit_calls(fn)

    assert not (legacy and migrated), (
        f"{module_name}.{route} has BOTH a `with bounded_lock_waits(...)` block and a "
        f"`with workspace_write(...)` block. A half-migrated route is neither shape: "
        f"finish the migration (drop the legacy bound, drop the commit) or revert it."
    )
    assert legacy or migrated, (
        f"{module_name}.{route} has neither a `with bounded_lock_waits(...)` block nor "
        f"a `with workspace_write(...)` block, so nothing bounds its lock waits through "
        f"COMMIT. A contended row makes it answer 500 with a raw OperationalError "
        f"instead of the contracted 429 + Retry-After: 2."
    )

    if migrated:
        assert len(migrated) == 1, (
            f"{module_name}.{route} has {len(migrated)} `with workspace_write(...)` "
            f"blocks; this gate is written for exactly one write scope per route."
        )
        assert not commits, (
            f"{module_name}.{route} uses `workspace_write` but still calls "
            f"session.commit() at line(s) {[c.lineno for c in commits]}. The manager "
            f"owns the commit — a route-level commit either double-commits or commits "
            f"outside the managed scope. Delete it."
        )
        return

    assert len(legacy) == 1, (
        f"{module_name}.{route} has {len(legacy)} `with bounded_lock_waits(...)` blocks; "
        f"this gate is written for exactly one bounded region per route. If the route "
        f"genuinely needs two, teach this check which commit belongs to which."
    )
    bound = legacy[0]
    inside = {id(c) for c in _session_commit_calls(bound)}
    outside = [c for c in commits if id(c) not in inside]
    assert inside, (
        f"{module_name}.{route}: session.commit() is NOT inside its "
        f"`with bounded_lock_waits(...)` block (bound at line {bound.lineno}"
        + (f", commit at line {outside[0].lineno}" if outside else "")
        + "). bounded_lock_waits leaves SET LOCAL lock_timeout in force when the "
        f"`with` exits but translates 55P03 -> RowLockTimeout only INSIDE its body, so "
        f"a commit outside the bound is still bounded yet UNtranslated: a contended row "
        f"makes {route} answer 500 with a raw OperationalError instead of the "
        f"contracted 429 + Retry-After: 2. Move the commit back inside the `with`."
    )
    assert not outside, (
        f"{module_name}.{route} calls session.commit() at line(s) "
        f"{[c.lineno for c in outside]}, outside its `with bounded_lock_waits(...)` "
        f"block. Every commit in this route must sit inside the bound."
    )
