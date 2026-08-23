"""F3e1a2 confirmation: the checker got smarter and the measurement did not move.

The slice's central claim is INERTNESS. `acquire_commit_mutex_at_generation` has zero
production callers, so every git row fails the positive rule before position is ever
consulted — whatever the allowlist does. If any assertion here fails, the slice
changed a measurement it promised not to touch.
"""

from __future__ import annotations

import ast
import pathlib

from tests.fencing.git_fence import _callee_name, fenced_qualnames, sink_calls
from tests.fencing.inventory import SLICE_STATUS, load_inventory

_PRIMITIVE_B = "acquire_commit_mutex_at_generation"

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_primitive_b_still_has_zero_production_callers():
    """The premise of the whole inertness argument. If this ever goes red, the slice
    is no longer inert and every other assertion here means something different."""
    hits = []
    for path in (_ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text())
        # Resolve aliases FIRST. `from ... import acquire_commit_mutex_at_generation
        # as acquire` then `acquire(...)` is a caller, and a bare-name check misses it
        # — the same alias handling F3e1a's own identity test already does.
        names = {_PRIMITIVE_B}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if a.name == _PRIMITIVE_B and a.asname:
                        names.add(a.asname)
            # `_callee_name`, not `isinstance(..., ast.Name)`: it resolves the final
            # name of an Attribute too, so `alias = mod.acquire_commit_mutex_at_...`
            # binds an alias just as `alias = acquire_commit_mutex_at_...` does. A
            # bare-Name check saw only the second form.
            elif isinstance(node, ast.Assign) and _callee_name(node.value) in names:
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        for node in ast.walk(tree):
            # Its own `def` is not a caller. Excluding the whole of fencing.py would
            # hide a real caller added inside it, so exclude the DEFINITION, by AST.
            if isinstance(node, ast.FunctionDef) and node.name == _PRIMITIVE_B:
                continue
            if isinstance(node, ast.Call) and _callee_name(node.func) in names:
                hits.append(f"{path.relative_to(_ROOT)}:{node.lineno}")
    assert hits == [], f"Primitive B gained a caller: {hits}"


#: The nine production sink holders, by (path, qualname). Fixed, not derived: a
#: count alone cannot tell "site removed + site added" from "nothing changed", and
#: a SET of holders additionally collapses two calls inside one function to one
#: entry. Both are how an added site hides. Identity plus a call count closes it.
_NINE_HOLDERS = {
    ("src/call_manager/web/api/workspaces.py", "post_init_spec._git"),
    ("src/call_manager/proposal_assist/citations/core.py", "_insert_citation_locked"),
    ("src/call_manager/proposal_assist/citations/bibliography.py", "build_bibliography"),
    ("src/call_manager/proposal_assist/citations/human_cite.py", "insert_citation_for_user"),
    ("src/call_manager/proposal_assist/phases/setup.py", "Phase0Worker._init_repo"),
    ("src/call_manager/proposal_assist/editing/save.py", "save_section"),
    ("src/call_manager/proposal_assist/collab/checkpoint.py", "checkpoint_artifact"),
    ("src/call_manager/proposal_assist/review/pass_runner.py", "run_review_pass"),
    ("src/call_manager/proposal_assist/analysis/insert.py", "insert_from_annotation"),
}


def _real_sink_calls():
    """Every sink call in `src/`, as (path, qualname, stmt_index). NOT deduped —
    the call count is half of what `test_the_real_tree_still_has_exactly_nine...`
    asserts."""
    out = []
    for path in (_ROOT / "src").rglob("*.py"):
        rel = str(path.relative_to(_ROOT))
        out += [(rel, q, i) for q, _n, i in sink_calls(path.read_text(), rel)]
    return out


def test_the_real_tree_still_has_exactly_nine_sink_holders():
    """Two-sided: §4.2 WIDENS discovery, so this must not grow either."""
    calls = _real_sink_calls()
    # Identity, not just count. `len(holders) == 9` survives a swap — one holder
    # lost and one gained — and survives two calls collapsing into one holder.
    assert {(rel, q) for rel, q, _i in calls} == _NINE_HOLDERS
    assert len(calls) == 9, sorted((rel, q) for rel, q, _i in calls)


def test_every_real_sink_gets_a_credited_position():
    """The slice's REASON TO EXIST, as a gate rather than a hope.

    F3e1a credited only a bare top-level `ast.Expr`, and not one of the nine is
    written that way — so every one measured `stmt_index=None` and no slice owning
    a git row could ever reach `complete`. The six-entry allowlist (§4.1) was
    derived from exactly these nine paths.

    Without this test the allowlist could go back to crediting nothing and every
    other assertion in this file would still pass, because they all key off
    Primitive B having zero callers. `None` here is the old bug returning.
    """
    uncredited = [(rel, q) for rel, q, i in _real_sink_calls() if i is None]
    assert uncredited == [], f"the allowlist stopped crediting real sinks: {uncredited}"


def test_no_real_site_measures_fenced():
    fenced = set()
    for path in (_ROOT / "src").rglob("*.py"):
        rel = str(path.relative_to(_ROOT))
        fenced |= fenced_qualnames(path.read_text(), rel)
    assert fenced == set(), f"a site measured fenced with zero Primitive B callers: {fenced}"


def test_every_git_row_is_still_unfenced_and_classified():
    rows = [r for r in load_inventory() if r["kind"] == "git_commit"]
    assert len(rows) == 9
    assert all(r["fenced"] is False for r in rows)
    assert all(SLICE_STATUS[r["slice"]] == "pending" for r in rows)


def test_this_slice_is_recorded_complete():
    """F3e1a2 owns ZERO inventory rows, so the row-iterating test above never
    reads its status — deleting the key entirely left the whole fencing suite
    green (measured: 187 passed either way). A zero-row slice needs its status
    asserted directly or the bookkeeping is decorative."""
    assert SLICE_STATUS["F3e1a2"] == "complete"


def test_the_allowlist_is_still_six_entries():
    """§4.1: the smallness IS the safety property. Growing this table needs a spec
    amendment and a real site that requires the entry — not a passing thought that a
    construct is eager."""
    from tests.fencing.git_fence import CREDITED_FIELDS

    assert {(k.__name__, tuple(sorted(v))) for k, v in CREDITED_FIELDS.items()} == {
        ("Assign", ("value",)),
        ("Expr", ("value",)),
        ("If", ("body",)),
        ("Try", ("body",)),
        ("With", ("body",)),
        ("AsyncWith", ("body",)),
    }


# NO GIT-DEPENDENT TESTS HERE. Two earlier drafts asserted `git diff` and
# `git show` against `merge-base master HEAD`. Both are wrong as permanent tests:
# once this merges, base == HEAD and they assert nothing; and CI's shallow
# checkout may have no local `master` ref at all, so they ERROR rather than fail.
# Those two checks are one-time PLAN commands against the fixed base `ea481e2c` —
# see Step 4 — not pytest tests.
