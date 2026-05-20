"""Tests for POST /workspaces/{id}/turns — B6 Task 6.

H1: response body contains the rendered user-turn row AND the SSE-connector div.
H2: user-supplied content is HTML-escaped before being embedded in the response.
"""

from __future__ import annotations


def test_post_turn_creates_turn_job(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from sqlalchemy import select

    from call_manager.db.models import TurnJob
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "Hello agent"},
    )
    assert response.status_code in (200, 201, 303)
    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
        assert job is not None
        assert job.status == "queued"
        assert job.submitted_by_user_id == current_user.id


def test_post_turn_response_includes_user_turn_and_sse_connector(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """H1 regression: response body contains both the rendered user turn
    AND the SSE-connector div (both appended to #chat-history)."""
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "Hello agent"},
    )
    assert response.status_code in (200, 201)
    body = response.content.decode()
    # User turn rendered.
    assert "Hello agent" in body
    assert "chat-turn" in body
    # SSE connector div present.
    assert "sse-connect=" in body
    assert "/turns/" in body and "/stream" in body
    assert "hx-ext='sse'" in body or 'hx-ext="sse"' in body
    assert "sse-close=" in body  # closes on `done` event


def test_post_turn_escapes_html_in_user_content(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """H2 regression: user-supplied content with HTML/script tags is
    escaped, not injected raw."""
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "<script>alert('xss')</script>"},
    )
    body = response.content.decode()
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


def test_post_turn_rejects_non_owner(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    """Viewers cannot send turns (spec: shared-read, single-write)."""
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
        f"/workspaces/{wsid}/turns",
        data={"content": "I shouldn't be able to do this"},
    )
    assert response.status_code == 403


def test_post_turn_returns_429_on_cap_exceeded(
    client_authed_owner,
    pg_session_factory,
    current_user,
    monkeypatch,
):
    """When TokenCapExceeded is raised by submit_user_turn, the handler
    returns 429 with a useful body."""
    # Override the cap directly on the cached settings instance (env-var
    # monkeypatch doesn't take effect after Settings() is constructed).
    client_authed_owner.app.state.settings.daily_token_cap_input = 100

    from tests.proposal_assist.conftest import make_chat_turn, make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        make_chat_turn(
            s, ws, role="assistant", content={"type": "text", "text": "x"}, tokens_in=200
        )
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "should fail"},
    )
    assert response.status_code == 429
    assert b"cap" in response.content.lower()
