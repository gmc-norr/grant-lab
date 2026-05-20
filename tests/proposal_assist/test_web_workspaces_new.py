def test_get_new_form_renders(client_authed_owner):
    response = client_authed_owner.get("/workspaces/new")
    assert response.status_code == 200
    assert b"title" in response.content.lower()


def test_post_new_creates_workspace_and_redirects(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from sqlalchemy import select

    from call_manager.db.models import Workspace, WorkspaceEvent, WorkspaceSetupJob

    response = client_authed_owner.post(
        "/workspaces/new",
        data={"title": "Test Workspace"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"].startswith("/workspaces/")

    with pg_session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.title == "Test Workspace"))
        assert ws is not None
        assert ws.owner_id == current_user.id
        events = list(
            s.scalars(
                select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == ws.id),
            )
        )
        assert any(e.kind == "created" for e in events)
        setup = s.scalar(select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws.id))
        assert setup is not None
        assert setup.status == "queued"


def test_post_new_with_call_and_profile_links(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """Workspace links to call_id + profile_id when provided in the form."""
    from sqlalchemy import select

    from call_manager.db.models import Call, Profile, Source, Workspace

    with pg_session_factory() as s:
        src = Source(slug="t", name="T")
        s.add(src)
        s.flush()
        call = Call(
            source_id=src.id,
            external_id="c1",
            title="A call",
            url="http://x/c1",
        )
        s.add(call)
        prof = Profile(
            name="P",
            description="d",
            kind="group",
            created_by=current_user.id,
        )
        s.add(prof)
        s.commit()
        cid, pid = call.id, prof.id

    response = client_authed_owner.post(
        "/workspaces/new",
        data={"title": "Linked", "call_id": str(cid), "profile_id": str(pid)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    with pg_session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.title == "Linked"))
        assert ws.call_id == cid
        assert ws.profile_id == pid
