"""Defect 5: deadline_local_date in the API must match the digest's Stockholm date.

The boundary case is a UTC timestamp just after midnight in Stockholm (UTC+2 in summer):
  2026-06-15T23:30:00Z  → in Stockholm (UTC+2) that is 2026-06-16T01:30:00+02:00
  i.e. the local date is 2026-06-16, NOT 2026-06-15.

Both the digest (via web.filters.display_date) and the API field must agree.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import select

from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import Call, Source, User
from grant_lab.main import create_app
from grant_lab.web.filters import deadline_local_date as _shared_helper
from grant_lab.web.filters import display_date

_STOCKHOLM = ZoneInfo("Europe/Stockholm")


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _login(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="u@umu.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client, uid


def _seed_call(pg_session_factory, *, deadline: datetime) -> int:
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id="DLD1",
            url="https://x",
            title="Deadline Test",
            published_at=datetime.now(UTC),
            deadline=deadline,
        )
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


# 2026-06-15T23:30:00Z → Stockholm (UTC+2) = 2026-06-16T01:30:00+02:00 → date 2026-06-16
_BOUNDARY_UTC = datetime(2026, 6, 15, 23, 30, 0, tzinfo=UTC)
_EXPECTED_LOCAL_DATE = "2026-06-16"


def test_shared_helper_matches_display_date():
    """The shared deadline_local_date helper and display_date must agree."""
    assert _shared_helper(_BOUNDARY_UTC) == _EXPECTED_LOCAL_DATE
    assert display_date(_BOUNDARY_UTC) == _EXPECTED_LOCAL_DATE


def test_shared_helper_returns_none_for_none():
    assert _shared_helper(None) is None


def test_calls_list_includes_deadline_local_date(monkeypatch, pg_url, pg_session_factory):
    """GET /api/calls items include deadline_local_date field.

    Uses a future deadline (the boundary-case correctness is proved by
    test_shared_helper_matches_display_date — pure unit test, no HTTP needed).
    The GET /api/calls list only shows open calls by default, so the deadline
    must be in the future to appear without show_closed=true.
    """
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    # Pick a future UTC time that crosses midnight in Stockholm (UTC+2):
    # noon UTC tomorrow = 14:00 Stockholm — the local date is tomorrow.
    future_deadline = datetime.now(UTC) + timedelta(days=30)
    _seed_call(pg_session_factory, deadline=future_deadline)
    expected_local = future_deadline.astimezone(_STOCKHOLM).strftime("%Y-%m-%d")
    with client:
        r = client.get("/api/calls")
    assert r.status_code == 200
    items = r.json()["items"]
    target = next((i for i in items if i["title"] == "Deadline Test"), None)
    assert target is not None, f"call not in feed; items={items}"
    assert target["deadline_local_date"] == expected_local


def test_call_detail_includes_deadline_local_date(monkeypatch, pg_url, pg_session_factory):
    """GET /api/calls/{id} call object includes deadline_local_date."""
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    cid = _seed_call(pg_session_factory, deadline=_BOUNDARY_UTC)
    with client:
        r = client.get(f"/api/calls/{cid}")
    assert r.status_code == 200
    assert r.json()["call"]["deadline_local_date"] == _EXPECTED_LOCAL_DATE


def test_null_deadline_gives_null_local_date(monkeypatch, pg_url, pg_session_factory):
    """A call without a deadline must have deadline_local_date=null."""
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    cid = _seed_call(pg_session_factory, deadline=None)  # type: ignore[arg-type]
    with client:
        r = client.get(f"/api/calls/{cid}")
    assert r.status_code == 200
    assert r.json()["call"]["deadline_local_date"] is None
