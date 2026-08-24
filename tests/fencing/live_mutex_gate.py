"""Gate: every function holding a git sink must be pinned or pass the lexical check.

"Passes the lexical check" is deliberately weaker than "provably fenced", which is
what this docstring used to say. See git_fence.py's module docstring: the check
proves the acquisition precedes the sink, not that the mutex is still held.

**The rule** (built directly on ``git_fence.unprotected_sinks``):

    Every ``(path, qualname)`` that ``unprotected_sinks`` reports across
    ``src/call_manager/`` must be in ``UNCONVERTED_LIVE_CALLERS``.

This is the gate that would have caught spec revision 1's defect: converting the
shared ``checkpoint_artifact`` while five kernel operations still took ``_live``
at an outer boundary and mutated before reaching it.

**Why direct, not a call graph.**  An earlier draft built its own call graph and
rejected only functions still taking ``_live``.  Two holes: deleting an outer
``_live`` WITHOUT adding ``_at_generation`` passed once its pin was removed, and
a bare-name call graph does not "only over-report" — an alias or a rebound helper
HIDES an edge, so it can under-report too.  ``sink_calls`` resolves aliases via
``_sink_bindings``, so the direct approach hides strictly less — not nothing.
MEASURED: ``_sink_bindings`` resolves ``from m import _run_git as wac`` and
``wac = _run_git``, but NOT the qualified ``wac = m._run_git``, which binds the
sink without ever writing a bare name.  A call graph would compound that miss
across every edge; the direct approach pays it once, at the call site.

**Safe direction.**  ``unprotected_sinks`` under-reports by design (Task 5 brief,
§"What this costs"): a correct ``try:``-acquire gives its sink a ``stmt_index``
of ``None`` and measures unfenced.  The gate therefore over-approximates the
sink-holder set — an extra holder is a pin entry a human reads, never a hidden
escape.

**Transitive callers are NOT covered.**  ``apply_citation_to_room`` mutates the
room and then calls ``checkpoint_artifact``, which holds the sink; a per-function
checker cannot see across that boundary.  Those enclosing-acquisition sites are
recorded in ``ENCLOSING_ACQUISITIONS`` and convert with §5.2 runtime proofs — not
this gate.  Seven of the eight are F3e1b's (spec §4.2); ``Phase0Worker._m1``
reaches an F5 sink and converts with F5.

Every ``acquire_commit_mutex_live`` caller in ``src/`` belongs to exactly one of
three records — ``ENCLOSING_ACQUISITIONS``, ``LIVE_ACQUIRING_SINK_HOLDERS``, or
``LIVE_ACQUIRERS_WITHOUT_A_SINK`` — and ``test_the_live_acquirers_partition_exactly``
holds that partition against the tree.  Before that test the records were one
frozenset with **zero readers**, and its own comment miscounted it.
"""

from __future__ import annotations

import pathlib
from collections.abc import Mapping

from tests.fencing import git_fence

# ---------------------------------------------------------------------------
# Repository root
# ---------------------------------------------------------------------------

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "call_manager"


# ---------------------------------------------------------------------------
# Pin list
# ---------------------------------------------------------------------------

#: Functions holding a git sink that the conservative checker cannot prove fenced.
#:
#: **A pin here does NOT mean the site is broken.** git_fence under-reports BY DESIGN
#: (Task 5): a correct acquisition inside a ``try:`` measures unfenced. Three sites are
#: exactly that shape and will still be pinned after F3e1b converts them correctly,
#: carrying a human sentence and a §5.2 runtime proof instead.
#:
#: This list SHRINKS; an addition is a reviewed diff, not a formality. Entries do NOT
#: all leave in one slice — a comment claiming they did would make a reader treat a
#: still-pinned site as a bug. The 2026-08-24 endgame is now removing them task by
#: task: Group A (`insert_from_annotation`, `insert_citation_for_user`) left in its
#: Task 2, and the remaining seven leave across its Tasks 3-6.
UNCONVERTED_LIVE_CALLERS: frozenset[tuple[str, str]] = frozenset(
    {
        # (repo-relative path, qualname) — derived from Step 2 probe output, not guessed
        (
            "src/call_manager/proposal_assist/citations/bibliography.py",
            "build_bibliography",
        ),
        (
            "src/call_manager/proposal_assist/citations/core.py",
            "_insert_citation_locked",
        ),
        (
            "src/call_manager/proposal_assist/collab/checkpoint.py",
            "checkpoint_artifact",
        ),
        (
            "src/call_manager/proposal_assist/editing/save.py",
            "save_section",
        ),
        (
            "src/call_manager/proposal_assist/phases/setup.py",
            "Phase0Worker._init_repo",
        ),
        (
            "src/call_manager/proposal_assist/review/pass_runner.py",
            "run_review_pass",
        ),
        (
            "src/call_manager/web/api/workspaces.py",
            "post_init_spec._git",
        ),
    }
)


