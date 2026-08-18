"""Fence F2 — the coverage gate F3-F9 land against."""

from __future__ import annotations

import ast

from tests.fencing.detectors import KIND_TOOL_CONTEXT, Site, qualified_defs
from tests.fencing.inventory import (
    CATEGORIES_BY_KIND,
    NON_SEMANTIC_PINS,
    REPO,
    RETAINED_PINS,
    SLICE_STATUS,
    SRC,
    UNCLASSIFIED,
    load_inventory,
    scan_tree,
)


def _as_sites(rows: list[dict]) -> set[Site]:
    return {Site(r["kind"], r["path"], r["symbol"]) for r in rows}


def test_the_inventory_matches_the_tree_exactly():
    found = scan_tree()
    recorded = _as_sites(load_inventory())
    unlisted = sorted(found - recorded)
    stale = sorted(recorded - found)

    assert not unlisted, (
        "NEW candidate effect sites are missing from fence_inventory.json:\n  "
        + "\n  ".join(f"{s.kind} {s.path}::{s.symbol}" for s in unlisted)
        + "\n\nRun `uv run python -m tests.fencing.regenerate_inventory`, then "
        "CLASSIFY each new entry."
    )
    assert not stale, (
        "fence_inventory.json lists sites that no longer exist:\n  "
        + "\n  ".join(f"{s.kind} {s.path}::{s.symbol}" for s in stale)
        + "\n\nIf RENAMED, use RENAMES in regenerate_inventory.py so the fenced "
        "flag carries across."
    )


def test_every_entry_is_well_formed():
    for row in load_inventory():
        assert set(row) - {"fenced_by", "ctx_lifetime"} == {
            "kind", "path", "symbol", "category", "fenced", "slice",
        }, row
        assert isinstance(row["fenced"], bool)


def test_each_category_is_legal_for_its_kind():
    """Without this, a future tool_context could be classified `accounting` and
    verify() would return success before its kind-specific rule ever ran."""
    for row in load_inventory():
        allowed = CATEGORIES_BY_KIND.get(row["kind"])
        assert allowed is not None, f"unknown kind: {row}"
        assert row["category"] in allowed, (
            f"category {row['category']!r} is not legal for kind {row['kind']!r}: {row}"
        )


def test_no_entry_is_unclassified():
    bad = [r for r in load_inventory() if r["category"] == UNCLASSIFIED]
    assert not bad, (
        "unclassified sites:\n  "
        + "\n  ".join(f"{r['kind']} {r['path']}::{r['symbol']}" for r in bad)
        + "\n\nClassify each: semantic | accounting | caller_fenced."
    )


def test_every_tool_context_declares_its_lifetime():
    """One-shot contexts must carry the generation at construction. The RETAINED
    interactive one cannot: it is built during launch, which deliberately runs
    BEFORE the claim (turns/processor.py:159), so the generation does not exist
    yet. Requiring a construction value would bless a placeholder."""
    for row in load_inventory():
        if row["kind"] != KIND_TOOL_CONTEXT:
            continue
        assert row.get("ctx_lifetime") in {"one_shot", "retained"}, (
            f"{row['path']}::{row['symbol']} declares no ctx_lifetime"
        )


def test_caller_fenced_entries_name_a_resolvable_ancestor():
    """`caller_fenced` without a named, resolvable ancestor is an unfalsifiable
    excuse. Task 4 additionally proves the ancestor encloses the call."""
    for row in load_inventory():
        if row["category"] != "caller_fenced":
            continue
        ancestor = row.get("fenced_by")
        assert ancestor, f"{row['path']}::{row['symbol']} names no fenced_by"
        defs = qualified_defs(ast.parse((REPO / row["path"]).read_text(encoding="utf-8")))
        assert ancestor in defs, (
            f"{row['path']}::{row['symbol']} names fenced_by={ancestor!r}, which "
            f"does not resolve in that file"
        )


