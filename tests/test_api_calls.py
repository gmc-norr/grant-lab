from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from call_manager.db.models import Call, CallRanking, Profile, ProfileKind, Source, User
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


@pytest.fixture
def authed_client(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
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
    with TestClient(app) as client:
        client.cookies.set("session", app.state.session_signer.make(user_id=uid))
        yield client, uid, pid


def test_api_calls_is_read_only(authed_client, pg_session_factory):
    c, uid, pid = authed_client
    r = c.get(f"/api/calls?profile={pid}")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and "summary" in body and body["ranking"] == {"pending": 0, "failed": 0}
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id is None


def test_api_calls_returns_seeded_call_shape(authed_client, pg_session_factory):
    c, uid, pid = authed_client
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        src = Source(name="Seeded Source", slug="seeded-source-api-test")
        s.add(src)
        s.flush()
        call = Call(
            source_id=src.id,
            external_id="ext-1",
            url="https://example.test/call/1",
            title="Open Grant Call",
            published_at=now,
            deadline=now + timedelta(days=10),
        )
        s.add(call)
        s.flush()
        s.add(
            CallRanking(
                call_id=call.id,
                profile_id=pid,
                aggregate_score=82,
                model="test-model",
                prompt_version="v1",
            )
        )
        s.commit()
        src_slug, src_name = src.slug, src.name

    r = c.get(f"/api/calls?profile={pid}")
    assert r.status_code == 200
    body = r.json()

    assert len(body["items"]) == 1
    item = body["items"][0]
    # The match card (spec §5) reads its whole body off this row, so the key set
    # is pinned by EQUALITY: a field silently dropped from CallOut is a blank
    # card, and a field silently added is payload nobody asked for on every row.
    assert set(item) == {
        "id",
        "title",
        "deadline",
        "deadline_local_date",
        "published_at",
        "source_name",
        "source_slug",
        "aggregate_score",
        "programme",
        "budget_min",
        "budget_max",
        "currency",
        "url",
        "summary",
        "keywords",
        "criteria",
        "applicant_note_count",
        "escalated",
    }
    assert item["source_slug"] == src_slug
    assert item["source_name"] == src_name
    assert item["aggregate_score"] == 82
    assert item["url"] == "https://example.test/call/1"
    assert item["keywords"] == []
    # This call is ranked but has no criteria and no applicant notes -- the
    # shape a Gemma-only ranking produces.
    assert item["criteria"] == []
    assert item["applicant_note_count"] == 0
    assert item["escalated"] is False

    # Typed containers now expose their fields (would catch a key rename).
    assert set(body["pagination"]) == {"page", "limit", "has_prev", "has_next", "total"}
    assert set(body["effective"]) == {"source", "sort", "profile_id", "show_closed"}


def test_post_default_profile_persists(authed_client, pg_session_factory):
    c, uid, pid = authed_client
    r = c.post(
        "/api/me/default-profile",
        headers={"origin": "http://testserver"},
        json={"profile_id": pid},
    )
    assert r.status_code == 200
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id == pid
    c.post(
        "/api/me/default-profile",
        headers={"origin": "http://testserver"},
        json={"profile_id": None},
    )
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id is None


def test_set_default_profile_invalid_404(authed_client, pg_session_factory):
    c, uid, pid = authed_client
    # Establish a valid stored default first.
    r = c.post(
        "/api/me/default-profile",
        headers={"origin": "http://testserver"},
        json={"profile_id": pid},
    )
    assert r.status_code == 200
    # An unknown profile id 404s and must NOT clobber the stored default.
    r = c.post(
        "/api/me/default-profile",
        headers={"origin": "http://testserver"},
        json={"profile_id": 999999},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id == pid  # unchanged