# ---------------------------------------------------------------------------
# Enclosing-acquisition record (NOT gate-proven — written down for F3e1b)
# ---------------------------------------------------------------------------

#: **Spec §4.2's "seven" is CORRECT — do not "fix" it.** An earlier version of this
#: comment claimed the spec's prose was wrong because it says seven while this record
#: held fourteen names.  They count different things, and the record was the confused one:
#:
#:   * §4.2's table is scoped to **F3e1b**. All seven of its rows reach
#:     ``checkpoint_artifact``, which is F3e1b's own inventory row.
#:   * This record held every ``acquire_commit_mutex_live`` caller that reaches a sink
#:     AT ALL — across every slice, and including six that hold the sink themselves and
#:     are therefore not "enclosing" anything.
#:
#: Two categories under a name claiming one is how a reader concludes the spec is wrong.
#: They are split below, and ``test_the_live_acquirers_partition_exactly`` pins the split
#: against the tree.  Nothing read this record before that test existed — a written-down
#: count with zero consumers drifts silently, which is precisely what happened here
#: (the comment said "thirteen", the set held fourteen, and neither was checked).

#: Take ``acquire_commit_mutex_live`` at a boundary, then DELEGATE to a direct sink
#: holder.  This static gate cannot see across that delegation boundary; the proof is
#: the §5.2 runtime race per entry point.  Mapped to the slice that converts each one —
#: seven are F3e1b's (§4.2's table), and ``Phase0Worker._m1`` is NOT: it reaches
#: ``_init_repo``, whose row is **F5** (spec §7 line 233 and §12), so it converts with F5.
#: Treating it as an F3e1b deliverable would make F3e1b's completion check unsatisfiable.
ENCLOSING_ACQUISITIONS: Mapping[tuple[str, str], str] = {
    ("src/call_manager/proposal_assist/collab/kernel.py", "apply_citation_to_room"): "F3e1b",
    ("src/call_manager/proposal_assist/collab/kernel.py", "kernel_accept_prose"): "F3e1b",
    ("src/call_manager/proposal_assist/collab/kernel.py", "kernel_init_spec"): "F3e1b",
    ("src/call_manager/proposal_assist/collab/kernel.py", "kernel_accept_citation"): "F3e1b",
    (
        "src/call_manager/proposal_assist/collab/kernel.py",
        "apply_annotation_insert_to_room",
    ): "F3e1b",
    ("src/call_manager/web/api/workspaces.py", "post_lock_spec"): "F3e1b",
    ("src/call_manager/web/api/workspaces.py", "post_unlock_spec"): "F3e1b",
    ("src/call_manager/proposal_assist/phases/setup.py", "Phase0Worker._m1"): "F5",
}

#: Take ``acquire_commit_mutex_live`` AND hold a git sink in the same function. Not
#: enclosing acquisitions — the static gate CAN see these, and each already owns an
#: inventory row.  Recorded only so the partition below is total.
LIVE_ACQUIRING_SINK_HOLDERS: frozenset[tuple[str, str]] = frozenset(
    {
        ("src/call_manager/proposal_assist/collab/checkpoint.py", "checkpoint_artifact"),
        ("src/call_manager/proposal_assist/citations/core.py", "_insert_citation_locked"),
        ("src/call_manager/proposal_assist/editing/save.py", "save_section"),
        ("src/call_manager/web/api/workspaces.py", "post_init_spec._git"),
    }
)

