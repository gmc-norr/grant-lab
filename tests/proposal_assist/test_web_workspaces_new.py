from tests.proposal_assist.conftest import make_call, make_profile


def test_get_new_form_renders_required_call_picker(client_authed_owner, pg_session_factory):
    """The form offers a *required* <select> of real calls (by title) — not a
    free-text numeric input, so an invalid call can't be entered from the UI."""
    with pg_session_factory() as s:
        call = make_call(s, external_id="pick-1")
        call.title = "Pickable Call"
        s.commit()

    response = client_authed_owner.get("/workspaces/new")
    assert response.status_code == 200
    body = response.text
    assert '<select name="call_id" required>' in body  # call is required
    assert "Pickable Call" in body  # rendered as an option
    assert 'type="number"' not in body  # no free-text ID inputs anymore


def test_post_new_creates_workspace_and_redirects(
    client_authed_owner, pg_session_factory, current_user
):
    """Happy path: a valid call_id creates the workspace + audit + setup job."""
    from sqlalchemy import select

    from call_manager.db.models import Workspace, WorkspaceEvent, WorkspaceSetupJob

    with pg_session_factory() as s:
        call = make_call(s, external_id="happy-1")
        s.commit()
        cid = call.id

    response = client_authed_owner.post(
        "/workspaces/new",
        data={"title": "Test Workspace", "call_id": str(cid)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"].startswith("/workspaces/")

    with pg_session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.title == "Test Workspace"))
        assert ws is not None
        assert ws.owner_id == current_user.id
        assert ws.call_id == cid
        events = list(s.scalars(select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == ws.id)))
        assert any(e.kind == "created" for e in events)
        setup = s.scalar(select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws.id))
        assert setup is not None
        assert setup.status == "queued"


def test_post_new_missing_call_id_rejected(client_authed_owner, pg_session_factory):
    """Phase 0 requires a call, so a workspace can't be created without one.
    Before: the form treated call as optional and the workspace silently died
    in setup (`RuntimeError: ... has no call_id`)."""
    from sqlalchemy import select

    from call_manager.db.models import Workspace

    response = client_authed_owner.post(
        "/workspaces/new", data={"title": "No Call"}, follow_redirects=False
    )
    assert response.status_code == 400
    with pg_session_factory() as s:
        assert s.scalar(select(Workspace).where(Workspace.title == "No Call")) is None


def test_post_new_nonexistent_call_id_is_400_not_500(client_authed_owner, pg_session_factory):
    """A bad call_id is a clean 400 — not the raw FK-violation 500 we saw."""
    from sqlalchemy import select

    from call_manager.db.models import Workspace

    response = client_authed_owner.post(
        "/workspaces/new",
        data={"title": "Bad Call", "call_id": "14233414"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    with pg_session_factory() as s:
        assert s.scalar(select(Workspace).where(Workspace.title == "Bad Call")) is None


def test_post_new_nonexistent_profile_id_is_400_not_500(client_authed_owner, pg_session_factory):
    """A bad profile_id (with a valid call) is also a clean 400, not a 500."""
    from sqlalchemy import select

    from call_manager.db.models import Workspace

    with pg_session_factory() as s:
        call = make_call(s, external_id="badprof-1")
        s.commit()
        cid = call.id

    response = client_authed_owner.post(
        "/workspaces/new",
        data={"title": "Bad Profile", "call_id": str(cid), "profile_id": "32452345"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    with pg_session_factory() as s:
        assert s.scalar(select(Workspace).where(Workspace.title == "Bad Profile")) is None


def test_post_new_blank_profile_id_treated_as_none(client_authed_owner, pg_session_factory):
    """The profile picker's '— none —' option submits an empty string; that's
    treated as no profile, not a validation error."""
    from sqlalchemy import select

    from call_manager.db.models import Workspace

    with pg_session_factory() as s:
        call = make_call(s, external_id="blankprof-1")
        s.commit()
        cid = call.id

    response = client_authed_owner.post(
        "/workspaces/new",
        data={"title": "Blank Profile", "call_id": str(cid), "profile_id": ""},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    with pg_session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.title == "Blank Profile"))
        assert ws is not None
        assert ws.profile_id is None


def test_post_new_with_call_and_profile_links(
    client_authed_owner, pg_session_factory, current_user
):
    """Workspace links to call_id + profile_id when both are valid."""
    from sqlalchemy import select

    from call_manager.db.models import Workspace

    with pg_session_factory() as s:
        call = make_call(s, external_id="link-1")
        prof = make_profile(s, name="Linkable Profile")
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
