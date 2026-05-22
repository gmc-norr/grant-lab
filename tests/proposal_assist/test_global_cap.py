"""Global daily cap: when ALL workspaces' today usage sums above the
global ceiling, no new submit is accepted regardless of per-workspace
remaining budget. The cap is a guardrail against runaway org-wide
spend; small overshoot from concurrent submits is acceptable."""

from datetime import UTC, datetime, timedelta

import pytest

from call_manager.config import Settings
from call_manager.db.models import ChatTurn
from call_manager.proposal_assist.turns.caps import GlobalTokenCapExceeded
from call_manager.proposal_assist.turns.submit import submit_user_turn
from tests.proposal_assist.conftest import make_user, make_workspace


def test_check_global_cap_sums_across_workspaces(pg_session_factory, _settings_env, monkeypatch):
    """Two workspaces' assistant rows from today must both count
    toward the global sum returned by check_global_daily_token_cap."""
    from call_manager.proposal_assist.turns.caps import check_global_daily_token_cap

    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "1000")
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_OUTPUT", "1000")
    settings = Settings()

    with pg_session_factory() as s:
        owner = make_user(s)
        s.commit()
        ws_a = make_workspace(s, owner=owner, title="A")
        ws_b = make_workspace(s, owner=owner, title="B")
        s.commit()
        s.add_all(
            [
                ChatTurn(
                    workspace_id=ws_a.id,
                    role="assistant",
                    content={"type": "text", "text": "x"},
                    tokens_in=400,
                    tokens_out=10,
                ),
                ChatTurn(
                    workspace_id=ws_b.id,
                    role="assistant",
                    content={"type": "text", "text": "y"},
                    tokens_in=300,
                    tokens_out=20,
                ),
            ]
        )
        s.commit()

    with pg_session_factory() as s:
        result = check_global_daily_token_cap(s, settings=settings)
    assert result.used_in == 700
    assert result.used_out == 30
    assert result.allowed is True


def test_submit_raises_global_cap_exceeded(pg_session_factory, _settings_env, monkeypatch):
    """When the global sum is already at/above the cap, a submit on
    a workspace with PLENTY of per-workspace headroom still fails."""
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "500")
    # Per-workspace stays at default (500_000) — way above what we'll seed.
    settings = Settings()

    with pg_session_factory() as s:
        owner = make_user(s)
        s.commit()
        ws_a = make_workspace(s, owner=owner, title="A")
        ws_b = make_workspace(s, owner=owner, title="B")
        s.commit()
        # Seed: ws_a already used 500 input today via a completed turn.
        s.add(
            ChatTurn(
                workspace_id=ws_a.id,
                role="assistant",
                content={"type": "text", "text": "x"},
                tokens_in=500,
                created_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        s.commit()
        wsid_b, owner_id = ws_b.id, owner.id

    # Submit on ws_b should fail with GlobalTokenCapExceeded even
    # though ws_b's per-workspace usage is 0.
    with pg_session_factory() as s, pytest.raises(GlobalTokenCapExceeded):
        submit_user_turn(
            session=s,
            workspace_id=wsid_b,
            content_text="hi",
            submitted_by_user_id=owner_id,
            settings=settings,
        )


def test_post_turn_returns_429_with_global_header(monkeypatch, pg_url, pg_session_factory):
    """The HTTP layer surfaces global cap exhaustion as 429 with the
    X-Token-Cap: global header so the UI can distinguish it from a
    per-workspace 429."""

    from fastapi.testclient import TestClient

    from call_manager.auth.session_cookie import SessionSigner
    from call_manager.db.models import User, Workspace
    from call_manager.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "100")
    app = create_app()

    with pg_session_factory() as s:
        owner = User(email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="Test")
        s.add(ws)
        s.flush()
        s.add(
            ChatTurn(
                workspace_id=ws.id,
                role="assistant",
                content={"type": "text", "text": "x"},
                tokens_in=150,  # already over the 100 global cap
            )
        )
        s.commit()
        uid, wsid = owner.id, ws.id

    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    try:
        signer: SessionSigner = app.state.session_signer
        client.cookies.set("session", signer.make(user_id=uid))
        resp = client.post(f"/workspaces/{wsid}/turns", data={"content": "hi"})
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 429
    assert resp.headers.get("X-Token-Cap") == "global"
    assert "global" in resp.text.lower()


def test_global_cap_wins_when_per_workspace_also_exceeded(monkeypatch, pg_url, pg_session_factory):
    """When BOTH global and per-workspace caps are exceeded, the
    global check runs first and raises GlobalTokenCapExceeded. The
    web layer surfaces the X-Token-Cap: global header so the UI can
    distinguish from a per-workspace 429."""
    from fastapi.testclient import TestClient

    from call_manager.auth.session_cookie import SessionSigner
    from call_manager.db.models import ChatTurn, User, Workspace
    from call_manager.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "100")
    monkeypatch.setenv("DAILY_TOKEN_CAP_INPUT", "100")
    app = create_app()

    with pg_session_factory() as s:
        owner = User(email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T")
        s.add(ws)
        s.flush()
        # Seed: 200 input tokens used today (over BOTH caps).
        s.add(
            ChatTurn(
                workspace_id=ws.id,
                role="assistant",
                content={"type": "text", "text": "x"},
                tokens_in=200,
            )
        )
        s.commit()
        uid, wsid = owner.id, ws.id

    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    try:
        signer: SessionSigner = app.state.session_signer
        client.cookies.set("session", signer.make(user_id=uid))
        resp = client.post(f"/workspaces/{wsid}/turns", data={"content": "hi"})
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 429
    # GLOBAL wins because it's checked first in submit_user_turn.
    assert resp.headers.get("x-token-cap") == "global"