#: Take ``acquire_commit_mutex_live`` and reach NO git sink. Nothing to convert, but
#: pinned by name so the partition is total: a NEW ``_live`` caller lands in none of the
#: three sets and fails the partition test, instead of being silently absent from a
#: record nobody reads.
LIVE_ACQUIRERS_WITHOUT_A_SINK: frozenset[tuple[str, str]] = frozenset(
    {
        ("src/call_manager/proposal_assist/collab/hydrate.py", "load_or_hydrate"),
        ("src/call_manager/proposal_assist/collab/flip.py", "maybe_auto_enable"),
        ("src/call_manager/workspaces.py", "soft_delete_workspace_db"),
        ("src/call_manager/proposal_assist/web/_writes.py", "workspace_write"),
    }
)


def live_acquirer_qualnames() -> set[tuple[str, str]]:
    """Every ``acquire_commit_mutex_live`` caller in ``src/``, as ``(path, qualname)``.

    PATH-QUALIFIED. Qualnames alone are dotted and consistent (``Phase0Worker._m1``,
    ``post_init_spec._git``), but a set of them still cannot tell two same-named
    functions in different files apart — so a site added in one file and removed in
    another leaves the partition green. That is the same weakness a holder COUNT has,
    and it survived here for one revision after being fixed in both gates.
    """
    import ast

    found: set[tuple[str, str]] = set()

    def walk(node: ast.AST, qual: str, rel: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                walk(child, f"{qual}.{child.name}" if qual else child.name, rel)
                continue
            if isinstance(child, ast.Call):
                func = child.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name == "acquire_commit_mutex_live":
                    found.add((rel, qual))
            walk(child, qual, rel)

    for path in _SRC.rglob("*.py"):
        walk(ast.parse(path.read_text()), "", str(path.relative_to(_REPO)))
    return found


# ---------------------------------------------------------------------------
# Gate functions
# ---------------------------------------------------------------------------


def all_unprotected() -> set[tuple[str, str]]:
    """Every ``(repo-relative-path, qualname)`` that ``unprotected_sinks`` reports
    across ``src/call_manager/``.

    ``sink_calls`` already resolves aliases, so nothing is hidden.  The set
    over-approximates the truly-unfenced set (``unprotected_sinks`` under-reports
    by design), so it is safe to use as the gate's basis.
    """
    result: set[tuple[str, str]] = set()
    for py_file in sorted(_SRC.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        source = py_file.read_text(encoding="utf-8")
        rel = str(py_file.relative_to(_REPO))
        unprotected = git_fence.unprotected_sinks(source, rel)
        for qualname in unprotected:
            result.add((rel, qualname))
    return result


def check_tree() -> list[str]:
    """One problem string per ``(path, qualname)`` in ``all_unprotected()`` that is
    NOT in ``UNCONVERTED_LIVE_CALLERS``.  Empty list means the gate is green.

    A non-empty result means a new (or newly-discovered) sink holder appeared in
    ``src/`` that has not been reviewed and pinned — this is the gate failing open.
    """
    problems: list[str] = []
    for path, qualname in sorted(all_unprotected()):
        if (path, qualname) not in UNCONVERTED_LIVE_CALLERS:
            problems.append(
                f"{path}::{qualname} holds a git sink and is not pinned in"
                f" UNCONVERTED_LIVE_CALLERS"
            )
    return problems


def check_sources(mapping: Mapping[str, str]) -> list[str]:
    """The same gate over a synthetic ``{path: source_text}`` map.

    Drives the gate without touching ``src/`` — essential for testing the gate's
    mechanical behaviour (fenced vs. unfenced shapes) after the tree is fully
    converted, at which point ``check_tree`` would always return ``[]``.

    ``UNCONVERTED_LIVE_CALLERS`` is deliberately NOT consulted here: synthetic paths
    never appear in the pin list, so every unprotected holder in the synthetic map is
    reported as a problem.  This lets the test prove the gate catches the right shape
    without needing a real pin.
    """
    problems: list[str] = []
    for path, source in sorted(mapping.items()):
        unprotected = git_fence.unprotected_sinks(source, path)
        for qualname in sorted(unprotected):
            problems.append(
                f"{path}::{qualname} holds a git sink and is not pinned in"
                f" UNCONVERTED_LIVE_CALLERS"
            )
    return problems
