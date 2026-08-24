"""F3d3a — the citation core relocation. A pure move: the objects are the same
objects, at a new address, with no behaviour changed.

Spec 2026-08-18 revision 8 section 7: "moves the whole session/model-dependent
citation core out of tools/ into the effects/domain layer and updates every
non-tool consumer".
"""

from __future__ import annotations

import inspect

MOVED = [
    "_validate_stance_confidence",
    "_read_proposal_and_ledger",
    "_current_head",
    "_citation_snapshot",
    "_find_idempotent_row",
    "_resolve_free_citekey",
    "_resolve_citekey_for_ref",
    "insert_citation_impl",
    "compute_citation_insert",
    "_write_citekey_cache",
    "_insert_citation_locked",
]

CONSTANTS = [
    "CITATIONS_FILENAME",
    "_BUSY_ERROR",
    "_DELETED_ERROR",
    "_GIT_COMMIT_FAILED_ERROR",
    "ALLOWED_STANCES",
    "ALLOWED_CONFIDENCE",
]


def test_the_whole_closure_lives_in_the_new_module():
    """The set is the transitive closure of the five symbols the spec names,
    computed from the AST — not a judgement call. Moving a subset leaves the new
    module calling a name it cannot see."""
    from call_manager.proposal_assist.citations import core

    missing = [n for n in MOVED + CONSTANTS if not hasattr(core, n)]
    assert not missing, f"not moved: {missing}"


def test_the_moved_functions_are_defined_there_not_imported_back():
    """GC-4: no re-export shim. If a moved function's __module__ still points at
    tools.citations, the move did not happen — an import was added instead, and
    tools/ keeps the violation the move exists to remove."""
    from call_manager.proposal_assist.citations import core

    wrong = [
        n
        for n in MOVED
        if getattr(core, n).__module__ != "call_manager.proposal_assist.citations.core"
    ]
    assert not wrong, f"still owned by another module: {wrong}"


#: The ONE moved name `tools/citations.py` deliberately imports back, because the two
#: staying tools call it. Every other moved name must be UNREACHABLE from the tool
#: module — not merely "not defined there".
DELIBERATELY_IMPORTED_BACK = frozenset({"_validate_stance_confidence"})


def test_the_tool_module_no_longer_defines_them():
    """The other half of the same claim, catching BOTH a copy and a re-export shim.

    The __module__ check above cannot see a shim. Adding
    `from ...citations.core import insert_citation_impl` to tools/citations.py leaves
    that function's __module__ pointing at citations.core, so it still looks correctly
    owned while tools/ goes on handing it out — and the violation the move exists to
    remove survives. An adversarial review found this hole by injecting the shim at
    runtime and watching BOTH guards stay green.

    So the rule is presence, not ownership: a moved name must not be reachable from the
    tool module at all, except the single one the staying tools genuinely call. That
    also subsumes the copy case a duplicated `def` would create.
    """
    from call_manager.proposal_assist.tools import citations as tool_mod

    leaked = sorted(
        n for n in MOVED if hasattr(tool_mod, n) and n not in DELIBERATELY_IMPORTED_BACK
    )
    assert not leaked, (
        f"still reachable from tools/citations.py: {leaked}\n\n"
        "Either still defined there (a copy, which drifts) or imported back (a shim, "
        "which keeps tools/ handing out the code this slice moved out). If a staying "
        "tool genuinely needs one of these, adding it to DELIBERATELY_IMPORTED_BACK is "
        "a reviewed design change, not a way to make this pass."
    )


def test_the_signatures_are_unchanged():
    """GC-1: this is a MOVE. A changed signature is a conversion wearing a move's
    clothes, and it destroys the one review property this slice has — that the
    diff can be checked by comparing bytes."""
    from call_manager.proposal_assist.citations import core

    assert str(inspect.signature(core._write_citekey_cache)) == (
        "(session: 'Session', ref: 'Reference', citekey: 'str', *, owns_txn: 'bool') -> 'None'"
    )
    assert list(inspect.signature(core._resolve_citekey_for_ref).parameters) == [
        "session",
        "ledger",
        "ref",
        "workspace_id",
    ]
    assert list(inspect.signature(core._insert_citation_locked).parameters)[:2] == [
        "ctx",
        "session",
    ]


def test_the_relocated_module_may_hold_a_session_and_that_is_the_point():
    """GC-3. The new module is outside tools/, so the import gate does not apply.
    Do not try to make it gate-clean: being able to name a Session here is the
    reason the code moved."""
    from call_manager.proposal_assist.citations import core

    src = inspect.getsource(core)
    assert "from sqlalchemy" in src or "import sqlalchemy" in src
    assert "ToolContext" in src


# Each patched name paired with the function in citations/core.py that actually READS
# it. Verified against f9014b68 with ast: compute_citation_insert reads
# _resolve_citekey_for_ref and new_ev_id; _insert_citation_locked reads
# write_and_commit_files. Pairing all three with one reader is how the first draft of
# this test got it wrong.
PATCH_TARGETS = [
    ("_resolve_citekey_for_ref", "compute_citation_insert"),
    ("new_ev_id", "compute_citation_insert"),
    ("write_and_commit_files", "_insert_citation_locked"),
]


