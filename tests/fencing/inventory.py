"""Fence F2 — inventory load/merge, including the rename guard."""

from __future__ import annotations

import json
import pathlib

from tests.fencing.detectors import (
    KIND_CLAIM_FN,
    KIND_TOOL_COMMIT,
    KIND_TOOL_CONTEXT,
    Site,
    find_sites,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "call_manager"
INVENTORY = pathlib.Path(__file__).resolve().parent / "fence_inventory.json"

_SLICE_FOR_KIND = {KIND_CLAIM_FN: "F4"}
_DEFAULT_SLICE = "F3"

#: A site no human has classified yet. The gate REFUSES these, so a regeneration
#: cannot quietly admit an unreviewed effect site.
UNCLASSIFIED = "UNCLASSIFIED"

#: Which categories each kind may declare. Without this, a future tool_context
#: could be classified `accounting` and verify() would return success before its
#: kind-specific rule ever ran.
CATEGORIES_BY_KIND = {
    KIND_TOOL_COMMIT: frozenset({"semantic", "accounting", "caller_fenced", UNCLASSIFIED}),
    KIND_TOOL_CONTEXT: frozenset({"semantic", UNCLASSIFIED}),
    KIND_CLAIM_FN: frozenset({"semantic", UNCLASSIFIED}),
}
CATEGORIES = frozenset().union(*CATEGORIES_BY_KIND.values())

#: Extra declared keys, preserved verbatim across regeneration — they are what
#: the verifier reads.
OPTIONAL_KEYS = ("fenced_by", "ctx_lifetime")


class FencedSiteVanished(RuntimeError):
    """A previously FENCED identity is missing from the new scan.

    Almost always a rename. Left to a naive merge it would drop a `true` entry
    and add a `false` one, silently un-proving a site — and after F3 deletes
    `test_f2_fences_nothing`, nothing else would catch it.
    """

    def __init__(self, lost: list[tuple[str, str, str]]) -> None:
        super().__init__(
            "previously fenced sites are gone from the scan:\n  "
            + "\n  ".join(f"{k} {p}::{s}" for k, p, s in lost)
            + "\n\nIf RENAMED, add a RENAMES mapping. If genuinely deleted, remove "
            "the entries by hand and say so in the commit message."
        )
        self.lost = lost


class BadRenameMap(RuntimeError):
    """A rename mapping that cannot apply: an unknown old identity, or a target
    absent from the scan. Both are typos. Ignoring either would let a human
    believe a rename carried a proof across when it did not."""


def scan_tree() -> set[Site]:
    sites: set[Site] = set()
    for p in sorted(SRC.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        sites |= find_sites(p.read_text(encoding="utf-8"), p.relative_to(REPO).as_posix())
    return sites


def load_inventory() -> list[dict]:
    return json.loads(INVENTORY.read_text(encoding="utf-8"))


def merge(
    previous: list[dict],
    found: set[Site],
    renames: dict[tuple[str, str, str], tuple[str, str, str]] | None,
) -> list[dict]:
    """Fold *found* into *previous*, preserving `fenced`, `category` and every
    declared optional key.

    Validates the WHOLE rename map up front, then aborts if any previously-fenced
    identity is still missing.
    """
    renames = renames or {}
    prev_by_id = {(r["kind"], r["path"], r["symbol"]): r for r in previous}
    found_ids = {(s.kind, s.path, s.symbol) for s in found}

    bad = []
    for old, new in renames.items():
        if old not in prev_by_id:
            bad.append(f"old identity not in the inventory: {old}")
        if new not in found_ids:
            bad.append(f"target not found in the scan: {new}")
    if bad:
        raise BadRenameMap("unusable rename mapping(s):\n  " + "\n  ".join(bad))

    for old, new in renames.items():
        row = prev_by_id.pop(old)
        prev_by_id[new] = {**row, "kind": new[0], "path": new[1], "symbol": new[2]}

    lost = sorted(i for i, r in prev_by_id.items() if r["fenced"] and i not in found_ids)
    if lost:
        raise FencedSiteVanished(lost)

    rows = []
    for s in sorted(found):
        prev = prev_by_id.get((s.kind, s.path, s.symbol))
        row = {
            "kind": s.kind,
            "path": s.path,
            "symbol": s.symbol,
            "category": prev["category"] if prev else UNCLASSIFIED,
            "fenced": prev["fenced"] if prev else False,
            "slice": _SLICE_FOR_KIND.get(s.kind, _DEFAULT_SLICE),
        }
        if prev:
            for key in OPTIONAL_KEYS:
                if key in prev:
                    row[key] = prev[key]
        rows.append(row)
    return rows
