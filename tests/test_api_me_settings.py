from fastapi.testclient import TestClient

from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import Profile, ProfileKind, Subscription, User
from grant_lab.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _login(app, pg_session_factory, *, name=None, is_admin=False):
    with pg_session_factory() as s:
        u = User(status="active", email="u@umu.se", name=name, is_admin=is_admin)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client, uid


def _profile(pg_session_factory, *, owner_id, name, active=True):
    with pg_session_factory() as s:
        p = Profile(
            name=name,
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level="group",
            description="d",
            structured={},
            created_by=owner_id,
            is_active=active,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def test_settings_read_model(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory, name="Pär", is_admin=True)
    pid_a = _profile(pg_session_factory, owner_id=uid, name="Group A")
    pid_b = _profile(pg_session_factory, owner_id=uid, name="Group B")
    _profile(pg_session_factory, owner_id=uid, name="Archived", active=False)
    with pg_session_factory() as s:
        s.add(Subscription(user_id=uid, profile_id=pid_a))
        s.commit()
    with client:
        r = client.get("/api/me/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Pär"
    assert body["email"] == "u@umu.se"
    assert body["is_admin"] is True
    assert body["has_password"] is False  # OAuth/passwordless test user
    # active profiles only, ordered by name, with subscribed flags
    assert [(p["name"], p["subscribed"]) for p in body["profiles"]] == [
        ("Group A", True),
        ("Group B", False),
    ]
    assert all(p["id"] in (pid_a, pid_b) for p in body["profiles"])


def test_settings_requires_auth(monkeypatch, pg_url):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/api/me/settings")
    assert r.status_code == 401


# --- whether the Monday digest will actually be emailed ----------------------
# UX review 2026-10-31: Settings promised "the Monday-morning digest you want
# emailed to you" on an install with no email transport at all, where nothing
# is ever sent. The page now reads this flag before promising.

import pytest  # noqa: E402


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        pytest.param({}, False, id="no-transport"),
        pytest.param({"RESEND_API_KEY": "re_test"}, True, id="resend"),
        pytest.param({"SMTP_HOST": "smtp.umu.se"}, True, id="smtp"),
        pytest.param({"RESEND_API_KEY": "re_test", "DIGEST_DRY_RUN": "true"}, False, id="dry-run"),
        # An explicit transport whose credential is missing builds no client, so
        # the digest job is never registered (scheduler.py): not deliverable.
        pytest.param({"EMAIL_TRANSPORT": "smtp"}, False, id="smtp-without-host"),
    ],
)
def test_settings_says_whether_the_digest_is_really_emailed(
    env, expected, monkeypatch, pg_url, pg_session_factory
):
    for k in ("RESEND_API_KEY", "SMTP_HOST", "EMAIL_TRANSPORT", "DIGEST_DRY_RUN"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # Config refuses the example.com sender once a transport is set.
    monkeypatch.setenv("EMAIL_FROM", "noreply@umu.se")
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    r = client.get("/api/me/settings")
    assert r.status_code == 200
    assert r.json()["digest_email_enabled"] is expected
