from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import (
    Call,
    CallRanking,
    CallRankingCriterion,
    Profile,
    ProfileKind,
    Source,
    User,
)
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _login(app, pg_session_factory, email="u@ki.se"):
    with pg_session_factory() as s:
        u = User(status="active", email=email)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client, uid


def _seed_call(pg_session_factory, *, title="CD"):
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id=title,
            url="https://x",
            title=title,
            summary="A summary.",
            published_at=datetime.now(UTC),
            keywords=["genomics", "oncology"],
        )
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _seed_profile(pg_session_factory, *, owner_id, active=True, name="P"):
    with pg_session_factory() as s:
        p = Profile(
            name=name,
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level="group",
            description="d",
            structured={},
            created_by=owner_id,
            is_active=active,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _seed_ranking(pg_session_factory, *, cid, pid, score=72, sim=0.42):
    with pg_session_factory() as s:
        s.add(
            CallRanking(
                call_id=cid,
                profile_id=pid,
                aggregate_score=score,
                model="claude-sonnet-4-6",
                prompt_version="v1",
                embed_similarity=sim,
                rationale="Strong fit.",
            )
        )
        s.add(
            CallRankingCriterion(
                call_id=cid, profile_id=pid, criterion="embedding", rating="pass"
            )
        )
        s.commit()


def test_detail_no_profile_returns_call_without_ranking(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r = client.get(f"/api/calls/{cid}")
    assert r.status_code == 200
    body = r.json()
    assert body["call"]["id"] == cid
    assert body["call"]["keywords"] == ["genomics", "oncology"]
    assert body["call"]["summary"] == "A summary."
    assert body["ranking"] is None
    assert body["criteria"] == []
    assert body["best_profile_id"] is None


def test_detail_active_profile_returns_ranking_and_why(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    pid = _seed_profile(pg_session_factory, owner_id=uid)
    _seed_ranking(pg_session_factory, cid=cid, pid=pid)
    with client:
        r = client.get(f"/api/calls/{cid}?profile={pid}")
    assert r.status_code == 200
    body = r.json()
    assert body["ranking"]["aggregate_score"] == 72
    assert body["ranking"]["model_label"] == "Sonnet 4.6"
    emb = next(c for c in body["criteria"] if c["criterion"] == "embedding")
    assert emb["detail"] == "Cosine similarity 0.420 (pass threshold 0.30)."
    assert emb["is_reason_code"] is True
    assert body["best_profile_id"] == pid


def test_detail_active_not_owned_profile_still_shows_ranking(monkeypatch, pg_url, pg_session_factory):
    # P1b parity: ranking display keys off is_active ALONE, not ownership.
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    other_uid = _login(app, pg_session_factory, email="other@ki.se")[1]
    pid = _seed_profile(pg_session_factory, owner_id=other_uid, name="Other")
    _seed_ranking(pg_session_factory, cid=cid, pid=pid)
    assert other_uid != uid
    with client:
        r = client.get(f"/api/calls/{cid}?profile={pid}")
    assert r.status_code == 200
    assert r.json()["ranking"]["aggregate_score"] == 72
    # ...but best_profile_id (ownership-gated) stays None for the logged-in user.
    assert r.json()["best_profile_id"] is None


def test_detail_inactive_profile_no_ranking(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    pid = _seed_profile(pg_session_factory, owner_id=uid, active=False)
    _seed_ranking(pg_session_factory, cid=cid, pid=pid)
    with client:
        r = client.get(f"/api/calls/{cid}?profile={pid}")
    assert r.status_code == 200
    assert r.json()["ranking"] is None


def test_detail_empty_profile_is_none(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r = client.get(f"/api/calls/{cid}?profile=")
    assert r.status_code == 200
    assert r.json()["ranking"] is None


def test_detail_bad_profile_400(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r = client.get(f"/api/calls/{cid}?profile=abc")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_detail_missing_call_404(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r = client.get("/api/calls/999999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_detail_requires_auth(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get(f"/api/calls/{cid}")
    assert r.status_code == 401
