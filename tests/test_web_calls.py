from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import Call, Source, User
from call_monitor.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _logged_in(app, pg_session_factory, email="alice@ki.se"):
    with pg_session_factory() as s:
        u = User(email=email)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def _seed_calls(pg_session_factory):
    with pg_session_factory() as s:
        eu = s.execute(select(Source).where(Source.slug == "eu_portal")).scalar_one()
        vn = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add_all([
            Call(source_id=eu.id, external_id="E-1", url="u1", title="EU one",
                 published_at=datetime(2026, 4, 1, tzinfo=UTC)),
            Call(source_id=vn.id, external_id="V-1", url="u2", title="Vin one",
                 published_at=datetime(2026, 4, 5, tzinfo=UTC)),
            Call(source_id=eu.id, external_id="E-2", url="u3", title="EU two",
                 published_at=datetime(2026, 3, 1, tzinfo=UTC)),
        ])
        s.commit()


def test_root_requires_auth(monkeypatch, pg_url):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 401


def test_root_lists_calls_newest_first(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/")
    assert r.status_code == 200
    body = r.text
    # Newest published_at first
    assert body.index("Vin one") < body.index("EU one") < body.index("EU two")


def test_root_filter_by_source(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?source=vinnova")
    assert r.status_code == 200
    assert "Vin one" in r.text
    assert "EU one" not in r.text


def test_call_detail_shows_metadata(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with pg_session_factory() as s:
        cid = s.execute(select(Call.id).where(Call.external_id == "V-1")).scalar_one()
    with client:
        r = client.get(f"/calls/{cid}")
    assert r.status_code == 200
    assert "Vin one" in r.text


def test_call_detail_404_for_unknown_id(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/calls/99999")
    assert r.status_code == 404
