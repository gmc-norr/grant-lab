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

_SLICE_FOR_KIND = {KIND_CLAIM_FN: "F4"}   # a NEW claim fn still defaults to F4
_DEFAULT_SLICE = "F3d"                    # was "F3"

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

#: EXEMPTION PINS. `accounting` and `caller_fenced` short-circuit the semantic
#: rule, and `retained` waives the construction-time generation. Each is a hole
#: in the gate, so each is pinned to the exact identity a human reviewed. A new
#: site cannot take an exemption by declaring one — it takes a diff here, which
#: is the point.
NON_SEMANTIC_PINS = {
    ("tool_commit", "src/call_manager/proposal_assist/tools/citations.py",
     "_write_citekey_cache"): "caller_fenced",
    ("tool_commit", "src/call_manager/proposal_assist/tools/references.py",
     "fetch_reference_impl"): "accounting",
    ("tool_commit", "src/call_manager/proposal_assist/tools/references.py",
     "fetch_reference_impl._refund_fetch_slot"): "accounting",
}

#: The ONE context built before its claim exists. `drafting/factory.py` runs
#: inside `controller.launch(...)`, and launch deliberately precedes the claim
#: (turns/processor.py:159 — the orphan sweep must run first). Every other
#: context knows its generation at construction and must carry it.
RETAINED_PINS = {
    ("src/call_manager/proposal_assist/drafting/factory.py",
     "_build_adapter.tools_factory"),
}

#: Per-slice activation. `pending` = that slice has not converted its sites, so
#: every row must be `fenced: false`. `complete` = every row must be `true`.
#: F3 is split into sub-slices (see the F3 mechanism spec §7); F4-F9 keep the
#: meanings the architecture spec gives them.
SLICE_STATUS = {
    "F3a": "pending",   # claim identity for every tool-originating actor
    "F3b": "pending",   # the ports
    "F3c": "pending",   # the import gate
    "F3d": "pending",   # convert the tool sites
    "F3e": "pending",   # the ingest pipeline
    "F3f": "pending",   # capability holders, then the handle
    "F4": "pending",    # background job families
    "F5": "pending",    # setup (Phase 0)
}

#: The ONLY files that may name `claimed_at_generation`. Scoped successor to F2's
#: unconditional guard, which forbade it everywhere and therefore goes red the
#: moment F3a's first claim writes it. Every path is an F3a identity or the
#: plumbing that carries its value.
F3A_COLUMN_FILES = frozenset({
    "proposal_assist/turns/processor.py",
    "proposal_assist/coherence/jobs.py",
    "proposal_assist/coherence/pass_runner.py",
    "proposal_assist/evaluator/jobs.py",
    "proposal_assist/evaluator/pass_runner.py",
    "proposal_assist/review/jobs.py",
    "proposal_assist/review/pass_runner.py",
    "proposal_assist/uncited_claims/jobs.py",
    "proposal_assist/uncited_claims/pass_runner.py",
    "db/models.py",
})

#: A SEPARATE, EXACT set for importing the F1 fencing primitives. It is EMPTY
#: through F3a — nothing imports them yet — and F3b adds precisely its own port
#: implementation files. Sharing one list with the column check would both permit
#: premature fencing imports across all of F3a's files and block F3b's ports,
#: which live in neither.
FENCING_IMPORT_FILES: frozenset[str] = frozenset()

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
            "slice": (
                prev["slice"] if prev is not None
                else _SLICE_FOR_KIND.get(s.kind, _DEFAULT_SLICE)
            ),
        }
        if prev:
            for key in OPTIONAL_KEYS:
                if key in prev:
                    row[key] = prev[key]
        rows.append(row)
    return rows
