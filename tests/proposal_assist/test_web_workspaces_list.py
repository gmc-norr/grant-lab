def test_list_shows_owned_workspaces(client_authed_owner, pg_session_factory, current_user):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.title = "Mine"
        s.commit()
    response = client_authed_owner.get("/workspaces")
    assert response.status_code == 200
    assert b"Mine" in response.content


def test_list_shows_viewer_of_workspaces(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    """A workspace where I'm a viewer (not owner) shows up too."""
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        other_owner = make_user(s, email="other@ki.se")
        ws = make_workspace(s, owner=other_owner)
        ws.title = "Shared"
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
    response = client_authed_user.get("/workspaces")
    assert response.status_code == 200
    assert b"Shared" in response.content


def test_list_hides_other_users_workspaces(client_authed_user, pg_session_factory):
    """A workspace I'm neither owner nor viewer of does NOT appear."""
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        other = make_user(s, email="x@ki.se")
        ws = make_workspace(s, owner=other)
        ws.title = "NotMine"
        s.commit()
    response = client_authed_user.get("/workspaces")
    assert b"NotMine" not in response.content


def test_list_excludes_soft_deleted(client_authed_owner, pg_session_factory, current_user):
    from datetime import UTC, datetime

    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.title = "Deleted"
        ws.soft_deleted_at = datetime.now(UTC)
        s.commit()
    response = client_authed_owner.get("/workspaces")
    assert b"Deleted" not in response.content


def test_list_filter_by_status(client_authed_owner, pg_session_factory, current_user):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws_d = make_workspace(s, owner=current_user)
        ws_d.title = "Draft"
        ws_d.status = "drafting"
        ws_s = make_workspace(s, owner=current_user)
        ws_s.title = "Submitted"
        ws_s.status = "submitted"
        s.commit()
    response = client_authed_owner.get("/workspaces?status=submitted")
    assert b"Submitted" in response.content
    assert b"Draft" not in response.content
