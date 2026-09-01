from call_manager.db.models import ChatTurn, User, Workspace, WorkspaceEvent
from call_manager.main import create_app
from call_manager.proposal_assist.spend import record_spend


def _setup(monkeypatch, pg_url, *, drafting_enabled=True):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ENABLED", "true" if drafting_enabled else "false")
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "1000")
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_OUTPUT", "500")
    return create_app()


def _admin(app, sf, test_client_factory, *, email="root@ki.se"):
    with sf() as s:
        u = User(status="active", email=email, is_admin=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = test_client_factory(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c


def test_drafting_requires_admin(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="alice@ki.se", is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = test_client_factory(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    assert c.get("/api/admin/drafting").status_code == 403


def test_drafting_kill_switch_and_cap(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _setup(monkeypatch, pg_url, drafting_enabled=True)
    c = _admin(app, pg_session_factory, test_client_factory)
    body = c.get("/api/admin/drafting").json()
    assert body["drafting_enabled"] is True
    assert body["global_cap"]["cap_in"] == 1000
    assert body["global_cap"]["cap_out"] == 500
    assert body["global_cap"]["allowed"] is True


def test_drafting_kill_switch_off(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _setup(monkeypatch, pg_url, drafting_enabled=False)
    c = _admin(app, pg_session_factory, test_client_factory)
    assert c.get("/api/admin/drafting").json()["drafting_enabled"] is False


def test_drafting_per_workspace_usage(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="VisibleInPanel")
        s.add(ws)
        s.flush()
        s.add(
            ChatTurn(
                workspace_id=ws.id,
                role="assistant",
                content={"type": "text", "text": "x"},
                tokens_in=42,
                tokens_out=7,
            )
        )
        s.commit()
    c = _admin(app, pg_session_factory, test_client_factory)
    rows = c.get("/api/admin/drafting").json()["per_workspace"]
    row = next(r for r in rows if r["title"] == "VisibleInPanel")
    assert row["owner_email"] == "owner@ki.se"
    assert row["used_in"] == 42 and row["used_out"] == 7
    assert isinstance(row["id"], str)  # UUID serialized as string for /cockpit/:id links


def test_drafting_per_workspace_desc_by_used_in(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = User(status="active", email="o@ki.se")
        s.add(owner)
        s.flush()
        small = Workspace(owner_id=owner.id, title="Small")
        big = Workspace(owner_id=owner.id, title="Big")
        s.add_all([small, big])
        s.flush()
        s.add(
            ChatTurn(
                workspace_id=small.id,
                role="assistant",
                content={"type": "text", "text": "x"},
                tokens_in=5,
            )
        )
        s.add(
            ChatTurn(
                workspace_id=big.id,
                role="assistant",
                content={"type": "text", "text": "x"},
                tokens_in=500,
            )
        )
        s.commit()
    c = _admin(app, pg_session_factory, test_client_factory)
    titles = [r["title"] for r in c.get("/api/admin/drafting").json()["per_workspace"]]
    assert titles.index("Big") < titles.index("Small")  # used_in desc


def test_drafting_recent_failed_events(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = User(status="active", email="o@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="WithFails")
        s.add(ws)
        s.flush()
        s.add(
            WorkspaceEvent(
                workspace_id=ws.id,
                kind="cloud_turn_failed",
                actor_id=owner.id,
                payload={"error": "distinctive_test_error"},
            )
        )
        s.commit()
    c = _admin(app, pg_session_factory, test_client_factory)
    events = c.get("/api/admin/drafting").json()["recent_events"]
    assert any(
        e["kind"] == "cloud_turn_failed" and e["payload"]["error"] == "distinctive_test_error"
        for e in events
    )


def test_drafting_cap_exceeded_reason(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = User(status="active", email="o@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="OverCapWs")
        s.add(ws)
        s.flush()
        # The global cap reads the llm_spend ledger now, so seed the 1500 there
        # rather than as a raw ChatTurn. cap_in=1000.
        record_spend(
            s,
            workspace_id=ws.id,
            kind="chat_turn",
            status="ok",
            tokens_in=1500,
        )
        s.commit()
    c = _admin(app, pg_session_factory, test_client_factory)
    cap = c.get("/api/admin/drafting").json()["global_cap"]
    assert cap["allowed"] is False
    assert cap["reason"] == "input_cap_exceeded"
