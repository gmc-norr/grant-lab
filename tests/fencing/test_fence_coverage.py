"""Fence F2 — the coverage gate F3-F9 land against."""

from __future__ import annotations

import ast

from tests.fencing.detectors import KIND_TOOL_CONTEXT, Site, qualified_defs
from tests.fencing.inventory import (
    CATEGORIES_BY_KIND,
    F3A_COLUMN_FILES,
    F3D_IDENTITIES,
    FENCING_IMPORT_FILES,
    NON_SEMANTIC_PINS,
    REPO,
    RETAINED_PINS,
    SLICE_STATUS,
    SRC,
    UNCLASSIFIED,
    load_inventory,
    merge,
    scan_tree,
)


def _as_sites(rows: list[dict]) -> set[Site]:
    return {Site(r["kind"], r["path"], r["symbol"]) for r in rows}


def test_the_inventory_matches_the_tree_exactly():
    found, _fenced_ids = scan_tree()
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


def test_only_f3a_identities_import_the_fencing_primitives():
    """Scoped successor to F2's unconditional guard, on its OWN exact set.
    F3A_COLUMN_FILES is the wrong list here in both directions: it would permit a
    premature fencing import anywhere in F3a's files, and it would block F3b's
    ports, which live in neither set."""
    offenders = []
    for p in sorted(SRC.rglob("*.py")):
        if "__pycache__" in p.parts or p.name == "fencing.py":
            continue
        if str(p.relative_to(SRC)) in FENCING_IMPORT_FILES:
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
    assert not offenders, (
        "fencing primitives imported outside F3a's identities:\n  " + "\n  ".join(offenders)
    )


def test_only_f3a_identities_touch_claimed_at_generation():
    """Scoped successor to F2's unconditional guard, which forbade the column
    everywhere and therefore goes red the moment F3a's first claim writes it.
    The obligation it carried is real and is kept: a file outside F3a's
    identities reaching for the column is wiring F4-F9 early."""
    offenders = [
        str(p.relative_to(SRC))
        for p in sorted(SRC.rglob("*.py"))
        if "__pycache__" not in p.parts
        and str(p.relative_to(SRC)) not in F3A_COLUMN_FILES
        and "claimed_at_generation" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "claimed_at_generation outside F3a's identities:\n  " + "\n  ".join(offenders)
    )


def test_every_slice_in_the_inventory_is_known():
    """A row naming a slice SLICE_STATUS does not know is unreachable: no gate
    would ever require it to be fenced."""
    unknown = sorted({r["slice"] for r in load_inventory()} - set(SLICE_STATUS))
    assert not unknown, f"inventory names slices with no status: {unknown}"


def test_the_four_tool_originating_claims_are_f3a():
    """Analysis and setup build no ToolContext and originate no tool call, so
    they stay with their architecture slices. Pulling them into F3a would
    demand a generation for actors that never call a tool."""
    by_symbol = {r["symbol"]: r["slice"] for r in load_inventory() if r["kind"] == "claim_fn"}
    assert by_symbol == {
        "claim_next_coherence_job": "F3a",
        "claim_next_evaluator_job": "F3a",
        "claim_next_review_run": "F3a",
        "claim_next_uncited_claims_job": "F3a",
        "claim_queued_jobs": "F4",
        "claim_next_setup_job": "F5",
    }


