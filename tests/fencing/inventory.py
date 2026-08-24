"""Fence F2 — inventory load/merge, including the rename guard."""

from __future__ import annotations

import json
import pathlib

from tests.fencing.detectors import (
    KIND_CLAIM_FN,
    KIND_GIT_COMMIT,
    KIND_TOOL_COMMIT,
    KIND_TOOL_CONTEXT,
    Site,
    find_fenced_git_commits,
    find_fenced_tool_commits,
    find_sites,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "call_manager"
INVENTORY = pathlib.Path(__file__).resolve().parent / "fence_inventory.json"

#: A site no human has classified yet. The gate REFUSES these, so a regeneration
#: cannot quietly admit an unreviewed effect site.
UNCLASSIFIED = "UNCLASSIFIED"

_SLICE_FOR_KIND = {
    KIND_CLAIM_FN: "F4",          # a NEW claim fn still defaults to F4
    KIND_GIT_COMMIT: UNCLASSIFIED,  # a NEW git site stops the build until allocated
}
_DEFAULT_SLICE = "F3d2"                   # was "F3d", split in F3d1

#: Which categories each kind may declare. Without this, a future tool_context
#: could be classified `accounting` and verify() would return success before its
#: kind-specific rule ever ran.
CATEGORIES_BY_KIND = {
    KIND_TOOL_COMMIT: frozenset({"semantic", "accounting", "caller_fenced", UNCLASSIFIED}),
    KIND_TOOL_CONTEXT: frozenset({"semantic", UNCLASSIFIED}),
    KIND_CLAIM_FN: frozenset({"semantic", UNCLASSIFIED}),
    # `semantic` only, and deliberately NO caller_fenced analogue: a caller's
    # transaction can fence a DB write, but nothing anyone else's transaction does
    # can fence a git commit. Either the function takes the mutex at a generation
    # itself or the effect is unfenced — there is no third party to defer to.
    KIND_GIT_COMMIT: frozenset({"semantic", UNCLASSIFIED}),
}

#: Kinds whose `fenced` is RECOMPUTED from source on every regeneration rather than
#: carried forward. A kind outside this set keeps its stored flag — `is this a port
#: call` is meaningless for a claim function.
MEASURED_KINDS = frozenset({KIND_TOOL_COMMIT, KIND_GIT_COMMIT})
CATEGORIES = frozenset().union(*CATEGORIES_BY_KIND.values())

#: EXEMPTION PINS. `accounting` and `caller_fenced` short-circuit the semantic
#: rule, and `retained` waives the construction-time generation. Each is a hole
#: in the gate, so each is pinned to the exact identity a human reviewed. A new
#: site cannot take an exemption by declaring one — it takes a diff here, which
#: is the point.
NON_SEMANTIC_PINS = {
    ("tool_commit", "src/call_manager/proposal_assist/citations/core.py",
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
    "F3a": "complete",  # claim identity for every tool-originating actor
    "F3b": "complete",  # the ports — no inventory row of its own; F3d converts callers
    "F3c": "complete",  # the import gate — no inventory row of its own
    # Spec §7's F3d, cut into three because it is ~26 production files and a
    # 512-line relocation. SLICE_STATUS has no partial state, so a multi-slice
    # conversion needs one key per slice or the first flipped row goes red.
    "F3d1": "complete",  # the spine: ports/, the bundle, the dispatch — no row of its own
    "F3d2": "complete",  # the submitters and the reads
    "F3d3": "complete",  # the citation core relocation, and the propose_* sites
    # Spec §7's F3e, cut into three for the same reason F3d was cut into three:
    # SLICE_STATUS has no partial state, so a multi-slice conversion needs one key
    # per slice or the first flipped row goes red.
    "F3e1a": "complete",  # the carrier, the git detector, the gates — no row of its own
    "F3e1a2": "complete",  # the allowlist walk — no row of its own
    # The 2026-08-24 endgame converts all 18 remaining sites across six tasks,
    # cutting ACROSS the F3e1b/F3e2/F4/F5 keys below. SLICE_STATUS has no partial
    # state, so — following the remedy this file already documents for F3d and F3e
    # ("a multi-slice conversion needs one key per slice or the first flipped row
    # goes red") — each endgame task gets its own key, marked `complete` when that
    # task lands, and the rows it converted are re-pointed to it. The keys below
    # keep exactly the rows their slice has NOT yet converted.
    "F3e-endgame-A": "complete",  # Task 2: the two workspace-object sites
    "F3e1b": "pending",   # the git-effect conversions
    "F3e2": "pending",    # the ingest pipeline and the filesystem effects
    "F3f": "pending",    # capability holders, then the handle
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

#: A SEPARATE, EXACT set for importing the F1 fencing primitives. Empty through
#: F3a — nothing imported them yet. F3b adds precisely its own port
#: implementation, which is the only place a fence may now be expressed: the
#: whole point of the ports is that no tool, route or pass runner writes one.
#: Sharing one list with the column check would both permit premature fencing
#: imports across all of F3a's files and block the ports, which live in neither.
#: The 2026-08-24 endgame widens this set as it converts: Primitive B lives in
#: `fencing.py` (the trusting primitive it replaces lived in `drafting/locking.py`),
#: so every converted git-write site necessarily imports `fencing` directly. Each
#: task's entries are a reviewed diff, exactly as the F3b entry above was.
FENCING_IMPORT_FILES: frozenset[str] = frozenset({
    "proposal_assist/port_impl/effects.py",
    # Endgame Group A (2026-08-24 Task 2)
    "proposal_assist/analysis/insert.py",
    "proposal_assist/citations/human_cite.py",
    # Endgame Group B (2026-08-24 Task 3) — workspace_write choke point
    "proposal_assist/web/_writes.py",
})

#: Extra declared keys, preserved verbatim across regeneration — they are what
#: the verifier reads.
OPTIONAL_KEYS = ("fenced_by", "ctx_lifetime")


#: Tool modules that do not yet satisfy the F3c import boundary, each PINNED to
#: the violations it is allowed to still have. Every entry is MIGRATION DEBT, not
#: a permission: F3d/F3e/F3f delete it as they convert the module,
#: `test_no_exemption_is_stale` goes red if a converted module is left here, and
#: `test_no_exempt_module_acquires_a_new_violation` goes red if a module on this
#: list reaches something new. Adding an entry and widening one are both reviewed
#: diffs — the cost falls on staying dirty.
#:
#: Violations are NORMALIZED (no path, no line number), so a pin survives an
#: unrelated edit; and the check is a SUBSET, so a REMOVED violation never fails.
#: Cleaning up is free, and both the list and each entry can only shrink.
TOOLS_NOT_YET_CONVERTED: dict[str, frozenset[str]] = {
    "audit.py": frozenset({
        "from call_manager.db.models import ...",
        "from call_manager.proposal_assist.citations.audit import ...",
        "from call_manager.proposal_assist.citations.bibliography import ...",
        "from call_manager.proposal_assist.citations.claims import ...",
        "from call_manager.proposal_assist.citations.core import ...",
        "from call_manager.proposal_assist.citations.markers import ...",
        "from call_manager.proposal_assist.tools.registry import ...",
        "names the resource locator repo_dir",
        "names the resource locator session_factory",
    }),
    "citations.py": frozenset({
        "from call_manager.db.models import ...",
        "from call_manager.proposal_assist.citations.core import ...",
        "from call_manager.proposal_assist.citations.ledger import ...",
        "from call_manager.proposal_assist.citations.markers import ...",
        "from call_manager.proposal_assist.citations.quotes import ...",
        "from call_manager.proposal_assist.collab import ...",
        "from call_manager.proposal_assist.collab.state import ...",
        "from call_manager.proposal_assist.editing.sectionizer import ...",
        "from call_manager.proposal_assist.tools.proposal import ...",
        "from call_manager.proposal_assist.tools.registry import ...",
        "names the resource locator repo_dir",
        "names the resource locator session_factory",
    }),
    "history.py": frozenset({
        "from call_manager.proposal_assist.tools.proposal import ...",
        "from call_manager.proposal_assist.tools.registry import ...",
        "names the resource locator repo_dir",
    }),
    "proposal.py": frozenset({
        "from call_manager.proposal_assist.artifacts import ...",
        "from call_manager.proposal_assist.citations.gitio import ...",
        "from call_manager.proposal_assist.collab import ...",
        "from call_manager.proposal_assist.collab.state import ...",
        "from call_manager.proposal_assist.editing.sectionizer import ...",
        "from call_manager.proposal_assist.tools.registry import ...",
        "names the resource locator repo_dir",
        "names the resource locator session_factory",
    }),
    "references.py": frozenset({
        "from call_manager.db.models import ...",
        "from call_manager.embeddings.client import ...",
        "from call_manager.proposal_assist.references.dispatcher import ...",
        "from call_manager.proposal_assist.references.embed import ...",
        "from call_manager.proposal_assist.references.fetcher import ...",
        "from call_manager.proposal_assist.references.ingest import ...",
        "from call_manager.proposal_assist.references.storage import ...",
        "from call_manager.proposal_assist.tools.registry import ...",
        "from sqlalchemy import ...",
        "from starlette.concurrency import ...",
        "names the resource locator session_factory",
        "names the resource locator settings",
        "names the resource locator workspaces_dir",
    }),
}

#: PERMANENTLY outside the gate, by name. `registry.py` is the composition root:
#: it defines ToolContext, builds the MCP server, imports every tool module to wire
#: it, and 47 sites import it — 7 non-tool src modules, 10 inside tools/, 30 tests.
#: It necessarily names things a tool may not. `__init__.py` re-exports from it.
#:
#: The skip is narrow, not unconditional — `check_composition_root` still forbids
#: both from importing the `call_manager.db` package at all (importing it hands you
#: `db.models`, see db/__init__.py:7), or sqlalchemy outside a REAL `if TYPE_CHECKING`,
#: so this cannot become where violations hide.
GATE_SKIPS: frozenset[str] = frozenset({"registry.py", "__init__.py"})


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


def scan_tree() -> tuple[set[Site], set[tuple[str, str, str]]]:
    """The sites, and the identities the code proves are port-only.

    Returns a PAIR since F3d2: `fenced` for MEASURED_KINDS is measured, not stored,
    so every caller that merges needs both halves and cannot forget the second.
    """
    sites: set[Site] = set()
    fenced_ids: set[tuple[str, str, str]] = set()
    for p in sorted(SRC.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(REPO).as_posix()
        source = p.read_text(encoding="utf-8")
        sites |= find_sites(source, rel)
        fenced_ids |= find_fenced_tool_commits(source, rel)
        fenced_ids |= find_fenced_git_commits(source, rel)
    return sites, fenced_ids


def load_inventory() -> list[dict]:
    return json.loads(INVENTORY.read_text(encoding="utf-8"))


def merge(
    previous: list[dict],
    found: set[Site],
    renames: dict[tuple[str, str, str], tuple[str, str, str]] | None,
    fenced_ids: set[tuple[str, str, str]],
) -> list[dict]:
    """Fold *found* into *previous*, preserving `category` and every declared
    optional key.

    `fenced` is NOT preserved for any kind in MEASURED_KINDS: since F3d2 it is
    MEASURED from the source via *fenced_ids*, so a hand-edited flag does not
    survive a regeneration. Every other kind still carries its stored value forward.

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
            # F3d2+F3e1a: for any kind in MEASURED_KINDS (tool_commit, git_commit),
            # `fenced` is MEASURED from the source, never carried forward. It was a
            # human boolean that nothing checked, which made "slice complete" an
            # assertion rather than a proof. Other kinds keep the stored flag: `is
            # this a port call` is meaningless for a claim function.
            "fenced": (
                (s.kind, s.path, s.symbol) in fenced_ids
                if s.kind in MEASURED_KINDS
                else (prev["fenced"] if prev else False)
            ),
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


#: The eight identities spec §7 pins by (slice, kind, symbol) — seven F3d's, one
#: F3e2's since F3d3b (moved to F3e1a Task 4).
#:
#: They are written down because the SCAN cannot be trusted to keep them: a
#: tool_commit is only detected inside the EFFECT SCOPE — proposal_assist/tools/
#: plus the declared RELOCATED_EFFECT_PATHS (detectors.py) — and FencedSiteVanished
#: only guards rows that are already fenced=True (see the merge above). Moving an
#: unfenced site to a path outside that scope therefore drops its row in silence,
#: and every remaining consistency test would keep passing over a smaller and
#: smaller set.
#:
#: Deleting an entry here is not a way to make a test pass. The entry leaves when
#: the identity is genuinely gone from the design — which for F3d means never; each
#: of these is converted, not abolished.
#:
#: `_write_citekey_cache` moved F3d3 -> F3e (F3d3b) -> F3e2 (F3e1a). Modifying a
#: slice string to follow a human reallocation is NOT the deletion this comment
#: forbids: the identity is intact and the row still exists.
F3D_IDENTITIES: frozenset[tuple[str, str, str]] = frozenset({
    ("F3d2", "tool_commit", "submit_review_impl"),
    ("F3d2", "tool_commit", "submit_scorecard_impl"),
    ("F3d2", "tool_commit", "submit_uncited_claims_impl"),
    ("F3d2", "tool_commit", "fetch_reference_impl"),
    ("F3d2", "tool_commit", "fetch_reference_impl._refund_fetch_slot"),
    ("F3d3", "tool_commit", "propose_citation_impl"),
    ("F3d3", "tool_commit", "_propose_artifact_edit_impl"),
    ("F3e2", "tool_commit", "_write_citekey_cache"),
})
