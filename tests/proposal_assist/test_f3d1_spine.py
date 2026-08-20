"""F3d1 — the spine. Everything a converted tool will need, in place, with nothing
converted.

Spec 2026-08-18 revision 8 §7. This slice is additive by construction: if any test
here starts needing a tool module to have changed, the slice has exceeded its scope.
"""

from __future__ import annotations


def test_the_artifact_filenames_are_reachable_from_ports():
    """A converted tool may import only stdlib and ports, and it still has to name
    proposal.md. These constants are the reason ports/artifacts.py exists."""
    from call_manager.proposal_assist.ports.artifacts import (
        CITATIONS_FILENAME,
        PROPOSAL_FILENAME,
        SPEC_FILENAME,
    )

    assert PROPOSAL_FILENAME == "proposal.md"
    assert SPEC_FILENAME == "proposal-spec.md"
    assert CITATIONS_FILENAME == "citations.json"


def test_the_old_artifacts_module_re_exports_the_same_objects():
    """GC-4: one source of truth. Every existing importer keeps working, and the
    value it gets is the value ports defines — not a second copy that can drift."""
    from call_manager.proposal_assist import artifacts as old
    from call_manager.proposal_assist.ports import artifacts as new

    assert old.PROPOSAL_FILENAME is new.PROPOSAL_FILENAME
    assert old.SPEC_FILENAME is new.SPEC_FILENAME
    assert old.Artifact is new.Artifact
    assert old.filename_for is new.filename_for


def test_spec_skeleton_stays_out_of_ports():
    """Seeded page content is not a capability. Keeping it behind is what stops
    ports/ from becoming the place things go when nobody knows where they belong."""
    from call_manager.proposal_assist import artifacts as old
    from call_manager.proposal_assist.ports import artifacts as new

    assert hasattr(old, "SPEC_SKELETON")
    assert not hasattr(new, "SPEC_SKELETON")


def test_no_module_redefines_the_citations_filename_except_the_unconverted_tool():
    """CITATIONS_FILENAME was written out FOUR times: references/deletion.py:38,
    reconcile.py:36, editing/save.py:41 and tools/citations.py:80. Three now import
    it. editing/save.py is included even though the plan's narrative is about tools
    — it is the copy eight other modules import (export.py, human_cite.py,
    collab/kernel.py, analysis/insert.py, web/api/workspaces.py,
    web/api/citation_audit.py, problems/aggregate.py), so leaving it out would leave
    the widest-blast-radius literal unguarded. The fourth is tools/citations.py,
    which F3d3 converts — until then this test is what stops the copies drifting."""
    from call_manager.proposal_assist import reconcile
    from call_manager.proposal_assist.editing import save
    from call_manager.proposal_assist.ports.artifacts import CITATIONS_FILENAME
    from call_manager.proposal_assist.references import deletion
    from call_manager.proposal_assist.tools import citations

    assert reconcile.CITATIONS_FILENAME is CITATIONS_FILENAME
    assert deletion.CITATIONS_FILENAME is CITATIONS_FILENAME
    assert save.CITATIONS_FILENAME is CITATIONS_FILENAME
    assert citations.CITATIONS_FILENAME == CITATIONS_FILENAME
