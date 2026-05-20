import uuid
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_monitor.db.models import PhaseApproval
from tests.proposal_assist.conftest import make_user, make_workspace


def test_phase_approval_insert(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        approver = make_user(s, email="approver@ki.se")
        s.add(
            PhaseApproval(
                workspace_id=ws.id,
                phase="spec",
                approving_user_id=approver.id,
                approved_artifact_sha="a" * 40,
            )
        )
        s.commit()
        pa = s.scalar(select(PhaseApproval).where(PhaseApproval.workspace_id == ws.id))
        assert isinstance(pa.id, uuid.UUID)
        assert pa.phase == "spec"
        assert pa.approving_user_id == approver.id
        assert pa.approved_artifact_sha == "a" * 40
        assert isinstance(pa.approved_at, datetime)


def test_phase_approval_phase_check_constraint(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        approver = make_user(s, email="approver2@ki.se")
        s.add(
            PhaseApproval(
                workspace_id=ws.id,
                phase="not_a_phase",
                approving_user_id=approver.id,
                approved_artifact_sha="b" * 40,
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


def test_phase_approval_cascade_on_workspace_delete(pg_session_factory):
    from sqlalchemy import delete
    from call_monitor.db.models import Workspace

    with pg_session_factory() as s:
        ws = make_workspace(s)
        approver = make_user(s, email="approver3@ki.se")
        s.add(
            PhaseApproval(
                workspace_id=ws.id,
                phase="plan",
                approving_user_id=approver.id,
                approved_artifact_sha="c" * 40,
            )
        )
        s.commit()
        ws_id = ws.id
        s.execute(delete(Workspace).where(Workspace.id == ws_id))
        s.commit()
        assert s.scalar(select(PhaseApproval).where(PhaseApproval.workspace_id == ws_id)) is None
