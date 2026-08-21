"""F3d3b — the two propose sites go onto the effect port, and F3d3 closes.

Spec 2026-08-18 section 7 (as amended 2026-08-21) assigns TWO rows to F3d3, both DB-only
writes on mechanism 1. It previously assigned three; _write_citekey_cache moved to F3e
because it is reached only from inside a transaction already holding the workspace commit
mutex EXCLUSIVELY, so the operation that fences it must own the whole git-then-rows
sequence — mechanism 2, which section 3.5 and EFFECT_MECHANISMS place in F3e.
"""

from __future__ import annotations


def test_the_citekey_row_belongs_to_the_slice_whose_mechanism_it_needs():
    """The reassignment, stated where a reviewer will look for it.

    Fencing this row with a mechanism-1 operation is not merely inelegant, it hangs: the
    caller holds pg_advisory_xact_lock on the workspace's mutex key and the port would
    open a SECOND connection asking for pg_advisory_xact_lock_shared on the same key.
    port_impl/effects.py's module docstring names this inversion explicitly.
    """
    from tests.fencing.inventory import F3D_IDENTITIES, load_inventory

    assert ("F3e", "tool_commit", "_write_citekey_cache") in F3D_IDENTITIES
    assert ("F3d3", "tool_commit", "_write_citekey_cache") not in F3D_IDENTITIES

    row = next(r for r in load_inventory() if r["symbol"] == "_write_citekey_cache")
    assert row["slice"] == "F3e"
    assert row["fenced"] is False


def test_f3d3_now_owns_exactly_the_two_propose_sites():
    from tests.fencing.inventory import load_inventory

    assert {r["symbol"] for r in load_inventory() if r["slice"] == "F3d3"} == {
        "propose_citation_impl",
        "_propose_artifact_edit_impl",
    }
