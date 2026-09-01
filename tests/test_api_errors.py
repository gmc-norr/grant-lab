from call_manager.db.models import Profile, ProfileKind, User
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def _login(app, pg_session_factory, test_client_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="u@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="Grp",
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level="group",
            description="d",
            structured={},
            created_by=u.id,
        )
        s.add(p)
        s.commit()
        uid, pid = u.id, p.id
    c = test_client_factory(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid, pid


def test_api_unknown_path_returns_envelope_404(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    c = test_client_factory(_app(monkeypatch, pg_url))
    r = c.get("/api/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert set(body["error"]) >= {"code", "message"}
    assert body["error"]["code"] == "not_found"


def test_non_api_404_is_unchanged(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    c = test_client_factory(_app(monkeypatch, pg_url))
    r = c.get("/totally-unknown-html-path", headers={"accept": "application/json"})
    assert r.status_code == 404
    assert "error" not in r.json()


def test_api_500_is_sanitized_envelope(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    app = _app(monkeypatch, pg_url)

    @app.get("/api/_boom")
    def _boom():
        raise RuntimeError("secret internals")

    c = test_client_factory(app, raise_server_exceptions=False)
    r = c.get("/api/_boom")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal_error"
    assert "secret internals" not in r.text


def test_api_5xx_httpexception_detail_is_not_leaked(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    from fastapi import HTTPException

    app = _app(monkeypatch, pg_url)

    @app.get("/api/_unavailable")
    def _unavailable():
        raise HTTPException(status_code=503, detail="db unavailable: secret path /var/db")

    c = test_client_factory(app, raise_server_exceptions=False)
    r = c.get("/api/_unavailable")
    assert r.status_code == 503
    body = r.json()
    assert body["error"]["code"] == "internal_error"
    assert "secret path" not in r.text and "db unavailable" not in r.text


def test_api_4xx_httpexception_detail_is_echoed(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    from fastapi import HTTPException

    app = _app(monkeypatch, pg_url)

    @app.get("/api/_teapot")
    def _teapot():
        raise HTTPException(status_code=418, detail="i am a teapot")

    c = test_client_factory(app, raise_server_exceptions=False)
    r = c.get("/api/_teapot")
    assert r.status_code == 418
    body = r.json()
    assert body["error"]["code"] == "error"
    assert body["error"]["message"] == "i am a teapot"


def test_api_4xx_structured_httpexception_preserves_envelope(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    from fastapi import HTTPException

    app = _app(monkeypatch, pg_url)

    @app.get("/api/_structured-conflict")
    def _structured_conflict():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "custom_conflict",
                "message": "resolve the conflict",
                "detail": {"field": "draft"},
            },
            headers={"X-Conflict": "draft"},
        )

    c = test_client_factory(app, raise_server_exceptions=False)
    r = c.get("/api/_structured-conflict")
    assert r.status_code == 409
    assert r.headers["X-Conflict"] == "draft"
    assert r.json() == {
        "error": {
            "code": "custom_conflict",
            "message": "resolve the conflict",
            "detail": {"field": "draft"},
        }
    }


def test_non_api_500_is_plain_text(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _app(monkeypatch, pg_url)

    @app.get("/_boom")
    def _boom():
        raise RuntimeError("secret internals")

    c = test_client_factory(app, raise_server_exceptions=False)
    r = c.get("/_boom")
    assert r.status_code == 500
    assert r.text == "Internal Server Error"
    assert "error" not in r.headers.get("content-type", "")


def test_api_csrf_rejection_is_envelope(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    c = test_client_factory(_app(monkeypatch, pg_url))
    # A cross-origin POST to an /api path: bad Origin → CSRF 403.
    # CSRF middleware runs before routing, so the route need not exist yet —
    # a bad Origin is rejected regardless.
    r = c.post("/api/me/default-profile", headers={"origin": "http://evil.example"}, json={})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_api_405_is_envelope_with_allow_header(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    c = test_client_factory(_app(monkeypatch, pg_url))
    # /api/me is GET-only; POST with a valid Origin passes CSRF, then routing 405s.
    r = c.post("/api/me", headers={"origin": "http://testserver"})
    assert r.status_code == 405
    assert r.json()["error"]["code"] == "method_not_allowed"
    assert "GET" in r.headers.get("allow", "")


def test_api_body_too_large_is_envelope(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "16")
    c = test_client_factory(_app(monkeypatch, pg_url))
    r = c.post(
        "/api/me/default-profile",
        headers={"origin": "http://testserver", "content-type": "application/json"},
        content=b"x" * 64,
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"


def test_api_validation_error_is_envelope(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    app = _app(monkeypatch, pg_url)
    c, _uid, _pid = _login(app, pg_session_factory, test_client_factory)
    r = c.post(
        "/api/me/default-profile",
        headers={"origin": "http://testserver"},
        json={"profile_id": "abc"},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "validation_error"


def test_api_inactive_account_is_envelope(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    app = _app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="pending", email="pending@ki.se")
        s.add(u)
        s.commit()
        uid = u.id
    c = test_client_factory(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    r = c.get("/api/me")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "account_inactive"
