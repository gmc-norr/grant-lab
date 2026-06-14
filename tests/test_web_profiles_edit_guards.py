from fastapi.testclient import TestClient

from call_manager.db.models import Profile, ProfileBootstrapJob, ProfileKind, User
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


def _owner_profile(app, pg_session_factory, job_status):
    with pg_session_factory() as s:
        u = User(status="active", email="owner@ki.se", is_admin=False)
        s.add(u)
        s.flush()
        p = Profile(
            name="Grp",
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level="group",
            description="",
            structured={"areas": []},
            created_by=u.id,
        )
        s.add(p)
        s.flush()
        s.add(ProfileBootstrapJob(profile_id=p.id, status=job_status))
        s.commit()
        uid, pid = u.id, p.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, pid


def test_edit_blocked_while_bootstrap_active(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid = _owner_profile(app, pg_session_factory, "running")
    r = c.post(
        f"/profiles/{pid}/edit",
        data={"description": "x", "budget_currency": "SEK"},
        follow_redirects=False,
    )
    assert r.status_code == 409


def test_blank_description_save_does_not_backfill(monkeypatch, pg_url, pg_session_factory):
    called = {"n": 0}
    monkeypatch.setattr(
        "call_manager.web.routes.profiles.backfill_profile",
        lambda **k: called.__setitem__("n", called["n"] + 1) or 0,
    )
    app = _app(monkeypatch, pg_url)
    c, pid = _owner_profile(app, pg_session_factory, "done")  # terminal → editable
    r = c.post(
        f"/profiles/{pid}/edit",
        data={"description": "   ", "budget_currency": "SEK"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert called["n"] == 0
