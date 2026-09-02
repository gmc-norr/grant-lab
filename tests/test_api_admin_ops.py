from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from call_manager.db.models import Call, RankingJob, Source, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _admin(app, sf, test_client_factory, *, email="root@ki.se"):
    with sf() as s:
        u = User(status="active", email=email, is_admin=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = test_client_factory(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid


class _FakeJob:
    def __init__(self):
        self.next_run_time = "UNSET"

    def modify(self, **kwargs):
        self.next_run_time = kwargs["next_run_time"]


class _FakeScheduler:
    def __init__(self, jobs):
        self._jobs = dict(jobs)
        self.running = True  # trigger_job_now refuses a stopped scheduler

    def get_job(self, job_id):
        return self._jobs.get(job_id)

    def shutdown(self, wait=True):
        return None


def test_rerank_enqueues_for_active_profiles(
    monkeypatch, pg_url, pg_session_factory, profile_factory, test_client_factory
):
    pid, _ = profile_factory()
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
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    r = c.post("/api/admin/rerank")
    assert r.status_code == 200
    assert r.json() == {"enqueued": 1}  # one ACTIVE PROFILE, not job count
    with pg_session_factory() as s:
        assert len(s.execute(select(RankingJob)).scalars().all()) == 1


def test_rerank_requires_admin(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="na@ki.se", is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = test_client_factory(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    assert c.post("/api/admin/rerank").status_code == 403


def test_send_digest_triggered(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    real = app.state.scheduler
    app.state.scheduler = _FakeScheduler({"digest:weekly": _FakeJob()})
    try:
        r = c.post("/api/admin/digest/send")
    finally:
        app.state.scheduler = real
    assert r.status_code == 200 and r.json() == {"triggered": True}


def test_send_digest_unconfigured(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    real = app.state.scheduler
    app.state.scheduler = _FakeScheduler({})
    try:
        r = c.post("/api/admin/digest/send")
    finally:
        app.state.scheduler = real
    assert r.status_code == 200 and r.json() == {"triggered": False}


def test_reenable_clears_and_returns_id(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    with pg_session_factory() as s:
        u = User(status="active", email="bounced@ki.se", digest_disabled_at=datetime.now(UTC))
        s.add(u)
        s.commit()
        s.refresh(u)
        tid = u.id
    r = c.post("/api/admin/digest/reenable", json={"user_id": tid})
    assert r.status_code == 200 and r.json() == {"id": tid}
    with pg_session_factory() as s:
        assert s.get(User, tid).digest_disabled_at is None


def test_reenable_already_enabled_noop(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    with pg_session_factory() as s:
        u = User(status="active", email="fine@ki.se", digest_disabled_at=None)
        s.add(u)
        s.commit()
        s.refresh(u)
        tid = u.id
    assert c.post("/api/admin/digest/reenable", json={"user_id": tid}).status_code == 200


def test_reenable_unknown_404(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    r = c.post("/api/admin/digest/reenable", json={"user_id": 999999})
    assert r.status_code == 404


def test_fetch_source_triggered(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    real = app.state.scheduler
    app.state.scheduler = _FakeScheduler({"ingest:vinnova": _FakeJob()})
    try:
        r = c.post("/api/admin/sources/vinnova/fetch")
    finally:
        app.state.scheduler = real
    assert r.status_code == 200 and r.json() == {"triggered": True}


def test_fetch_source_job_missing(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    real = app.state.scheduler
    app.state.scheduler = _FakeScheduler({})
    try:
        r = c.post("/api/admin/sources/vinnova/fetch")
    finally:
        app.state.scheduler = real
    assert r.status_code == 200 and r.json() == {"triggered": False}


def test_fetch_unknown_slug_404(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    assert c.post("/api/admin/sources/does-not-exist/fetch").status_code == 404