def test_every_f3d_identity_is_still_in_the_inventory():
    """ANTI-VACUITY. The scan only records a tool_commit inside the effect scope —
    tools/ plus the declared RELOCATED_EFFECT_PATHS — and FencedSiteVanished only
    fires for rows already fenced=True, so moving an UNFENCED site to a path outside
    that scope deletes its row silently, and a slice can reach `complete` over an
    empty set.

    This is the ratchet that makes that impossible. If a row here has vanished, the
    fix is not to edit this set. Since F3d3a it is: declare the destination in
    RELOCATED_EFFECT_PATHS and carry the row across with a one-shot RENAMES entry, in
    the SAME commit as the code.

    Do NOT flip the row `fenced: true` to make it survive the move. That was the
    pre-F3d3a workaround and it is now a lie the inventory cannot detect — `fenced`
    became a MEASUREMENT in F3d2 (port call AND no raw commit), so asserting it by
    hand over a site that still commits directly is exactly the false claim the
    measurement exists to prevent.
    """
    present = {(r["slice"], r["kind"], r["symbol"]) for r in load_inventory()}
    missing = sorted(F3D_IDENTITIES - present)
    assert not missing, (
        "F3d identities that disappeared from the inventory:\n  "
        + "\n  ".join(f"{s} {k} {sym}" for s, k, sym in missing)
        + "\n\nA relocated site is not a converted site. Flip the row fenced=true "
        "in the SAME commit that moves the code, or before it."
    )


def test_a_complete_f3d_slice_has_every_one_of_its_identities_fenced():
    """`complete` must mean every identity the slice owns is proven, not merely that
    nothing red is left in the scan.

    Known and accepted: this is not an independent falsifier. Its red set is a subset
    of the union of test_every_f3d_identity_is_still_in_the_inventory (row relocated)
    and test_each_slice_matches_its_declared_status (row present but unfenced). It is
    kept for the message it produces, which names the identity rather than the row.
    """
    by_id = {(r["slice"], r["kind"], r["symbol"]): r for r in load_inventory()}
    unproven = []
    for identity in sorted(F3D_IDENTITIES):
        slice_name = identity[0]
        if SLICE_STATUS.get(slice_name) != "complete":
            continue
        row = by_id.get(identity)
        if row is None or not row.get("fenced"):
            unproven.append(identity)
    assert not unproven, (
        "slices marked complete with identities that are not fenced:\n  "
        + "\n  ".join(f"{s} {k} {sym}" for s, k, sym in unproven)
    )


def test_a_fully_converted_site_is_measured_as_fenced():
    """`fenced` stops being a box a human ticks. Before this, merge() copied the
    stored boolean forward and nothing checked it against the code — a slice could
    be marked complete with every row hand-flipped and nothing converted."""
    import pathlib

    from tests.fencing.detectors import find_fenced_tool_commits

    src = (
        pathlib.Path(__file__).parent / "fixtures" / "converted_site.py"
    ).read_text(encoding="utf-8")
    path = "src/call_manager/proposal_assist/tools/scorecard.py"
    assert find_fenced_tool_commits(src, path) == {(path, "submit_scorecard_impl")}


def test_a_half_converted_site_is_not_fenced():
    """Port call AND a residual raw commit — a PARTIAL conversion. Nothing in the
    tree has this shape after F3d2, which is exactly why it needs a fixture: the
    rule has to refuse it before a future slice produces one and reports itself
    complete over code that still commits directly."""
    import pathlib

    from tests.fencing.detectors import find_fenced_tool_commits, find_sites

    src = (
        pathlib.Path(__file__).parent / "fixtures" / "half_converted_site.py"
    ).read_text(encoding="utf-8")
    path = "src/call_manager/proposal_assist/tools/references.py"
    # The row still EXISTS — it is a write site either way.
    assert any(s.symbol == "fetch_reference_impl" for s in find_sites(src, path))
    # It is simply not fenced.
    assert find_fenced_tool_commits(src, path) == set()


def test_the_inventory_fenced_flags_match_the_tree():
    """The gate this task exists to arm. Every tool_commit row's `fenced` value is
    now recomputed from the source on every regeneration, so a hand-edit does not
    survive and `complete` cannot be claimed over unconverted code."""
    from tests.fencing.inventory import load_inventory, scan_tree

    _sites, fenced_ids = scan_tree()
    problems = [
        f"{row['path']}::{row['symbol']} says fenced={row['fenced']}"
        for row in load_inventory()
        if row["kind"] == "tool_commit"
        and row["fenced"] is not ((row["path"], row["symbol"]) in fenced_ids)
    ]
    assert not problems, (
        "tool_commit rows whose fenced flag disagrees with the code:\n  "
        + "\n  ".join(problems)
        + "\n\nRegenerate the inventory; do not edit the flag by hand."
    )


