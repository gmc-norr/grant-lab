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
            elif (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Name)
                and node.value.id in names
            ):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        for node in ast.walk(tree):
            # Its own `def` is not a caller. Excluding the whole of fencing.py would
            # hide a real caller added inside it, so exclude the DEFINITION, by AST.
            if isinstance(node, ast.FunctionDef) and node.name == _PRIMITIVE_B:
                continue
            if isinstance(node, ast.Call) and _callee_name(node.func) in names:
                hits.append(f"{path.relative_to(_ROOT)}:{node.lineno}")
    assert hits == [], f"Primitive B gained a caller: {hits}"


def test_the_real_tree_still_has_exactly_nine_sink_holders():
    """Two-sided: §4.2 WIDENS discovery, so this must not grow either."""
    # Identity is (path, qualname), NOT qualname alone: two files can both define
    # `_git` or `checkpoint_artifact`, and counting bare qualnames would collapse
    # them — hiding an ADDED site behind an existing name.
    holders = set()
    for path in (_ROOT / "src").rglob("*.py"):
        rel = str(path.relative_to(_ROOT))
        holders |= {(rel, q) for q, _n, _i in sink_calls(path.read_text(), rel)}
    assert len(holders) == 9, sorted(holders)


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
