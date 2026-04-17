from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import Call, CallRanking, Source, User
from call_monitor.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _logged_in(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="user@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def _seed(pg_session_factory, profile_id):
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c1 = Call(
            source_id=src.id, external_id="A", url="https://x/a", title="High score",
            published_at=datetime(2026, 4, 1, tzinfo=UTC),
        )
        c2 = Call(
            source_id=src.id, external_id="B", url="https://x/b", title="Low score",
            published_at=datetime(2026, 4, 2, tzinfo=UTC),
        )
        c3 = Call(
            source_id=src.id, external_id="C", url="https://x/c", title="Unranked",
            published_at=datetime(2026, 4, 3, tzinfo=UTC),
        )
        s.add_all([c1, c2, c3])
        s.flush()
        s.add(CallRanking(call_id=c1.id, profile_id=profile_id, score=85,
                          rationale="Great", model="gemma4:e4b-q8", prompt_version="v1"))
        s.add(CallRanking(call_id=c2.id, profile_id=profile_id, score=15,
                          rationale="Poor", model="gemma4:e4b-q8", prompt_version="v1"))
        s.commit()


def test_root_with_profile_orders_by_score_desc(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    profile_id, _ = profile_factory()
    app = _setup(monkeypatch, pg_url)
    _seed(pg_session_factory, profile_id)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get(f"/?profile={profile_id}")
    assert r.status_code == 200
    body = r.text
    # High score first, then low, then unranked (NULL → last)
    assert body.index("High score") < body.index("Low score") < body.index("Unranked")
    # Score column appears for the ranked rows
    assert "85" in body
    assert "15" in body


def test_root_without_profile_still_uses_published_at(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    profile_id, _ = profile_factory()
    app = _setup(monkeypatch, pg_url)
    _seed(pg_session_factory, profile_id)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/")
    assert r.status_code == 200
    body = r.text
    # No ?profile → published-at desc → C, B, A
    assert body.index("Unranked") < body.index("Low score") < body.index("High score")


def test_root_unknown_profile_degrades_gracefully(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Bogus ?profile=<id> shouldn't 500 or hide calls. The route LEFT
    JOINs call_rankings on profile_id=<unknown>, which matches nothing,
    so all calls render with empty score cells and the dropdown falls
    back to the '— No profile —' option (id 99999 isn't in the active
    profiles list). Non-fatal by design — a stale bookmark from before
    a soft-delete shouldn't error."""
    profile_factory()  # create a real one but request a different id
    # Seed one call so the page has content to render
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add(Call(source_id=src.id, external_id="U-1", url="https://x", title="Visible call"))
        s.commit()

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?profile=99999")
    assert r.status_code == 200
    # The score column is present (profile was requested) but its cell is empty
    # for every row because no CallRanking matches profile_id=99999.
    assert "Score" in r.text  # column header
    assert "Visible call" in r.text  # call still renders
    # Dropdown falls back to "— No profile —" because 99999 is not in the list
    assert "— No profile —" in r.text
