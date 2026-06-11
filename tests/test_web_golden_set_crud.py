from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import (
    Call,
    GoldenSetTriple,
    Profile,
    ProfileKind,
    Source,
    User,
)
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _admin(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="root@ki.se", is_admin=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client, uid


def _seed_call_profile(pg_session_factory, uid):
    with pg_session_factory() as s:
        p = Profile(
            name="P",
            kind=ProfileKind.ORGANIZATIONAL_UNIT,
            description="d",
            structured={},
            created_by=uid,
        )
        s.add(p)
        s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id="c1",
            url="https://x/1",
            title="C1",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.commit()
        return c.id, p.id


def test_add_then_delete_triple_via_routes(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    client, uid = _admin(app, pg_session_factory)
    cid, pid = _seed_call_profile(pg_session_factory, uid)
    with client:
        r = client.post(
            "/admin/golden-set/add",
            data={"call_id": cid, "profile_id": pid, "expected_score": 65, "note": "x"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        page = client.get("/admin/golden-set")
        assert "C1" in page.text and "65" in page.text
    with pg_session_factory() as s:
        row = s.execute(select(GoldenSetTriple)).scalar_one()
        tid = row.id
    with client:
        r = client.post("/admin/golden-set/delete", data={"triple_id": tid}, follow_redirects=False)
        assert r.status_code == 303
    with pg_session_factory() as s:
        assert s.execute(select(GoldenSetTriple)).first() is None


def test_add_rejects_out_of_range_score(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    client, uid = _admin(app, pg_session_factory)
    cid, pid = _seed_call_profile(pg_session_factory, uid)
    with client:
        r = client.post(
            "/admin/golden-set/add",
            data={"call_id": cid, "profile_id": pid, "expected_score": 150},
            follow_redirects=False,
        )
    assert r.status_code == 400


def test_golden_set_page_requires_admin(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="alice@ki.se", is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.get("/admin/golden-set")
    assert r.status_code in (403, 401)
