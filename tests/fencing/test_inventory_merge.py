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
    out = merge([], {_site("alpha")}, None)
    assert out[0]["category"] == "UNCLASSIFIED"
    assert out[0]["fenced"] is False


def test_an_unchanged_site_keeps_flag_and_category():
    out = merge([_row("alpha", fenced=True)], {_site("alpha")}, None)
    assert out[0]["fenced"] is True
    assert out[0]["category"] == "semantic"


def test_declared_extra_fields_SURVIVE_regeneration():
    """fenced_by and ctx_lifetime are declarations the verifier depends on. A
    merge that rebuilds rows from a fixed key list drops them silently."""
    prev = [_row("alpha", category="caller_fenced", fenced_by="outer", ctx_lifetime="retained")]
    out = merge(prev, {_site("alpha")}, None)
    assert out[0]["fenced_by"] == "outer"
    assert out[0]["ctx_lifetime"] == "retained"


def test_dropping_an_UNFENCED_site_is_allowed_silently():
    assert merge([_row("alpha", fenced=False)], set(), None) == []


def test_dropping_a_FENCED_site_ABORTS():
    """THE DEFECT. Rename a fenced function and its proof silently vanishes;
    after F3 removes test_f2_fences_nothing, CI stays green."""
    with pytest.raises(FencedSiteVanished) as ei:
        merge([_row("alpha", fenced=True)], set(), None)
    assert (KIND_TOOL_COMMIT, P, "alpha") in ei.value.lost


def test_a_rename_carries_the_flag_and_the_extra_fields_across():
    prev = [_row("alpha", fenced=True, category="caller_fenced", fenced_by="outer")]
    renames = {(KIND_TOOL_COMMIT, P, "alpha"): (KIND_TOOL_COMMIT, P, "beta")}
    out = merge(prev, {_site("beta")}, renames)
    assert len(out) == 1
    assert out[0]["symbol"] == "beta"
    assert out[0]["fenced"] is True
    assert out[0]["fenced_by"] == "outer"


def test_a_rename_whose_TARGET_is_absent_is_a_typo():
    renames = {(KIND_TOOL_COMMIT, P, "alpha"): (KIND_TOOL_COMMIT, P, "typo")}
    with pytest.raises(BadRenameMap):
        merge([_row("alpha", fenced=True)], {_site("beta")}, renames)


def test_a_rename_whose_OLD_identity_is_unknown_is_a_typo():
    """Silently ignoring it lets a human believe a rename was carried across."""
    renames = {(KIND_TOOL_COMMIT, P, "ghost"): (KIND_TOOL_COMMIT, P, "beta")}
    with pytest.raises(BadRenameMap):
        merge([_row("beta")], {_site("beta")}, renames)


def test_output_is_sorted_for_a_stable_diff():
    out = merge([], {_site("zeta"), _site("alpha")}, None)
    assert [r["symbol"] for r in out] == ["alpha", "zeta"]


def test_regeneration_preserves_every_human_slice_assignment():
    """merge() rebuilt `slice` from the kind defaults, so a regeneration would
    silently revert F3a's claims to F4, setup to F5's row to F4, the contexts to
    F3d and ingest to F3d — with every other gate still green."""
    before = {(r["kind"], r["path"], r["symbol"]): r["slice"] for r in load_inventory()}
    merged = merge(load_inventory(), scan_tree(), renames={})
    after = {(r["kind"], r["path"], r["symbol"]): r["slice"] for r in merged}
    assert after == before, "regeneration changed a human slice assignment"
    assert len(after) == 20
