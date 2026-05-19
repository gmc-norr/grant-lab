import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_monitor.db.models import Workspace
from tests.proposal_assist.conftest import make_call, make_user


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
        s.add(Workspace(
            title="Linked draft",
            owner_id=owner.id,
            call_id=call.id,
            deadline=datetime(2026, 12, 1, tzinfo=UTC),
        ))
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
