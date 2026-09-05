from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Call, CallDecision, Profile, ProfileKind, Source, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _login(app, pg_session_factory):
    """Active user + ONE active profile they own. Every hide/un-hide now needs a
    profile_id, so the fixture that used to be "a user" is "a user and a profile"."""
    with pg_session_factory() as s:
        u = User(status="active", email="u@umu.se")
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
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client, uid, pid


def _seed_call(pg_session_factory):
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(source_id=src.id, external_id="H", url="https://x", title="H",
                 published_at=datetime.now(UTC))
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def test_hide_returns_hidden_true_and_creates_the_decision(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        r = client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
    assert r.status_code == 200
    assert r.json() == {"hidden": True}
    with pg_session_factory() as s:
        row = s.execute(
            select(CallDecision).where(
                CallDecision.user_id == uid, CallDecision.call_id == cid
            )
        ).scalar_one()
    assert row.decided_at is not None
    assert (row.profile_id, row.state) == (pid, "hidden")


def test_hide_is_idempotent(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        r2 = client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
    assert r2.status_code == 200
    with pg_session_factory() as s:
        rows = s.execute(
            select(CallDecision).where(
                CallDecision.user_id == uid, CallDecision.call_id == cid
            )
        ).scalars().all()
    assert len(rows) == 1


def test_hide_404_for_unknown_call(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    with client:
        r = client.post("/api/calls/999999/hide", params={"profile_id": pid})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_hide_requires_auth(monkeypatch, pg_url, pg_session_factory):
    """401 BEFORE 422: auth is checked ahead of the now-required profile_id, so an
    anonymous caller cannot use the parameter validator to probe for call ids."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post(f"/api/calls/{cid}/hide")
    assert r.status_code == 401


# --- Defect 4: un-hide + list hidden ---

def test_unhide_removes_the_decision(monkeypatch, pg_url, pg_session_factory):
    """DELETE /api/calls/{id}/hide removes the hidden decision so the call reappears."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        r = client.delete(f"/api/calls/{cid}/hide", params={"profile_id": pid})
    assert r.status_code == 200
    assert r.json() == {"hidden": False}
    with pg_session_factory() as s:
        row = s.execute(
            select(CallDecision).where(
                CallDecision.user_id == uid, CallDecision.call_id == cid
            )
        ).scalar_one_or_none()
    assert row is None, "the hidden decision row should have been deleted"


def test_unhide_is_idempotent(monkeypatch, pg_url, pg_session_factory):
    """DELETE on a not-hidden call is a 200, not a 404."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    with client:
        r = client.delete(f"/api/calls/{cid}/hide", params={"profile_id": pid})
    assert r.status_code == 200
    assert r.json() == {"hidden": False}


def test_unhide_404_for_unknown_call(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    with client:
        r = client.delete("/api/calls/999999/hide", params={"profile_id": pid})
    assert r.status_code == 404


def test_unhide_requires_auth(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.delete(f"/api/calls/{cid}/hide")
    assert r.status_code == 401


def test_hidden_list_shows_hidden_calls(monkeypatch, pg_url, pg_session_factory):
    """GET /api/calls/hidden returns the calls hidden by the current user."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        r = client.get("/api/calls/hidden", params={"profile_id": pid})
    assert r.status_code == 200
    ids = [item["id"] for item in r.json()["items"]]
    assert cid in ids


def test_hidden_list_excludes_unhidden_calls(monkeypatch, pg_url, pg_session_factory):
    """After un-hide the call no longer appears in GET /api/calls/hidden."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        client.delete(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        r = client.get("/api/calls/hidden", params={"profile_id": pid})
    assert r.status_code == 200
    ids = [item["id"] for item in r.json()["items"]]
    assert cid not in ids


def test_hidden_list_requires_auth(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/api/calls/hidden")
    assert r.status_code == 401
