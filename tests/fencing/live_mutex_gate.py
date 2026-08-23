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
HIDES an edge, so it can under-report too.  ``sink_calls`` already resolves
aliases via ``_sink_bindings``, so the direct approach hides nothing.

**Safe direction.**  ``unprotected_sinks`` under-reports by design (Task 5 brief,
§"What this costs"): a correct ``try:``-acquire gives its sink a ``stmt_index``
of ``None`` and measures unfenced.  The gate therefore over-approximates the
sink-holder set — an extra holder is a pin entry a human reads, never a hidden
escape.

**Transitive callers are NOT covered.**  ``apply_citation_to_room`` mutates the
room and then calls ``checkpoint_artifact``, which holds the sink; a per-function
checker cannot see across that boundary.  Those enclosing-acquisition sites are
recorded in ``ENCLOSING_ACQUISITIONS`` and convert in F3e1b with §5.2 runtime
proofs — not this gate.
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
#: This list SHRINKS; an addition is a reviewed diff, not a formality. Entries leave
#: across THREE slices, not one: the citation/checkpoint/save sites in F3e1b, the
#: annotation and bibliography sites in F3e2, and phase 0's in F5. A comment claiming
#: they all leave in F3e1b would make a reader treat a still-pinned F5 site as a bug.
UNCONVERTED_LIVE_CALLERS: frozenset[tuple[str, str]] = frozenset(
    {
        # (repo-relative path, qualname) — derived from Step 2 probe output, not guessed
        (
            "src/call_manager/proposal_assist/analysis/insert.py",
            "insert_from_annotation",
        ),
        (
            "src/call_manager/proposal_assist/citations/bibliography.py",
            "build_bibliography",
        ),
        (
            "src/call_manager/proposal_assist/citations/core.py",
            "_insert_citation_locked",
        ),
        (
            "src/call_manager/proposal_assist/citations/human_cite.py",
            "insert_citation_for_user",
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

#: Spec §4.2's enclosing-acquisition sites: functions that take
#: ``acquire_commit_mutex_live`` at a top-level boundary and then delegate to a
#: direct sink holder.  This static gate cannot see across that delegation boundary;
#: their proof is the §5.2 runtime race per entry point, which lands in F3e1b.
#:
#: NOTE: the brief's prose says "seven" enclosing acquisitions but then enumerates
#: thirteen names below.  The count in the prose is wrong; all enumerated names are
#: recorded faithfully here.
#:
#: ``_init_repo`` is NOT here — it holds the sink directly and belongs in
#: ``UNCONVERTED_LIVE_CALLERS``.  ``_m1`` IS here (setup.py:363 takes _live and
#: calls self._init_repo at :375 — the enclosing-acquisition shape).
#: ``post_init_spec._git`` appears in BOTH lists: it is a direct sink holder (pin
#: list) AND it also acquires _live directly before its sink (workspaces.py:540),
#: placing it in the enclosing-acquisition record too.
ENCLOSING_ACQUISITIONS: frozenset[str] = frozenset(
    {
        "apply_citation_to_room",
        "kernel_accept_prose",
        "kernel_init_spec",
        "kernel_accept_citation",
        "apply_annotation_insert_to_room",
        "post_lock_spec",
        "post_unlock_spec",
        "checkpoint_artifact",
        "_insert_citation_locked",
        "insert_citation_for_user",
        "save_section",
        "insert_from_annotation",
        "post_init_spec._git",
        "_m1",
    }
)


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
