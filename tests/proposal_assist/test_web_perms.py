import uuid

import pytest
from fastapi import HTTPException

from grant_lab.db.models import WorkspaceViewer
from grant_lab.proposal_assist.web._perms import require_member, require_owner
from tests.proposal_assist.conftest import make_user, make_workspace


def test_require_member_allows_owner(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s, email="o@umu.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        assert require_member(workspace_id=ws.id, user=owner, session=s).id == ws.id


def test_require_member_allows_viewer(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s, email="o2@umu.se")
        member = make_user(s, email="m2@umu.se")
        ws = make_workspace(s, owner=owner)
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=member.id))
        s.commit()
        assert require_member(workspace_id=ws.id, user=member, session=s).id == ws.id


def test_require_member_rejects_stranger(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s, email="o3@umu.se")
        stranger = make_user(s, email="s3@umu.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        with pytest.raises(HTTPException) as ei:
            require_member(workspace_id=ws.id, user=stranger, session=s)
        assert ei.value.status_code == 403


def test_require_owner_rejects_member(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s, email="o4@umu.se")
        member = make_user(s, email="m4@umu.se")
        ws = make_workspace(s, owner=owner)
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=member.id))
        s.commit()
        with pytest.raises(HTTPException) as ei:
            require_owner(workspace_id=ws.id, user=member, session=s)
        assert ei.value.status_code == 403


def test_require_member_missing_workspace_404(pg_session_factory):
    with pg_session_factory() as s:
        u = make_user(s, email="u5@umu.se")
        s.commit()
        with pytest.raises(HTTPException) as ei:
            require_member(workspace_id=uuid.uuid4(), user=u, session=s)
        assert ei.value.status_code == 404
