"""Fence F2 — merge/rename fixtures. All synthetic.

A one-time manual check that unchanged identities survive proves almost nothing;
the dangerous case is the identity that CHANGES.
"""

from __future__ import annotations

import pytest

from tests.fencing.detectors import KIND_TOOL_COMMIT, Site
from tests.fencing.inventory import (
    BadRenameMap,
    FencedSiteVanished,
    load_inventory,
    merge,
    scan_tree,
)

P = "src/call_manager/proposal_assist/tools/a.py"


def _row(symbol, *, fenced=False, category="semantic", **extra):
    return {
        "kind": KIND_TOOL_COMMIT,
        "path": P,
        "symbol": symbol,
        "category": category,
        "fenced": fenced,
        "slice": "F3",
        **extra,
    }


def _site(symbol):
    return Site(KIND_TOOL_COMMIT, P, symbol)


def test_a_new_site_arrives_unclassified_and_unfenced():
    out = merge([], {_site("alpha")}, None, set())
    assert out[0]["category"] == "UNCLASSIFIED"
    assert out[0]["fenced"] is False


def test_an_unchanged_site_keeps_flag_and_category():
    # F3d2: tool_commit fenced flag is measured, not stored. Pass it in fenced_ids
    # to get fenced=True in output.
    out = merge([_row("alpha", fenced=True)], {_site("alpha")}, None, {(P, "alpha")})
    assert out[0]["fenced"] is True
    assert out[0]["category"] == "semantic"


def test_declared_extra_fields_SURVIVE_regeneration():
    """fenced_by and ctx_lifetime are declarations the verifier depends on. A
    merge that rebuilds rows from a fixed key list drops them silently."""
    prev = [_row("alpha", category="caller_fenced", fenced_by="outer", ctx_lifetime="retained")]
    out = merge(prev, {_site("alpha")}, None, set())
    assert out[0]["fenced_by"] == "outer"
    assert out[0]["ctx_lifetime"] == "retained"


def test_dropping_an_UNFENCED_site_is_allowed_silently():
    assert merge([_row("alpha", fenced=False)], set(), None, set()) == []


def test_dropping_a_FENCED_site_ABORTS():
    """THE DEFECT. Rename a fenced function and its proof silently vanishes;
    after F3 removes test_f2_fences_nothing, CI stays green."""
    with pytest.raises(FencedSiteVanished) as ei:
        merge([_row("alpha", fenced=True)], set(), None, set())
    assert (KIND_TOOL_COMMIT, P, "alpha") in ei.value.lost


def test_a_rename_carries_the_flag_and_the_extra_fields_across():
    prev = [_row("alpha", fenced=True, category="caller_fenced", fenced_by="outer")]
    renames = {(KIND_TOOL_COMMIT, P, "alpha"): (KIND_TOOL_COMMIT, P, "beta")}
    # F3d2: tool_commit fenced flag is measured from fenced_ids. Pass the renamed
    # identity to preserve its fenced status.
    out = merge(prev, {_site("beta")}, renames, {(P, "beta")})
    assert len(out) == 1
    assert out[0]["symbol"] == "beta"
    assert out[0]["fenced"] is True
    assert out[0]["fenced_by"] == "outer"


def test_a_rename_whose_TARGET_is_absent_is_a_typo():
    renames = {(KIND_TOOL_COMMIT, P, "alpha"): (KIND_TOOL_COMMIT, P, "typo")}
    with pytest.raises(BadRenameMap):
        merge([_row("alpha", fenced=True)], {_site("beta")}, renames, set())


def test_a_rename_whose_OLD_identity_is_unknown_is_a_typo():
    """Silently ignoring it lets a human believe a rename was carried across."""
    renames = {(KIND_TOOL_COMMIT, P, "ghost"): (KIND_TOOL_COMMIT, P, "beta")}
    with pytest.raises(BadRenameMap):
        merge([_row("beta")], {_site("beta")}, renames, set())


def test_output_is_sorted_for_a_stable_diff():
    out = merge([], {_site("zeta"), _site("alpha")}, None, set())
    assert [r["symbol"] for r in out] == ["alpha", "zeta"]


def test_regeneration_preserves_every_human_slice_assignment():
    """merge() rebuilt `slice` from the kind defaults, so a regeneration would
    silently revert F3a's claims to F4, setup to F5's row to F4, the contexts to
    F3d and ingest to F3d — with every other gate still green."""
    before = {(r["kind"], r["path"], r["symbol"]): r["slice"] for r in load_inventory()}
    sites, fenced_ids = scan_tree()
    merged = merge(load_inventory(), sites, renames={}, fenced_ids=fenced_ids)
    after = {(r["kind"], r["path"], r["symbol"]): r["slice"] for r in merged}
    assert after == before, "regeneration changed a human slice assignment"
    assert len(after) == 20


def test_a_rename_can_move_a_row_into_the_relocated_scope():
    """The mechanism F3d3a's move depends on. Before Task 1, merge() rejected any
    rename whose target lived outside tools/ — `target not found in the scan` —
    because find_sites never recorded one. That made RENAMES useless for the only
    move this slice makes."""
    from tests.fencing.detectors import KIND_TOOL_COMMIT, Site
    from tests.fencing.inventory import merge

    old = (
        "tool_commit",
        "src/call_manager/proposal_assist/tools/citations.py",
        "_write_citekey_cache",
    )
    new = (
        "tool_commit",
        "src/call_manager/proposal_assist/citations/core.py",
        "_write_citekey_cache",
    )
    previous = [
        {
            "kind": old[0], "path": old[1], "symbol": old[2],
            "category": "caller_fenced", "fenced": False, "slice": "F3d3",
            "fenced_by": "_insert_citation_locked",
        }
    ]
    found = {Site(KIND_TOOL_COMMIT, new[1], new[2])}

    rows = merge(previous, found, {old: new}, set())

    assert len(rows) == 1
    assert rows[0]["path"] == new[1]
    # The human-owned fields ride along — that is the point of a rename.
    assert rows[0]["category"] == "caller_fenced"
    assert rows[0]["slice"] == "F3d3"
    assert rows[0]["fenced_by"] == "_insert_citation_locked"


def test_a_rename_into_an_undeclared_path_is_still_refused():
    """The limit. A rename is not a way to park a row anywhere — the target has to
    be somewhere the scan actually looks, which is tools/ plus the declared
    relocated set. Otherwise a row could be 'carried' to a path nothing ever
    re-checks, and the identity would be preserved in name only."""
    import pytest

    from tests.fencing.inventory import BadRenameMap, merge

    old = (
        "tool_commit",
        "src/call_manager/proposal_assist/tools/citations.py",
        "_write_citekey_cache",
    )
    new = (
        "tool_commit",
        "src/call_manager/proposal_assist/editing/save.py",
        "_write_citekey_cache",
    )
    previous = [
        {
            "kind": old[0], "path": old[1], "symbol": old[2],
            "category": "caller_fenced", "fenced": False, "slice": "F3d3",
            "fenced_by": "_insert_citation_locked",
        }
    ]
    # The scan did not find it there, because editing/save.py is not in scope.
    with pytest.raises(BadRenameMap, match="target not found in the scan"):
        merge(previous, set(), {old: new}, set())
