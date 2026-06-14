from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Call, RankingJob, Source, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _admin_client(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="root@ki.se", is_admin=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_admin_shows_queue_counts(monkeypatch, pg_url, pg_session_factory, profile_factory):
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(source_id=src.id, external_id="X", url="https://x", title="T")
        s.add(c)
        s.flush()
        from call_manager.db.models import RankingJobStatus

        s.add(RankingJob(call_id=c.id, profile_id=profile_id, status=RankingJobStatus.PENDING))
        s.commit()

    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    assert "Ranking queue" in r.text
    # All four status labels render
    for label in ("Pending", "Running", "Failed", "Done"):
        assert label in r.text
    # The seeded job is in PENDING; the rendered table cell shows ">1<"
    # (i.e. count of 1 surrounded by td tags)
    assert ">1<" in r.text


def test_admin_rerank_enqueues_jobs_for_recent_calls(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    from datetime import UTC, datetime, timedelta

    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add(
            Call(
                source_id=src.id,
                external_id="R",
                url="https://x/r",
                title="T",
                published_at=datetime.now(UTC) - timedelta(days=10),
            )
        )
        s.commit()

    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.post("/admin/rerank", follow_redirects=False)
    assert r.status_code == 303  # redirect back to /admin
    with pg_session_factory() as s:
        jobs = s.execute(select(RankingJob)).scalars().all()
    assert len(jobs) == 1


def test_admin_rerank_requires_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    # Non-admin user
    with pg_session_factory() as s:
        u = User(status="active", email="nonadmin@ki.se", is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.post("/admin/rerank")
    assert r.status_code == 403
