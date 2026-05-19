import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_monitor.db.models import Workspace, WorkspaceEvent
from call_monitor.workspaces import create_workspace
from tests.proposal_assist.conftest import make_call, make_user


def test_create_workspace_inserts_workspace_row(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        s.commit()
        ws = create_workspace(
            session=s,
            owner_id=owner.id,
            title="Cancer-biomarker proposal",
        )
        s.commit()

        fetched = s.get(Workspace, ws.id)
        assert fetched is not None
        assert fetched.title == "Cancer-biomarker proposal"
        assert fetched.owner_id == owner.id
        assert fetched.call_id is None
        assert fetched.profile_id is None
        assert fetched.status == "drafting"


def test_create_workspace_writes_created_audit_event(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        s.commit()
        ws = create_workspace(
            session=s,
            owner_id=owner.id,
            title="audit-test",
        )
        s.commit()

        events = s.scalars(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws.id,
                WorkspaceEvent.kind == "created",
            )
        ).all()
        assert len(events) == 1
        assert events[0].actor_id == owner.id
        assert events[0].payload["title"] == "audit-test"


def test_create_workspace_does_not_enqueue_phase0_setup_job(
    pg_session_factory,
):
    """B1 deliberately leaves Phase 0 enqueueing to Slice A Part 2, which
    owns the `workspace_setup_jobs` table. If a future revision adds the
    enqueue here without also pulling in WorkspaceSetupJob, this regression
    test will catch the resulting import/runtime error.

    Note: this test only proves the absence of the enqueue at B1's
    boundaries. It cannot import WorkspaceSetupJob (doesn't exist yet).
    Slice A Part 2 should add the positive test that asserts the row IS
    enqueued via its own helper / extended create_workspace.
    """
    with pg_session_factory() as s:
        owner = make_user(s)
        s.commit()
        ws = create_workspace(session=s, owner_id=owner.id, title="x")
        s.commit()

        # Only base-spec tables should have rows for this workspace:
        # workspaces (1) + workspace_events (1, the 'created' row).
        # If a future commit adds a WorkspaceSetupJob enqueue without
        # also moving that table into B1, the import in create_workspace
        # will fail at import time and this test will fail to collect.
        events = s.scalars(select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == ws.id)).all()
        assert len(events) == 1
        assert events[0].kind == "created"


def test_create_workspace_with_call_and_profile(pg_session_factory):
    """Optional call_id + profile_id propagate to the row. Slice A Part 2's
    Phase 0 worker uses these to know which call to snapshot."""
    with pg_session_factory() as s:
        owner = make_user(s)
        call = make_call(s)
        s.commit()
        ws = create_workspace(
            session=s,
            owner_id=owner.id,
            title="linked",
            call_id=call.id,
            profile_id=None,
        )
        s.commit()

        fetched = s.get(Workspace, ws.id)
        assert fetched.call_id == call.id


def test_create_workspace_raises_for_unknown_owner(pg_session_factory):
    """If owner_id doesn't exist, the workspace insert fails (FK).
    No partial state is left behind."""
    with pg_session_factory() as s:
        with pytest.raises(IntegrityError):
            create_workspace(
                session=s,
                owner_id=99999,
                title="bad",
            )
            s.commit()
        s.rollback()
        assert s.scalar(select(WorkspaceEvent)) is None
        assert s.scalar(select(Workspace)) is None
