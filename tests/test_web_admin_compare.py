from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import Call, CallRanking, Source, User
from call_monitor.main import create_app


def _setup_admin(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", "admin@ki.se")
    app = create_app()
    with TestClient(app):  # trigger lifespan → bootstrap admin
        pass
    return app


def _admin_cookie(app, pg_session_factory):
    with pg_session_factory() as s:
        u = s.execute(select(User).where(User.email == "admin@ki.se")).scalar_one()
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_compare_view_renders_dual_scheme_rows(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="Compare profile")
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="C", url="https://x", title="CompareCall",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(CallRanking(
            call_id=c.id, profile_id=pid, scheme="v1",
            aggregate_score=70, rationale="v1 rationale",
            model="m", prompt_version="v1", ranked_at=datetime.now(UTC),
        ))
        s.add(CallRanking(
            call_id=c.id, profile_id=pid, scheme="v2",
            aggregate_score=85, rationale="Topic 4/5 · Eligibility: pass · Exclusions: pass",
            model="m", prompt_version="v2.0", ranked_at=datetime.now(UTC),
        ))
        s.commit()

    app = _setup_admin(monkeypatch, pg_url)
    client = _admin_cookie(app, pg_session_factory)
    with client:
        r = client.get(f"/admin/ranking-compare?profile={pid}")
    assert r.status_code == 200
    assert "CompareCall" in r.text
    assert "70" in r.text
    assert "85" in r.text
    assert "v1 rationale" in r.text
    assert "Topic 4/5" in r.text
    # Δ column
    assert "15" in r.text  # |85-70|


def test_compare_view_requires_admin(monkeypatch, pg_url, pg_session_factory):
    """Non-admin user gets 403 (or 401)."""
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()
    with pg_session_factory() as s:
        u = User(email="notadmin@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.get("/admin/ranking-compare?profile=1")
    assert r.status_code in (401, 403)


def test_compare_view_missing_profile_param_returns_400(
    monkeypatch, pg_url, pg_session_factory
):
    app = _setup_admin(monkeypatch, pg_url)
    client = _admin_cookie(app, pg_session_factory)
    with client:
        r = client.get("/admin/ranking-compare")
    assert r.status_code == 400
