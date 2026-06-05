"""B6 Task 11 — viewer management (add by email, remove, settings page listing)."""


def test_add_viewer_by_email(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from sqlalchemy import select

    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        target = make_user(s, email="viewer@ki.se")
        s.commit()
        wsid, target_id = ws.id, target.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/viewers",
        data={"email": "viewer@ki.se"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"] == f"/workspaces/{wsid}/settings"

    with pg_session_factory() as s:
        v = s.scalar(
            select(WorkspaceViewer).where(
                WorkspaceViewer.workspace_id == wsid,
                WorkspaceViewer.user_id == target_id,
            )
        )
    assert v is not None


def test_add_viewer_unknown_email_400(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/viewers",
        data={"email": "ghost@nowhere.com"},
    )
    assert response.status_code == 400
    # No user-enumeration oracle: the rejected email must NOT be echoed
    # back (a per-email "no user with email X" message would let an owner
    # probe which addresses have accounts).
    assert b"ghost@nowhere.com" not in response.content


def test_add_viewer_owner_as_viewer_is_noop(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """Adding the workspace OWNER as a viewer is a silent no-op: the owner
    already has full access, so no redundant WorkspaceViewer row is
    created. Same redirect as a successful add."""
    from sqlalchemy import select

    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/viewers",
        data={"email": current_user.email},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"] == f"/workspaces/{wsid}/settings"
    with pg_session_factory() as s:
        rows = list(s.scalars(select(WorkspaceViewer).where(WorkspaceViewer.workspace_id == wsid)))
    assert rows == []


def test_add_viewer_duplicate_is_idempotent(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """Adding a viewer who's already a viewer is a no-op (no IntegrityError)."""
    from sqlalchemy import select

    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        target = make_user(s, email="dup@ki.se")
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=target.id))
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/viewers",
        data={"email": "dup@ki.se"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    with pg_session_factory() as s:
        rows = list(s.scalars(select(WorkspaceViewer).where(WorkspaceViewer.workspace_id == wsid)))
    assert len(rows) == 1


def test_remove_viewer(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from sqlalchemy import select

    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        target = make_user(s, email="goaway@ki.se")
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=target.id))
        s.commit()
        wsid, target_id = ws.id, target.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/viewers/{target_id}/remove",
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    with pg_session_factory() as s:
        rows = list(s.scalars(select(WorkspaceViewer).where(WorkspaceViewer.workspace_id == wsid)))
    assert rows == []


def test_settings_lists_current_viewers(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """The GET /settings page renders the list of current viewers."""
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        target = make_user(s, email="listed@ki.se")
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=target.id))
        s.commit()
        wsid = ws.id

    response = client_authed_owner.get(f"/workspaces/{wsid}/settings")
    assert response.status_code == 200
    assert b"listed@ki.se" in response.content


def test_add_viewer_rejects_non_owner(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        owner = make_user(s, email="o@ki.se")
        ws = make_workspace(s, owner=owner)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        wsid = ws.id
    response = client_authed_user.post(
        f"/workspaces/{wsid}/viewers",
        data={"email": "x@ki.se"},
    )
    assert response.status_code == 403