def test_each_slice_matches_its_declared_status():
    """Successor to F2's `test_f2_fences_nothing`, and permanent.

    While a slice is `pending` every one of its rows must be `fenced: false`;
    once it is `complete` every row must be `true`. F3 flips its entry in the
    SAME commit it converts its sites, so a forgotten flag fails here instead of
    leaving a semantic site unverified with green tests. This is also what makes
    the `slice` field load-bearing — it was required but never read.
    """
    problems = []
    for row in load_inventory():
        status = SLICE_STATUS.get(row["slice"])
        assert status in {"pending", "complete"}, (
            f"{row['slice']!r} has no entry in SLICE_STATUS: {row}"
        )
        want = status == "complete"
        if row["fenced"] is not want:
            problems.append(
                f"{row['kind']} {row['path']}::{row['symbol']} is fenced="
                f"{row['fenced']} but slice {row['slice']} is {status}"
            )
    assert not problems, (
        "slice status and fenced flags disagree:\n  " + "\n  ".join(problems)
    )


def test_only_pinned_sites_take_a_non_semantic_category():
    """`accounting` and `caller_fenced` short-circuit the semantic rule.

    Without this pin, relabelling a semantic site `accounting` is a legal
    inventory edit that silently stops verifying it — confirmed reachable: the
    unfenced fixture passes `verify()` the moment its category changes. Taking
    an exemption must cost a diff in NON_SEMANTIC_PINS, which a human reads.
    """
    problems = []
    for row in load_inventory():
        ident = (row["kind"], row["path"], row["symbol"])
        pinned = NON_SEMANTIC_PINS.get(ident)
        if row["category"] == "semantic":
            if pinned is not None:
                problems.append(f"{ident} is pinned {pinned!r} but declares semantic")
        elif row["category"] != pinned:
            problems.append(
                f"{ident} declares {row['category']!r}; NON_SEMANTIC_PINS says "
                f"{pinned!r}"
            )
    assert not problems, "unpinned exemption:\n  " + "\n  ".join(problems)


def test_only_the_pinned_context_may_be_retained():
    """`retained` waives the construction-time generation entirely.

    It is correct for exactly one identity — the interactive context built
    during launch, which precedes the claim. Relabelling a one-shot background
    context `retained` lets it omit the generation with every test green.
    """
    problems = [
        f"{row['path']}::{row['symbol']}"
        for row in load_inventory()
        if row.get("ctx_lifetime") == "retained"
        and (row["path"], row["symbol"]) not in RETAINED_PINS
    ]
    assert not problems, (
        "unpinned retained context — it may omit the generation at "
        "construction, so it must be reviewed:\n  " + "\n  ".join(problems)
    )


def test_nothing_imports_the_fencing_primitives_yet():
    """Direct successor to F1's inertness check, catching BOTH import spellings.
    F1's matched only `node.module.endswith("proposal_assist.fencing")`, so
    `from call_manager.proposal_assist import fencing` slipped past it."""
    offenders = []
    for p in sorted(SRC.rglob("*.py")):
        if "__pycache__" in p.parts or p.name == "fencing.py":
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.endswith("proposal_assist.fencing") or any(
                    a.name == "fencing" for a in node.names
                ):
                    offenders.append(f"{p}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith("proposal_assist.fencing"):
                        offenders.append(f"{p}: import {alias.name}")
    assert not offenders, "the fencing primitives are wired:\n  " + "\n  ".join(offenders)


def test_nothing_reads_or_writes_claimed_at_generation_yet():
    """Direct successor to F1's third inertness check. NOT subsumed by the
    `fenced` flag: setting `job.claimed_at_generation = 0` while leaving the
    entry `false` changes no DETECTED site and passes every other check here."""
    offenders = [
        str(p)
        for p in sorted(SRC.rglob("*.py"))
        if "__pycache__" not in p.parts
        and p.name != "models.py"
        and "claimed_at_generation" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "claimed_at_generation is wired before F4:\n  " + "\n  ".join(offenders)
    )


def test_every_fenced_entry_really_fences():
    """Has no live data at F2 — every entry is `fenced: false`. Its logic is
    proven against fixtures in test_verifiers.py; this wires it to the tree so
    F3's first flip is checked the moment it lands."""
    from tests.fencing.verifiers import verify

    problems = []
    for row in load_inventory():
        if not row["fenced"]:
            continue
        src = (REPO / row["path"]).read_text(encoding="utf-8")
        for reason in verify(row, src):
            problems.append(f"{row['path']}::{row['symbol']}: {reason}")
    assert not problems, (
        "entries marked fenced that are not properly fenced:\n  " + "\n  ".join(problems)
    )
