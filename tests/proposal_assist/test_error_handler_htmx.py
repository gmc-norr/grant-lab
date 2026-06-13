def test_htmx_partial_gets_json_detail_even_with_accept_html(  # noqa: E501
    monkeypatch, pg_url, pg_session_factory
):
    """A 409 raised by post_turn during Phase-0 setup must reach app.js as JSON
    {"detail": ...} for an htmx partial request, even when the browser also
    sends Accept: text/html (so the global htmx:responseError handler renders
    the detail inline rather than swapping an HTML error page)."""
    from fastapi.testclient import TestClient

    from call_manager.db.models import User, Workspace
    from call_manager.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()

    with pg_session_factory() as s:
        owner = User(email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T", current_phase="setup")  # Phase-0 → 409 gate
        s.add(ws)
        s.commit()
        uid, wsid = owner.id, ws.id

    client = TestClient(app)
    client.__enter__()
    try:
        client.cookies.set("session", app.state.session_signer.make(user_id=uid))
        headers = {"HX-Request": "true", "Accept": "text/html"}
        resp = client.post(f"/workspaces/{wsid}/turns", data={"content": "hi"}, headers=headers)
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 409
    assert resp.headers["content-type"].startswith("application/json")
    assert isinstance(resp.json()["detail"], str)


def test_plain_browser_navigation_still_gets_html_error_page(  # noqa: E501
    monkeypatch, pg_url, pg_session_factory
):
    from fastapi.testclient import TestClient

    from call_manager.db.models import User, Workspace
    from call_manager.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()

    with pg_session_factory() as s:
        owner = User(email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T", current_phase="setup")
        s.add(ws)
        s.commit()
        uid, wsid = owner.id, ws.id

    client = TestClient(app)
    client.__enter__()
    try:
        client.cookies.set("session", app.state.session_signer.make(user_id=uid))
        resp = client.post(f"/workspaces/{wsid}/turns", data={"content": "hi"},
                           headers={"Accept": "text/html"})  # no HX-Request
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 409
    assert resp.headers["content-type"].startswith("text/html")
