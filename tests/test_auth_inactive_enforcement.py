from fastapi.testclient import TestClient

from call_manager.auth.dependencies import AccountInactive, get_current_user
from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import User
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _user(sf, status):
    with sf() as s:
        u = User(email=f"{status}@umu.se", status=status)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def test_inactive_api_caller_gets_403(monkeypatch, pg_url, pg_session_factory):
    """A disabled user hitting an /api route gets the 403 account_inactive envelope
    carrying the /auth/no-access redirect target + a cleared (now-inert) cookie.
    The SPA's unwrap routes off this 403 (no server-side browser redirect)."""
    app = _app(monkeypatch, pg_url)
    uid = _user(app.state.session_factory, "disabled")
    with TestClient(app) as c:
        c.cookies.set("session", app.state.session_signer.make(user_id=uid))
        r = c.get("/api/me")
    assert r.status_code == 403
    body = r.json()
    assert body["error"]["code"] == "account_inactive"
    assert body["error"]["detail"]["redirect"] == "/auth/no-access"
    set_cookie = " ".join(r.headers.get_list("set-cookie"))
    assert "session=" in set_cookie and (
        "Max-Age=0" in set_cookie or "expires=" in set_cookie.lower()
    )


def test_pending_api_caller_gets_pending_redirect(monkeypatch, pg_url, pg_session_factory):
    """Parity with the old pending-vs-no-access split: a pending user's 403 carries
    the /auth/pending target, every other non-active status the /auth/no-access
    catch-all."""
    app = _app(monkeypatch, pg_url)
    uid = _user(app.state.session_factory, "pending")
    with TestClient(app) as c:
        c.cookies.set("session", app.state.session_signer.make(user_id=uid))
        r = c.get("/api/me")
    assert r.status_code == 403
    assert r.json()["error"]["detail"]["redirect"] == "/auth/pending"


def test_get_current_user_raises_account_inactive(pg_url, pg_session_factory):
    # Direct unit check of the dependency raising the typed exception.
    import pytest
    from fastapi import FastAPI, Request

    from call_manager.db.session import make_engine, make_session_factory

    signer = SessionSigner(secret="x" * 32, max_age_seconds=3600)
    engine = make_engine(pg_url)
    sf = make_session_factory(engine)
    try:
        with sf() as s:
            u = User(email="p@umu.se", status="pending")
            s.add(u)
            s.commit()
            s.refresh(u)
            uid = u.id
        app = FastAPI()
        app.state.session_signer = signer
        app.state.session_factory = sf
        scope = {"type": "http", "headers": [], "app": app}
        req = Request(scope)
        with pytest.raises(AccountInactive):
            get_current_user(req, session=signer.make(user_id=uid))
    finally:
        engine.dispose()