def test_non_tool_commit_rows_keep_their_stored_flag():
    """Scoping, tested against merge() ITSELF rather than against the file.

    Reading load_inventory() here would prove nothing: it reports what the JSON
    says, and the JSON says F3a is fenced because a human wrote that. The claim
    under test is about the CODE — that merge() applies the measured flag only to
    tool_commit rows — so drive merge() with a fenced_ids set that would flip an
    F3a row if the kind check were missing.
    """
    previous = load_inventory()
    f3a = [r for r in previous if r["slice"] == "F3a"]
    assert len(f3a) == 9
    assert all(r["kind"] in {"claim_fn", "tool_context"} for r in f3a)

    found, _measured = scan_tree()
    # Deliberately EMPTY: nothing is measured as port-only. A merge() that ignored
    # `kind` would therefore unfence every F3a row.
    merged = merge(previous, found, None, set())
    by_id = {(r["kind"], r["path"], r["symbol"]): r for r in merged}
    for row in f3a:
        assert by_id[(row["kind"], row["path"], row["symbol"])]["fenced"] is True, row


def test_a_tool_commit_row_does_not_keep_its_stored_flag():
    """The other half of the same rule, and the reason the one above is not
    vacuous: for a tool_commit the stored value is DISCARDED and the measurement
    wins. Same empty fenced_ids, opposite outcome."""
    previous = load_inventory()
    found, _measured = scan_tree()
    merged = merge(previous, found, None, set())
    tool_commits = [r for r in merged if r["kind"] == "tool_commit"]
    assert tool_commits
    assert not any(r["fenced"] for r in tool_commits)


def test_the_four_converted_modules_are_no_longer_exempt():
    """The point of the slice, stated as a fact about the exemption list. These
    four import stdlib and ports only; the gate now enforces them with no pins."""
    from tests.fencing.inventory import TOOLS_NOT_YET_CONVERTED

    assert set(TOOLS_NOT_YET_CONVERTED) == {
        "audit.py",       # F3f — capability holders
        "citations.py",   # F3d3 — the citation core relocation
        "history.py",     # F3f — the repo_dir locator, and zero inventory rows
        "proposal.py",    # F3d3
        "references.py",  # F3e — the ingest half
    }


def test_f3d2_is_complete_and_every_row_it_owns_is_measured_fenced():
    """`complete` is now a measurable claim. Task 2 made `fenced` a function of the
    source, so this cannot be satisfied by editing the JSON."""
    from tests.fencing.inventory import SLICE_STATUS, load_inventory, scan_tree

    assert SLICE_STATUS["F3d2"] == "complete"
    _sites, fenced_ids = scan_tree()
    owned = [r for r in load_inventory() if r["slice"] == "F3d2"]
    assert len(owned) == 5
    for row in owned:
        assert row["fenced"] is True, row["symbol"]
        assert (row["path"], row["symbol"]) in fenced_ids, row["symbol"]


def test_the_ingest_row_is_untouched_and_still_f3e():
    """The boundary this slice must not cross. fetch_reference_impl._do_fetch_and_ingest
    is a SEPARATE nested identity (references.py:551) holding the ingest commit at
    :580. F3d2 converts the accounting around it and leaves it alone — which is also
    why fetch_reference_impl itself CAN go green here."""
    from tests.fencing.inventory import load_inventory

    row = next(
        r
        for r in load_inventory()
        if r["symbol"] == "fetch_reference_impl._do_fetch_and_ingest"
    )
    assert row["slice"] == "F3e"
    assert row["fenced"] is False
