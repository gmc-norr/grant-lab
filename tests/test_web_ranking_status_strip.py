from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.db.models import (
    Call,
    CallRanking,
    Profile,
    ProfileKind,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def _setup(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="u@ki.se")
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
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        calls = [
            Call(
                source_id=src.id,
                external_id=f"E{i}",
                url=f"https://x/{i}",
                title=f"C{i}",
                published_at=datetime(2026, 4, i + 1, tzinfo=UTC),
            )
            for i in range(3)
        ]
        s.add_all(calls)
        s.flush()
        ids = (p.id, u.id, [c.id for c in calls])
        s.commit()
    pid, uid, call_ids = ids
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, pid, call_ids


def test_strip_empty_when_nothing_pending_or_failed(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid, _ = _setup(app, pg_session_factory)
    r = c.get(f"/calls/ranking-status?profile={pid}")
    assert r.status_code == 200 and "Ranking" not in r.text and "couldn't be scored" not in r.text


def test_strip_shows_pending_and_unresolved_failed(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid, call_ids = _setup(app, pg_session_factory)
    with pg_session_factory() as s:
        s.add(RankingJob(call_id=call_ids[0], profile_id=pid, status=RankingJobStatus.PENDING))
        s.add(RankingJob(call_id=call_ids[1], profile_id=pid, status=RankingJobStatus.FAILED))
        # A FAILED job that was later scored (has a CallRanking) must NOT count.
        s.add(RankingJob(call_id=call_ids[2], profile_id=pid, status=RankingJobStatus.FAILED))
        s.add(
            CallRanking(
                call_id=call_ids[2],
                profile_id=pid,
                aggregate_score=70,
                rationale="ok",
                model="g",
                prompt_version="v",
            )
        )
        s.commit()
    r = c.get(f"/calls/ranking-status?profile={pid}")
    assert "Ranking 1 call" in r.text
    assert "1 call couldn't be scored" in r.text  # only the unresolved one
    assert "poll=1" in r.text  # self-poll armed


def test_drain_poll_triggers_refresh(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid, _ = _setup(app, pg_session_factory)  # nothing pending
    r = c.get(f"/calls/ranking-status?profile={pid}&poll=1")
    assert r.headers.get("HX-Refresh") == "true"
