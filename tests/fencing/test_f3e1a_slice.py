"""Fence F3e1a — what this slice did, and what it deliberately did not."""

from __future__ import annotations


def test_the_carrier_has_no_production_callers():
    """F3e1a ships the carrier; F3e1b uses it. A caller here means a conversion
    leaked into the spine slice.

    AST, not a regex: `def stamp_for_workspace(` matches a name regex, so a
    text scan would report the DEFINITION as its own caller and could never pass.
    Alias-resolving too, since `import ... as` would evade a bare name match.
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "src"
    hits = []
    for f in src.rglob("*.py"):
        if "__pycache__" in f.parts:
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        names = {"stamp_for_workspace"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names |= {a.asname for a in node.names
                          if a.name == "stamp_for_workspace" and a.asname}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if called in names:
                    hits.append(f.relative_to(src).as_posix())
    assert hits == [], f"F3e1a converts nothing, but these call the carrier: {hits}"


def test_no_git_site_is_fenced_yet():
    from tests.fencing.inventory import load_inventory

    git = [r for r in load_inventory() if r["kind"] == "git_commit"]
    assert len(git) == 9
    assert not any(r["fenced"] for r in git)


def test_every_git_row_is_classified_and_allocated():
    from tests.fencing.inventory import UNCLASSIFIED, load_inventory

    for row in load_inventory():
        if row["kind"] == "git_commit":
            assert row["category"] == "semantic", row
            assert row["slice"] != UNCLASSIFIED, row


def test_the_pins_did_not_move():
    """F3e1a retires no exemption. Four modules are held by repo_dir and
    references.py by session_factory; both retire in F3f."""
    from tests.fencing.inventory import TOOLS_NOT_YET_CONVERTED as T

    assert len(T) == 5
    assert sum(any("repo_dir" in v for v in vs) for vs in T.values()) == 4
    assert any("session_factory" in v for v in T["references.py"])


def test_unclassified_default_refuses_a_new_git_site():
    """A newly discovered git site lands UNCLASSIFIED and the slice gate refuses it.

    We synthesise a Site rather than editing src/ — the whole point is to drive
    merge() with a brand-new identity that has no previous inventory entry, and
    then confirm the same rule that `test_each_slice_matches_its_declared_status`
    applies would reject it: UNCLASSIFIED has no entry in SLICE_STATUS, so
    `status in {"pending", "complete"}` is False.
    """
    from tests.fencing.detectors import KIND_GIT_COMMIT, Site
    from tests.fencing.inventory import (
        SLICE_STATUS,
        UNCLASSIFIED,
        load_inventory,
        merge,
        scan_tree,
    )

    synthetic = Site(KIND_GIT_COMMIT, "src/x.py", "f")
    previous = load_inventory()
    # scan_tree() returns (found_sites, fenced_ids); add the synthetic site to
    # the real scan so merge() doesn't think previously-fenced identities vanished.
    found_sites, fenced_ids = scan_tree()
    rows = merge(previous, found_sites | {synthetic}, None, fenced_ids)

    # Find the row the synthetic site produced.
    new_row = next(r for r in rows if r["path"] == "src/x.py" and r["symbol"] == "f")

    # A new git site must land UNCLASSIFIED — not silently allocated.
    assert new_row["slice"] == UNCLASSIFIED, (
        f"expected UNCLASSIFIED, got {new_row['slice']!r}"
    )

    # The same rule test_each_slice_matches_its_declared_status applies: UNCLASSIFIED
    # has no entry in SLICE_STATUS, so status is None — not "pending" or "complete".
    # The gate would therefore reject this row.
    status = SLICE_STATUS.get(new_row["slice"])
    assert status not in {"pending", "complete"}, (
        f"UNCLASSIFIED unexpectedly appeared in SLICE_STATUS as {status!r}; "
        "the gate would accept this site without human review"
    )
