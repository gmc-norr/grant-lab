"""Fence F2 — the coverage gate F3-F9 land against."""

from __future__ import annotations

import ast

from tests.fencing.detectors import KIND_TOOL_CONTEXT, Site, qualified_defs
from tests.fencing.inventory import (
    CATEGORIES_BY_KIND,
    REPO,
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


def test_f2_fences_nothing():
    """Global Constraint 1.

    DELETE THIS TEST IN F3, in the same commit that flips the first entry — not
    before, and never as a drive-by fix when it starts failing.
    """
    fenced = [r for r in load_inventory() if r["fenced"]]
    assert not fenced, (
        "F2 must fence nothing; these are marked fenced:\n  "
        + "\n  ".join(f"{r['kind']} {r['path']}::{r['symbol']}" for r in fenced)
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
