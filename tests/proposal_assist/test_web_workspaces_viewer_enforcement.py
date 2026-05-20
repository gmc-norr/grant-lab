"""Viewer read-only enforcement regression (server-side AND client-side)
+ cloud banner smoke."""

from call_manager.db.models import WorkspaceViewer
from tests.proposal_assist.conftest import (
    make_chat_turn,
    make_user,
    make_workspace,
)


def _setup_viewer_workspace(pg_session_factory, current_user):
    """Create a workspace owned by someone else, with current_user as
    a viewer. Returns wsid."""
    with pg_session_factory() as s:
        owner = make_user(s, email=f"owner-{__import__('uuid').uuid4().hex[:6]}@ki.se")
        ws = make_workspace(s, owner=owner)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        return ws.id


def test_viewer_sees_chat_history(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    """A viewer can READ the workspace's chat history."""
    with pg_session_factory() as s:
        owner = make_user(s, email="vchat@ki.se")
        ws = make_workspace(s, owner=owner)
        make_chat_turn(s, ws, role="user", content={"type": "text", "text": "owner msg"})
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        wsid = ws.id
    response = client_authed_user.get(f"/workspaces/{wsid}")
    assert response.status_code == 200
    assert b"owner msg" in response.content


def test_viewer_does_not_see_composer_form(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    """The composer textarea + Send button are gated by `{% if is_owner %}`
    in workspace_main.html — verify a viewer's rendered page omits them."""
    wsid = _setup_viewer_workspace(pg_session_factory, current_user)
    response = client_authed_user.get(f"/workspaces/{wsid}")
    assert response.status_code == 200
    # The composer form has id="composer" — must NOT appear for viewers.
    assert b'id="composer"' not in response.content
    # The viewer-facing "read-only" notice SHOULD appear.
    assert b"read-only" in response.content.lower()


def test_viewer_post_turn_returns_403(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    """Server-side enforcement: even if a viewer bypasses the hidden
    composer (curl, etc.), POST /turns returns 403."""
    wsid = _setup_viewer_workspace(pg_session_factory, current_user)
    response = client_authed_user.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "I shouldn't be allowed to send this"},
    )
    assert response.status_code == 403


def test_cloud_banner_renders_on_main_view(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """The per-turn cloud-data banner is in the page chrome for the
    owner (and viewer) — verify the spec language appears."""
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id
    response = client_authed_owner.get(f"/workspaces/{wsid}")
    assert response.status_code == 200
    # The banner partial mentions "Anthropic" — same string is in the
    # disclosure modal but the banner is a SEPARATE element.
    # Check for the spec-mandated language pattern.
    assert b"sent to Anthropic" in response.content


def test_cloud_banner_renders_for_viewer_too(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    """Viewers see the banner too — they're seeing cloud-touched content."""
    wsid = _setup_viewer_workspace(pg_session_factory, current_user)
    response = client_authed_user.get(f"/workspaces/{wsid}")
    assert response.status_code == 200
    assert b"sent to Anthropic" in response.content
