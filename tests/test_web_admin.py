from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Source, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _logged_in(app, pg_session_factory, *, is_admin: bool):
    with pg_session_factory() as s:
        u = User(email="root@ki.se" if is_admin else "alice@ki.se", is_admin=is_admin)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_admin_requires_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory, is_admin=False)
    with client:
        r = client.get("/admin")
    assert r.status_code == 403


def test_admin_lists_sources_with_status(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    # Mark one source as recently fetched. UTC 03:15 in April → 05:15 CEST,
    # so the rendered page must show 05:15 (the spec mandates Europe/Stockholm
    # display).
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        src.last_fetched_at = datetime(2026, 4, 16, 3, 15, tzinfo=UTC)
        s.commit()

    client = _logged_in(app, pg_session_factory, is_admin=True)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    assert "EU Funding &amp; Tenders Portal" in r.text
    assert "Vinnova" in r.text
    assert "2026-04-16" in r.text
    # Stockholm conversion: stored 03:15 UTC → displayed 05:15 (CEST in April)
    assert "05:15" in r.text
    # And NOT the raw UTC value:
    assert "03:15 UTC" not in r.text
