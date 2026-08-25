"""F3e1a2 confirmation: the checker got smarter and the measurement did not move.

F3e1a2's central claim was INERTNESS — `acquire_commit_mutex_at_generation` had zero
production callers, so every git row failed the positive rule before position was ever
consulted. **That premise is over.** The 2026-08-24 endgame converts those callers, so
the three assertions that restated it — Primitive B has no callers, no real site
measures fenced, every git row is unfenced — are RETIRED here rather than weakened.
Deleting them loses nothing: each was a statement about the tree's conversion state,
and that state is now measured per row by `test_the_inventory_git_flags_match_the_tree`
and `test_each_slice_matches_its_declared_status` in test_fence_coverage.py.

What remains is what F3e1a2 actually built and what still has to hold while the tree
converts: the nine sink holders are still nine and still those nine, every one of them
still gets a credited position, the allowlist is still six entries, and the slice's own
status is still recorded.
"""

from __future__ import annotations

import pathlib

from tests.fencing.git_fence import sink_calls
from tests.fencing.inventory import SLICE_STATUS, load_inventory

_ROOT = pathlib.Path(__file__).resolve().parents[2]


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

    Without this test the allowlist could go back to crediting nothing and nothing
    else in this file would notice — none of the remaining assertions reads a
    position. `None` here is the old bug returning, and since the endgame started
    converting these sites it would also silently un-fence every converted row.
    """
    uncredited = [(rel, q) for rel, q, i in _real_sink_calls() if i is None]
    assert uncredited == [], f"the allowlist stopped crediting real sinks: {uncredited}"


def test_every_git_row_is_still_counted_and_allocated():
    """Was `test_every_git_row_is_still_unfenced_and_classified`.

    Its `fenced is False` and `slice is pending` clauses asserted that the tree was
    UNCONVERTED — F3e1a2's inertness premise, deliberately ended by the 2026-08-24
    endgame. What is left is the part that is not about inertness: the nine rows
    still exist and every one names a slice the bookkeeping knows. The retired
    clauses' real obligation — a row's `fenced` must match the source, and a
    `pending` slice must have no fenced row — is enforced per row by
    `test_the_inventory_git_flags_match_the_tree` and
    `test_each_slice_matches_its_declared_status`.
    """
    rows = [r for r in load_inventory() if r["kind"] == "git_commit"]
    assert len(rows) == 9
    assert all(SLICE_STATUS[r["slice"]] in {"pending", "complete"} for r in rows)


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
