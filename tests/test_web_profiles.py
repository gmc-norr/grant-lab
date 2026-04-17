from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import User
from call_monitor.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _logged_in(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="u@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_profiles_list_requires_auth(monkeypatch, pg_url):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/profiles")
    assert r.status_code == 401


def test_profiles_list_shows_active_only(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    p_active, _ = profile_factory(name="Active group")
    p_inactive, _ = profile_factory(name="Inactive group")
    from call_monitor.db.models import Profile
    with pg_session_factory() as s:
        s.get(Profile, p_inactive).is_active = False
        s.commit()

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/profiles")
    assert r.status_code == 200
    assert "Active group" in r.text
    assert "Inactive group" not in r.text


def test_profile_new_form_renders(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/profiles/new")
    assert r.status_code == 200
    assert "name" in r.text.lower()
    assert "orcid" in r.text.lower()


def test_profile_new_post_creates_profile_and_redirects(
    monkeypatch, pg_url, pg_session_factory
):
    """POST creates a Profile synchronously (bootstrap is mocked via
    app.state override) and redirects to /profiles/<id>/edit."""
    from unittest.mock import patch

    from call_monitor.db.models import Profile
    from call_monitor.profiles.seed import DraftProfile

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)

    fake_draft = DraftProfile(
        description="fake draft description",
        structured={
            "areas": ["digital pathology"],
            "budget_min": None,
            "budget_max": None,
            "trl_min": None,
            "trl_max": None,
            "must_include": [],
            "hard_exclusions": [],
        },
        publications_fetched=0,
        publications_skipped=0,
    )
    with (
        patch("call_monitor.web.routes.profiles.bootstrap_profile", return_value=fake_draft),
        client,
    ):
        r = client.post(
            "/profiles/new",
            data={
                "name": "Pathology AI",
                "kind": "group",
                "orcids": "",
                "names_affiliations": "",
                "pasted_grants": "dummy",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/profiles/")
    assert r.headers["location"].endswith("/edit")
    with pg_session_factory() as s:
        p = s.execute(select(Profile).where(Profile.name == "Pathology AI")).scalar_one()
    assert p.description == "fake draft description"
    assert p.structured["areas"] == ["digital pathology"]
    assert p.is_active is True
