from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Call, Source, User
from call_manager.main import create_app

# NOTE: the dashboard `GET /` tests that used to live here were removed in Plan 3
# Task 9 when the Jinja calls_index handler was retired (the React SPA now owns
# `/`). Their read-model feed behaviors (ordering, filtering, show_closed,
# pagination, summary badges, profile resolution) were ported to
# tests/test_api_calls_feed_behavior.py against the JSON `GET /api/calls`. What
# remains here exercises the still-server-rendered call-detail page.


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _logged_in(app, pg_session_factory, email="alice@ki.se"):
    with pg_session_factory() as s:
        u = User(email=email, status="active")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def _seed_calls(pg_session_factory):
    # One Vinnova call (V-1) the call-detail tests fetch by id; all OPEN so they
    # are not filtered by any closed-deadline logic.
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        vn = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add(
            Call(
                source_id=vn.id,
                external_id="V-1",
                url="u2",
                title="Vin one",
                published_at=datetime(2026, 4, 5, tzinfo=UTC),
                deadline=now + timedelta(days=45),
            )
        )
        s.commit()


def test_call_detail_treats_empty_profile_as_unfiltered(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with pg_session_factory() as s:
        cid = s.execute(select(Call.id).where(Call.external_id == "V-1")).scalar_one()
    with client:
        r = client.get(f"/calls/{cid}?profile=")
    assert r.status_code == 200


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


# NOTE: test_app_js_autosubmit_selector_is_generalized was removed in Plan 3
# Task 9. The [data-autosubmit] generalization it guarded existed for the Jinja
# dashboard's filter form + show-closed checkbox; that dashboard (calls_index.html)
# was retired, leaving NO template consumer of data-autosubmit, so the guard no
# longer protects any live behavior (the SPA drives filters in React).
