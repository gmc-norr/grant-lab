from fastapi.testclient import TestClient

import call_manager.profiles.post_edit as post_edit
from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Profile, ProfileBootstrapJob, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    # Keep the re-embed/backfill driver offline (it would hit Ollama).
    monkeypatch.setattr(post_edit, "refresh_and_backfill_in_background", lambda **kw: None)
    return create_app()


def _client_for(app, uid):
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def _other_user(pg_session_factory, *, is_admin=False, email="o@ki.se"):
    with pg_session_factory() as s:
        u = User(status="active", email=email, is_admin=is_admin)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


_EDIT = {
    "description": "Updated description.", "areas": "oncology\nimaging",
    "budget_min": "100", "budget_max": "200", "budget_currency": "SEK",
    "trl_min": "3", "trl_max": "7", "must_include": "pediatric", "hard_exclusions": "",
    "level": "group", "career_stage": "", "applicant_types": "", "role": "",
    "years_since_phd": "", "org_type": "university_hospital", "capabilities": "biobank",
    "patient_populations": "", "scale": "", "funding_instruments": "project_grant",
    "funder_exclusions": "", "country": "SE", "host_institution": "KI",
}


def test_update_areas_only_change_triggers_reembed(  # noqa: E501
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    # Editing ONLY areas (description resubmitted UNCHANGED) must still re-embed,
    # because areas feed build_profile_text — not just `description`. Parity with the
    # retired Jinja test_editing_only_areas_triggers_reembed (Codex-H1). Here we SPY
    # on the driver (instead of no-op'ing it) to assert refresh_embedding=True.
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    captured: dict = {}

    def _spy(**kw):
        captured.update(kw)

    monkeypatch.setattr(post_edit, "refresh_and_backfill_in_background", _spy)
    app = create_app()
    pid, owner_id = profile_factory(
        name="Lab", description="Imaging research lab.", structured={"areas": ["old area"]}
    )
    client = _client_for(app, owner_id)
    # Same non-empty description (so the bg task is scheduled), only areas differ.
    body = {**_EDIT, "description": "Imaging research lab.", "areas": "new area"}
    with client:
        r = client.put(f"/api/profiles/{pid}", json=body)
    assert r.status_code == 200
    assert captured.get("refresh_embedding") is True  # areas changed -> re-embed


def test_update_owner_renamespaces(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Lab", structured={})
    client = _client_for(app, owner_id)
    with client:
        r = client.put(f"/api/profiles/{pid}", json=_EDIT)
    assert r.status_code == 200
    assert r.json() == {"id": pid}
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.structured["areas"] == ["oncology", "imaging"]
        assert p.structured["org"]["capabilities"] == ["biobank"]
        # form funding_instruments -> structured["funding"]["instruments"]
        assert p.structured["funding"]["instruments"] == ["project_grant"]
        assert p.budget_currency == "SEK"


def test_update_403_non_owner(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Lab")
    other = _other_user(pg_session_factory)
    client = _client_for(app, other)
    with client:
        r = client.put(f"/api/profiles/{pid}", json=_EDIT)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_update_409_while_drafting(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Lab")
    with pg_session_factory() as s:
        s.add(ProfileBootstrapJob(profile_id=pid, status="running", seed={"x": 1}))
        s.commit()
    client = _client_for(app, owner_id)
    with client:
        r = client.put(f"/api/profiles/{pid}", json=_EDIT)
    assert r.status_code == 409


def test_update_403_before_409_when_drafting(monkeypatch, pg_url, pg_session_factory, profile_factory):  # noqa: E501
    # Combined failure: a non-owner AND an active bootstrap job. The in-session
    # order checks 403 (ownership) BEFORE the 409 (drafting), so a non-owner sees
    # 403 even while the profile is drafting (profiles.py 297-309).
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Lab")
    with pg_session_factory() as s:
        s.add(ProfileBootstrapJob(profile_id=pid, status="running", seed={"x": 1}))
        s.commit()
    other = _other_user(pg_session_factory, email="other2@ki.se")
    client = _client_for(app, other)
    with client:
        r = client.put(f"/api/profiles/{pid}", json=_EDIT)
    assert r.status_code == 403  # NOT 409 — 403 checked before the drafting 409


def test_update_bad_currency_400_before_404(monkeypatch, pg_url, pg_session_factory):
    # input-shape validation (currency 400) fires BEFORE the in-session 404 — parity.
    app = _setup(monkeypatch, pg_url)
    owner = _other_user(pg_session_factory, email="owner@ki.se")
    client = _client_for(app, owner)
    with client:
        r = client.put("/api/profiles/999999", json={**_EDIT, "budget_currency": "ZZZ"})
    assert r.status_code == 400


def test_update_404_missing(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    owner = _other_user(pg_session_factory, email="owner@ki.se")
    client = _client_for(app, owner)
    with client:
        r = client.put("/api/profiles/999999", json=_EDIT)
    assert r.status_code == 404


def test_update_budget_out_of_range_400(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Lab")
    client = _client_for(app, owner_id)
    with client:
        r = client.put(f"/api/profiles/{pid}", json={**_EDIT, "budget_min": "999999999999999"})
    assert r.status_code == 400


def test_update_trl_out_of_range_400(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Lab")
    client = _client_for(app, owner_id)
    with client:
        r = client.put(f"/api/profiles/{pid}", json={**_EDIT, "trl_max": "99"})
    assert r.status_code == 400


def test_update_unknown_level_400(monkeypatch, pg_url, pg_session_factory, profile_factory):
    # level is validated IN-session (org units only) — profile_factory makes an org unit.
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Lab")
    client = _client_for(app, owner_id)
    with client:
        r = client.put(f"/api/profiles/{pid}", json={**_EDIT, "level": "galaxy"})
    assert r.status_code == 400


def test_update_description_too_large_413(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Lab")
    client = _client_for(app, owner_id)
    with client:
        r = client.put(f"/api/profiles/{pid}", json={**_EDIT, "description": "x" * 50_001})
    assert r.status_code == 413
