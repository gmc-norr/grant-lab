def test_get_settings_renders_for_owner(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.title = "Original"
        s.commit()
        wsid = ws.id
    response = client_authed_owner.get(f"/workspaces/{wsid}/settings")
    assert response.status_code == 200
    assert b"Original" in response.content
    assert b"<form" in response.content


def test_get_settings_rejects_non_owner(
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
    response = client_authed_user.get(f"/workspaces/{wsid}/settings")
    assert response.status_code == 403


def test_post_settings_updates_title_status_deadline(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from sqlalchemy import select

    from call_manager.db.models import Workspace
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/settings",
        data={
            "title": "Updated title",
            "status": "submitted",
            "deadline": "2026-12-31",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"] == f"/workspaces/{wsid}"

    with pg_session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.id == wsid))
    assert ws.title == "Updated title"
    assert ws.status == "submitted"
    assert ws.deadline is not None
    assert ws.deadline.year == 2026


def test_soft_delete_redirects_and_excludes_from_list(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from sqlalchemy import select

    from call_manager.db.models import Workspace
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.title = "ToDelete"
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/delete",
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"] == "/workspaces"

    with pg_session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.id == wsid))
    assert ws.soft_deleted_at is not None

    # Verify list page no longer shows it.
    list_resp = client_authed_owner.get("/workspaces")
    assert b"ToDelete" not in list_resp.content


def test_post_settings_rejects_non_owner(
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
        f"/workspaces/{wsid}/settings",
        data={"title": "x", "status": "drafting"},
    )
    assert response.status_code == 403
