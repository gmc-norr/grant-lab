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


def test_the_busy_error_is_reachable_from_ports():
    """Every Effects method can raise this — the shared lease has a 10s cap. A
    converted tool that cannot NAME it has to let it reach _safe_tool's catch-all,
    which turns "busy, retry" into "internal error"."""
    from call_manager.proposal_assist.ports.errors import (
        BUSY_ERROR_MESSAGE,
        CommitMutexTimeout,
    )

    assert issubclass(CommitMutexTimeout, RuntimeError)
    assert BUSY_ERROR_MESSAGE == "workspace is busy — try again in a moment"


def test_locking_re_exports_the_identical_error_class():
    """GC-4, and it is load-bearing here: collab/test_collab_retire_mutex.py asserts
    membership of this class in a set of clean errors. Two classes that merely look
    alike would pass every equality test in the tree and fail that one."""
    from call_manager.proposal_assist.drafting import locking
    from call_manager.proposal_assist.ports import errors

    assert locking.CommitMutexTimeout is errors.CommitMutexTimeout
    assert locking.BUSY_ERROR_MESSAGE is errors.BUSY_ERROR_MESSAGE
    assert locking.DELETED_ERROR_MESSAGE is errors.DELETED_ERROR_MESSAGE


def test_an_existing_raise_site_is_still_caught_by_the_ports_class():
    """The relocation must not fork the taxonomy: something raised through the old
    import path is catchable through the new one."""
    from call_manager.proposal_assist.drafting.locking import CommitMutexTimeout as Old
    from call_manager.proposal_assist.ports.errors import CommitMutexTimeout as New

    try:
        raise Old("busy")
    except New as exc:
        assert str(exc) == "busy"


def test_invocation_is_reachable_from_ports():
    """GC-2, the reason this slice exists. Every exempt tool module pins the
    tools.registry import, a converted impl needs Invocation in its signature, and
    `from __future__ import annotations` does not save you — ruff reports F821 for a
    lazily-annotated name with no import. So the type must live in ports."""
    from call_manager.proposal_assist.ports.invocation import Invocation

    assert hasattr(Invocation, "stamp")


def test_the_snapshot_factory_stays_with_the_context_it_reads():
    """ports/ is stdlib-only, so nothing in it may name ToolContext. The factory
    belongs beside its only production caller, _safe_tool."""
    from call_manager.proposal_assist.ports.invocation import Invocation
    from call_manager.proposal_assist.tools.registry import snapshot_invocation

    assert not hasattr(Invocation, "snapshot")
    assert callable(snapshot_invocation)


def test_registry_still_exports_invocation_as_the_same_class():
    """GC-4. Two test files and any future reader import it from registry."""
    from call_manager.proposal_assist.ports.invocation import Invocation as FromPorts
    from call_manager.proposal_assist.tools.registry import Invocation as FromRegistry

    assert FromPorts is FromRegistry


def test_a_snapshot_still_stamps_and_still_refuses_an_unfenced_actor():
    """The behaviour F3a and F3b pinned survives the move: generation 0 is REAL, and
    a missing generation raises rather than defaulting."""
    import uuid

    import pytest

    from call_manager.proposal_assist.ports.errors import UnfencedInvocation
    from call_manager.proposal_assist.tools.registry import (
        ToolContext,
        snapshot_invocation,
    )

    ws = uuid.uuid4()

    def _ctx(generation):
        return ToolContext(
            workspace_id=ws,
            repo_dir=None,
            session_factory=None,
            settings=None,
            lifecycle_generation=generation,
        )

    assert snapshot_invocation(_ctx(0)).stamp().generation == 0
    with pytest.raises(UnfencedInvocation):
        snapshot_invocation(_ctx(None)).stamp()
