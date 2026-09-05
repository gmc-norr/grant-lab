import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from grant_lab.db.models import Workspace, WorkspaceViewer
from tests.proposal_assist.conftest import make_call, make_user, make_workspace


def test_workspace_insert_minimum_fields(pg_session_factory):
    """Owner is the only required FK; call_id and profile_id are nullable."""
    with pg_session_factory() as s:
        owner = make_user(s)
        s.add(Workspace(title="My proposal", owner_id=owner.id))
        s.commit()
        ws = s.scalar(select(Workspace).where(Workspace.title == "My proposal"))
        assert ws is not None
        assert isinstance(ws.id, uuid.UUID)
        assert ws.owner_id == owner.id
        assert ws.call_id is None
        assert ws.profile_id is None
        assert ws.status == "drafting"
        assert ws.deadline is None
        assert ws.soft_deleted_at is None
        assert isinstance(ws.created_at, datetime)
        assert isinstance(ws.updated_at, datetime)


def test_workspace_optional_call_and_profile(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        call = make_call(s)
        s.commit()
        s.add(
            Workspace(
                title="Linked draft",
                owner_id=owner.id,
                call_id=call.id,
                deadline=datetime(2026, 12, 1, tzinfo=UTC),
            )
        )
        s.commit()
        ws = s.scalar(select(Workspace).where(Workspace.title == "Linked draft"))
        assert ws.call_id == call.id
        assert ws.deadline == datetime(2026, 12, 1, tzinfo=UTC)


def test_workspace_status_check_constraint(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        s.add(Workspace(title="bad", owner_id=owner.id, status="not_a_status"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_workspace_owner_fk_required(pg_session_factory):
    with pg_session_factory() as s:
        s.add(Workspace(title="no owner"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_workspace_viewer_composite_pk(pg_session_factory):
    """The (workspace_id, user_id) composite PK is enforced — inserting the
    same pair twice raises IntegrityError."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        viewer = make_user(s, email="viewer@umu.se")
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=viewer.id))
        s.commit()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=viewer.id))
        with pytest.raises(IntegrityError):
            s.commit()


def test_workspace_viewer_cascade_on_workspace_delete(pg_session_factory):
    """Deleting a workspace cascades to its viewers."""
    from sqlalchemy import delete

    with pg_session_factory() as s:
        ws = make_workspace(s)
        viewer = make_user(s, email="viewer2@umu.se")
        ws_id = ws.id
        viewer_id = viewer.id
        s.add(WorkspaceViewer(workspace_id=ws_id, user_id=viewer_id))
        s.commit()
        s.execute(delete(Workspace).where(Workspace.id == ws_id))
        s.commit()
        remaining = s.scalar(
            select(WorkspaceViewer).where(
                WorkspaceViewer.workspace_id == ws_id,
                WorkspaceViewer.user_id == viewer_id,
            )
        )
        assert remaining is None


def test_workspace_updated_at_bumped_on_modification(pg_session_factory):
    """onupdate=func.now() on Workspace.updated_at means a column UPDATE
    refreshes the timestamp. Matches the convention Profile/RankingJob
    already use; future status-change logic relies on this."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        original = ws.updated_at
        ws.title = "renamed"
        s.commit()
        s.refresh(ws)
        assert ws.updated_at > original


def test_workspace_phased_columns_have_safe_defaults(pg_session_factory):
    """Phased-column defaults let a brand-new workspace land via B1's
    create_workspace() without filling these out — Phase 0 populates
    them later."""
    with pg_session_factory() as s:
        owner = make_user(s)
        s.flush()
        ws = Workspace(title="Defaults check", owner_id=owner.id)
        s.add(ws)
        s.commit()
        s.refresh(ws)
        assert ws.current_phase == "setup"
        assert ws.call_body_snapshot == ""
        assert ws.profile_desc_snapshot == ""
        assert isinstance(ws.snapshot_refreshed_at, datetime)
        assert ws.rubric_criteria == []
        assert ws.rubric_funder_notes is None
        assert isinstance(ws.rubric_updated_at, datetime)
        assert ws.section_extracts == {}
        assert ws.review_runs_per_day_override is None
        assert ws.review_dollar_cap_override is None


def test_workspace_current_phase_check_constraint(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws.current_phase = "not_a_phase"
        with pytest.raises(IntegrityError):
            s.commit()
