from fastapi.testclient import TestClient
from sqlalchemy import select

from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import Profile, ProfileBootstrapJob, Subscription, User
from grant_lab.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _login(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="creator@umu.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client, uid


_BODY = {
    "name": "Digital Pathology Lab", "kind": "organizational_unit", "level": "group",
    "orcids": "", "names_affiliations": "", "pasted_grants": "", "mission_text": "imaging",
    "areas": "oncology", "career_stage": "", "applicant_types": "", "role": "",
    "years_since_phd": "", "org_type": "university_hospital", "capabilities": "biobank",
    "patient_populations": "", "scale": "", "funding_instruments": "", "funder_exclusions": "",
    "country": "SE", "host_institution": "UmU",
}


def test_create_returns_id_and_enqueues_job(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    with client:
        r = client.post("/api/profiles", json=_BODY)
    assert r.status_code == 200
    new_id = r.json()["id"]
    with pg_session_factory() as s:
        assert s.get(Profile, new_id) is not None
        job = s.execute(
            select(ProfileBootstrapJob).where(ProfileBootstrapJob.profile_id == new_id)
        ).scalar_one()
        assert job.status == "queued"
        sub = s.execute(
            select(Subscription).where(
                Subscription.user_id == uid, Subscription.profile_id == new_id
            )
        ).scalar_one_or_none()
        assert sub is not None  # creator auto-subscribed
        assert s.get(User, uid).default_profile_id == new_id  # default set


def test_create_duplicate_name_409(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r1 = client.post("/api/profiles", json=_BODY)
        assert r1.status_code == 200
        r2 = client.post("/api/profiles", json={**_BODY})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "conflict"


def test_create_dup_name_checked_before_member_bounds(monkeypatch, pg_url, pg_session_factory):
    # Combined failure: a duplicate active name AND >50 members. Parity requires
    # the dup-name 409 to fire BEFORE the member-bounds 400 (profiles.py:131-137).
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    too_many = "\n".join(f"0000-0001-0000-{i:04d}" for i in range(60))
    with client:
        assert client.post("/api/profiles", json=_BODY).status_code == 200
        r = client.post("/api/profiles", json={**_BODY, "orcids": too_many})
    assert r.status_code == 409  # NOT 400 — dup checked before member bounds


def test_create_name_too_long_400(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r = client.post("/api/profiles", json={**_BODY, "name": "x" * 201})
    assert r.status_code == 400


def test_create_grants_too_large_413(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r = client.post("/api/profiles", json={**_BODY, "pasted_grants": "y" * 100_001})
    assert r.status_code == 413


def test_create_requires_auth(monkeypatch, pg_url):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post("/api/profiles", json=_BODY)
    assert r.status_code == 401
