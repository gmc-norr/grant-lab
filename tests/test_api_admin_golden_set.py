from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.db.models import (
    Call,
    CallRanking,
    GoldenSetTriple,
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


def _admin(app, sf, *, email="root@ki.se"):
    with sf() as s:
        u = User(status="active", email=email, is_admin=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid


def _seed_call_profile(sf, uid):
    with sf() as s:
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


def test_golden_set_requires_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="alice@ki.se", is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    assert c.get("/api/admin/golden-set").status_code == 403


def test_add_then_read_then_delete(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, uid = _admin(app, pg_session_factory)
    cid, pid = _seed_call_profile(pg_session_factory, uid)
    r = c.post(
        "/api/admin/golden-set",
        json={"call_id": cid, "profile_id": pid, "expected_score": 65, "note": "x"},
    )
    assert r.status_code == 201
    tid = r.json()["id"]
    body = c.get("/api/admin/golden-set").json()
    row = next(t for t in body["triples"] if t["id"] == tid)
    assert row["call_title"] == "C1" and row["expected_score"] == 65 and row["note"] == "x"
    assert set(row) == {
        "id",
        "call_id",
        "profile_id",
        "expected_score",
        "note",
        "call_title",
        "profile_name",
    }
    assert c.request("DELETE", f"/api/admin/golden-set/{tid}").status_code == 204
    with pg_session_factory() as s:
        assert s.execute(select(GoldenSetTriple)).first() is None


def test_add_rejects_out_of_range_score(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, uid = _admin(app, pg_session_factory)
    cid, pid = _seed_call_profile(pg_session_factory, uid)
    r = c.post(
        "/api/admin/golden-set", json={"call_id": cid, "profile_id": pid, "expected_score": 150}
    )
    assert r.status_code == 400


def test_add_unknown_fk_is_400_not_500(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    r = c.post(
        "/api/admin/golden-set",
        json={"call_id": 999999, "profile_id": 999999, "expected_score": 50},
    )
    assert r.status_code == 400
    assert "unknown call or profile" in r.json()["error"]["message"]


def test_add_duplicate_pair_is_409(monkeypatch, pg_url, pg_session_factory):
    # A second triple for the same (call, profile) hits uq_golden_set_triples_pair.
    # It must be a 409 with a "duplicate" message — NOT the FK "unknown" 400.
    app = _setup(monkeypatch, pg_url)
    c, uid = _admin(app, pg_session_factory)
    cid, pid = _seed_call_profile(pg_session_factory, uid)
    first = c.post(
        "/api/admin/golden-set", json={"call_id": cid, "profile_id": pid, "expected_score": 65}
    )
    assert first.status_code == 201
    dup = c.post(
        "/api/admin/golden-set", json={"call_id": cid, "profile_id": pid, "expected_score": 70}
    )
    assert dup.status_code == 409
    assert "already exists" in dup.json()["error"]["message"]


def test_delete_absent_is_204_idempotent(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    assert c.request("DELETE", "/api/admin/golden-set/999999").status_code == 204


def test_escalation_disagreement_candidate_appears(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, uid = _admin(app, pg_session_factory)
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
        call = Call(
            source_id=src.id,
            external_id="esc",
            url="https://x/e",
            title="EscCall",
            published_at=datetime.now(UTC),
        )
        s.add(call)
        s.flush()
        s.add(
            CallRanking(
                call_id=call.id,
                profile_id=p.id,
                aggregate_score=20,
                aggregate_reason="eligibility",
                rationale="r",
                model="claude-sonnet-4-6",
                prompt_version="v5.0",
                raw_response={},
                ranked_at=datetime.now(UTC),
                escalated=True,
                escalated_from_score=80,
            )
        )
        s.commit()
    cands = c.get("/api/admin/golden-set").json()["candidates"]
    esc = next(x for x in cands if x["call_title"] == "EscCall")
    assert esc["reason"] == "escalation_disagreement"
