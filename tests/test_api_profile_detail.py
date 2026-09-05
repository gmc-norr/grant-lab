from fastapi.testclient import TestClient

from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import Profile, ProfileBootstrapJob, User
from grant_lab.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _client_for(app, pg_session_factory, uid):
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def _new_user(pg_session_factory, *, is_admin=False, email="u@umu.se"):
    with pg_session_factory() as s:
        u = User(status="active", email=email, is_admin=is_admin)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def test_detail_flattens_namespaced_structured(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    structured = {
        "areas": ["oncology"],
        "budget_min": 100, "budget_max": 200,
        "trl_min": 3, "trl_max": 7,
        "must_include": ["pediatric"], "hard_exclusions": ["defence"],
        "org": {"org_type": "university_hospital", "capabilities": ["biobank"],
                "patient_populations": ["children"], "scale": "~400 researchers"},
        "funding": {"instruments": ["project_grant"], "funder_exclusions": ["x"]},
        "geo": {"country": "SE", "host_institution": "UmU"},
        "eligibility": {"applicant_types": ["solo_pi"], "role": "PI", "years_since_phd": 5},
    }
    pid, owner_id = profile_factory(name="Lab", structured=structured)
    client = _client_for(app, pg_session_factory, owner_id)
    with client:
        r = client.get(f"/api/profiles/{pid}")
    assert r.status_code == 200
    b = r.json()
    assert b["areas"] == ["oncology"]
    assert b["org_type"] == "university_hospital"
    assert b["capabilities"] == ["biobank"]
    # The one asymmetry: form field funding_instruments <- structured["funding"]["instruments"].
    assert b["funding_instruments"] == ["project_grant"]
    assert b["country"] == "SE"
    assert b["can_edit"] is True


def test_detail_can_edit_false_nulls_bootstrap(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Lab")
    with pg_session_factory() as s:
        s.add(ProfileBootstrapJob(profile_id=pid, status="running", seed={"x": 1}))
        s.commit()
    other = _new_user(pg_session_factory, email="other@umu.se")
    client = _client_for(app, pg_session_factory, other)
    with client:
        r = client.get(f"/api/profiles/{pid}")
    assert r.status_code == 200
    b = r.json()
    assert b["can_edit"] is False
    assert b["bootstrap_status"] is None  # nulled for non-owner


def test_detail_owner_sees_bootstrap_status(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Lab")
    with pg_session_factory() as s:
        s.add(ProfileBootstrapJob(profile_id=pid, status="running", seed={"x": 1}))
        s.commit()
    client = _client_for(app, pg_session_factory, owner_id)
    with client:
        r = client.get(f"/api/profiles/{pid}")
    assert r.json()["bootstrap_status"] == "running"


def test_detail_admin_can_edit(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Lab")
    admin = _new_user(pg_session_factory, is_admin=True, email="admin@umu.se")
    client = _client_for(app, pg_session_factory, admin)
    with client:
        r = client.get(f"/api/profiles/{pid}")
    assert r.json()["can_edit"] is True


def test_detail_404_inactive(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Lab")
    with pg_session_factory() as s:
        s.execute(Profile.__table__.update().where(Profile.id == pid).values(is_active=False))
        s.commit()
    client = _client_for(app, pg_session_factory, owner_id)
    with client:
        r = client.get(f"/api/profiles/{pid}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
