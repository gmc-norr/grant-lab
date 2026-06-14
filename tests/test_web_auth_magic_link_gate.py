# tests/test_web_auth_magic_link_gate.py
from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.email import InMemoryEmailSender
from call_manager.db.models import MagicLinkToken, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    app.state.email_sender = InMemoryEmailSender()
    return app


def test_unknown_email_creates_no_user_no_token(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        assert c.post("/login", data={"email": "ghost@ki.se"}).status_code == 200
    with pg_session_factory() as s:
        found = s.execute(select(User).where(User.email == "ghost@ki.se")).scalar_one_or_none()
        assert found is None
        assert s.execute(select(MagicLinkToken)).first() is None


def test_disabled_after_issue_cannot_consume(monkeypatch, pg_url, pg_session_factory):
    from call_manager.auth.tokens import issue_token
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="real@ki.se", status="active")
        s.add(u)
        s.flush()
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()
        uid = u.id
    with pg_session_factory() as s:
        s.get(User, uid).status = "disabled"
        s.commit()
    with TestClient(app) as c:
        r = c.post("/login/verify", data={"token": raw},
                   headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/auth/no-access"
        assert "session=" not in "".join(h for h in r.headers.get_list("set-cookie"))
