from fastapi.testclient import TestClient

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


def test_api_unknown_path_returns_envelope_404(monkeypatch, pg_url, pg_session_factory):
    c = TestClient(_app(monkeypatch, pg_url))
    r = c.get("/api/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert set(body["error"]) >= {"code", "message"}
    assert body["error"]["code"] == "not_found"


def test_non_api_404_is_unchanged(monkeypatch, pg_url, pg_session_factory):
    c = TestClient(_app(monkeypatch, pg_url))
    r = c.get("/totally-unknown-html-path", headers={"accept": "application/json"})
    assert r.status_code == 404
    assert "error" not in r.json()


def test_api_500_is_sanitized_envelope(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)

    @app.get("/api/_boom")
    def _boom():
        raise RuntimeError("secret internals")

    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/api/_boom")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal_error"
    assert "secret internals" not in r.text


def test_api_5xx_httpexception_detail_is_not_leaked(monkeypatch, pg_url, pg_session_factory):
    from fastapi import HTTPException

    app = _app(monkeypatch, pg_url)

    @app.get("/api/_unavailable")
    def _unavailable():
        raise HTTPException(status_code=503, detail="db unavailable: secret path /var/db")

    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/api/_unavailable")
    assert r.status_code == 503
    body = r.json()
    assert body["error"]["code"] == "internal_error"
    assert "secret path" not in r.text and "db unavailable" not in r.text


def test_api_4xx_httpexception_detail_is_echoed(monkeypatch, pg_url, pg_session_factory):
    from fastapi import HTTPException

    app = _app(monkeypatch, pg_url)

    @app.get("/api/_teapot")
    def _teapot():
        raise HTTPException(status_code=418, detail="i am a teapot")

    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/api/_teapot")
    assert r.status_code == 418
    body = r.json()
    assert body["error"]["code"] == "error"
    assert body["error"]["message"] == "i am a teapot"


def test_non_api_500_is_plain_text(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)

    @app.get("/_boom")
    def _boom():
        raise RuntimeError("secret internals")

    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/_boom")
    assert r.status_code == 500
    assert r.text == "Internal Server Error"
    assert "error" not in r.headers.get("content-type", "")