def test_the_monkeypatch_targets_are_the_module_that_actually_reads_them():
    """GC-5, the sharpest hazard in this slice.

    monkeypatch.setattr(mod, "name", ...) rebinds the name AS THAT MODULE SEES IT.
    Three test modules patch these names on tools.citations at four sites; after the
    move the readers live in citations.core, so a patch on the old module rebinds a
    name nothing reads and the test passes vacuously. Two of the four are NEGATIVE
    tests (a forced citekey collision at test_accepting.py:591, a forced GitCommitError
    at :655), which is the direction that fails silently.

    Pinning the READER, not just the module, is what makes this a real check: a name
    that is present in citations.core but read by nobody would satisfy a hasattr test
    and still leave the patch site dead.
    """
    import importlib

    core = importlib.import_module("call_manager.proposal_assist.citations.core")

    for name, reader_name in PATCH_TARGETS:
        assert hasattr(core, name), f"citations.core has no {name}"
        reader = getattr(core, reader_name)
        assert name in reader.__code__.co_names, (
            f"{name} is not read by {reader_name} — the patch-target list is stale, "
            "which is exactly the failure this test exists to catch"
        )


def test_no_tool_module_gained_a_port_call():
    """GC-1. The written-down set of EVERY fenced identity — not only the tool-commit
    ones the function name suggests. F3d3a converted nothing (F3d2's five); F3d3b
    Task 2 added _propose_artifact_edit_impl and Task 3 adds propose_citation_impl —
    BOTH of F3d3's propose sites now write through ports.effects and no longer commit.

    The 2026-08-24 endgame slice then added the first two `git_commit` rows, which is
    why the literal is no longer homogeneous in `kind`. Scoping the comparison to
    `kind == "tool_commit"` was considered and REJECTED: that is precisely the derived
    set this docstring warns about, and it would make every later git_commit
    conversion invisible here.

    The set stays a literal on purpose: a derived set would silently absorb the next
    conversion instead of forcing this diff. Each endgame task that converts a site is
    expected to edit this literal — that cost IS the feature."""
    from tests.fencing.inventory import load_inventory, scan_tree

    _sites, fenced = scan_tree()
    assert fenced == {
        (
            "tool_commit",
            "src/call_manager/proposal_assist/tools/scorecard.py",
            "submit_scorecard_impl",
        ),
        (
            "tool_commit",
            "src/call_manager/proposal_assist/tools/review_submit.py",
            "submit_review_impl",
        ),
        (
            "tool_commit",
            "src/call_manager/proposal_assist/tools/uncited_claims.py",
            "submit_uncited_claims_impl",
        ),
        (
            "tool_commit",
            "src/call_manager/proposal_assist/tools/references.py",
            "fetch_reference_impl",
        ),
        (
            "tool_commit",
            "src/call_manager/proposal_assist/tools/references.py",
            "fetch_reference_impl._refund_fetch_slot",
        ),
        (
            "tool_commit",
            "src/call_manager/proposal_assist/tools/proposal.py",
            "_propose_artifact_edit_impl",
        ),
        (
            "tool_commit",
            "src/call_manager/proposal_assist/tools/citations.py",
            "propose_citation_impl",
        ),
        # --- 2026-08-24 endgame, Task 2: the first two git_commit conversions ---
        (
            "git_commit",
            "src/call_manager/proposal_assist/analysis/insert.py",
            "insert_from_annotation",
        ),
        (
            "git_commit",
            "src/call_manager/proposal_assist/citations/human_cite.py",
            "insert_citation_for_user",
        ),
    }, (
        "F3d2's five, plus F3d3b's TWO propose sites (artifact-edit + citation), "
        "plus the endgame slice's git_commit conversions"
    )
    # Measured from the LIVE tree, not the stored JSON: this task fences the SECOND
    # of F3d3's two sites (the citation propose); Task 2 fenced the first. The
    # inventory JSON is regenerated in Task 4, so load_inventory() still reports both
    # F3d3 rows fenced=false here — the SCAN is the source of truth for what the code
    # actually does now.
    f3d3_sites = {
        (r["kind"], r["path"], r["symbol"])
        for r in load_inventory()
        if r["slice"] == "F3d3"
    }
    assert len(f3d3_sites & fenced) == 2
    assert (
        "tool_commit",
        "src/call_manager/proposal_assist/tools/proposal.py",
        "_propose_artifact_edit_impl",
    ) in fenced
    assert (
        "tool_commit",
        "src/call_manager/proposal_assist/tools/citations.py",
        "propose_citation_impl",
    ) in fenced


def test_the_exemption_list_still_has_five_keys():
    """The honest headline. Both citations.py and proposal.py are blocked on the
    repo_dir locator, which retires in F3f — so this slice SHRINKS two pin sets and
    retires neither. Saying otherwise would misreport the fence's progress."""
    from tests.fencing.inventory import TOOLS_NOT_YET_CONVERTED

    assert set(TOOLS_NOT_YET_CONVERTED) == {
        "audit.py",
        "citations.py",
        "history.py",
        "proposal.py",
        "references.py",
    }


def test_the_moved_code_still_names_repo_dir_and_that_is_expected():
    """Where the remaining work lives. Four of citations.py's nine repo_dir namings
    left tools/ with this move; the rest sit in list_citations_impl and
    propose_citation_impl, which stay. F3f owns the locator."""
    import inspect

    from call_manager.proposal_assist.citations import core

    assert "repo_dir" in inspect.getsource(core)
