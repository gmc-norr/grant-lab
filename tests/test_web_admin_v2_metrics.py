from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import (
    Call,
    CallRanking,
    Profile,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from call_monitor.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", "admin@ki.se")
    return create_app()


def _admin_client(app, pg_session_factory):
    with TestClient(app):  # trigger lifespan to bootstrap admin
        pass
    with pg_session_factory() as s:
        u = s.execute(select(User).where(User.email == "admin@ki.se")).scalar_one()
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_admin_shows_per_scheme_queue_depth(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="P")
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="X", url="https://x", title="X",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(RankingJob(call_id=c.id, profile_id=pid, scheme="v1",
                         status=RankingJobStatus.PENDING))
        s.add(RankingJob(call_id=c.id, profile_id=pid, scheme="v2",
                         status=RankingJobStatus.PENDING))
        s.commit()

    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    # Per-scheme queue depth section
    import re
    m = re.search(r"v1[^<]*</dt>\s*<dd>\s*(\d+)\s*</dd>", r.text)
    assert m and int(m.group(1)) >= 1
    m = re.search(r"v2[^<]*</dt>\s*<dd>\s*(\d+)\s*</dd>", r.text)
    assert m and int(m.group(1)) >= 1


def test_admin_shows_per_profile_scheme_and_last_rank_time(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="HasRanking")
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        p.ranking_scheme = "v2"
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="LR", url="https://x", title="LR",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(CallRanking(
            call_id=c.id, profile_id=pid, scheme="v2",
            aggregate_score=80, rationale="r", model="m",
            prompt_version="v2.0", ranked_at=datetime.now(UTC),
        ))
        s.commit()

    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    assert "HasRanking" in r.text
    assert "v2" in r.text  # active scheme rendered next to profile name
