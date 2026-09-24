from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from grant_lab.db.models import Call, RankingJob, Source, User
from grant_lab.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _admin(app, sf, test_client_factory, *, email="root@umu.se"):
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
                deadline=datetime(2099, 1, 1, tzinfo=UTC),
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


def test_rerank_enqueues_fresh_and_suppressed_open_pairs_but_not_closed(
    monkeypatch, pg_url, pg_session_factory, profile_factory, test_client_factory
):
    """G23 (gap 2, D5): the override ignores freshness AND suppression."""
    from grant_lab.db.models import CallRanking, Profile, RankingJobStatus
    from grant_lab.profiles.fingerprint import call_rank_fingerprint, rank_fingerprint
    from tests.freshness_helpers import ctx_for

    pid, _ = profile_factory()
    ctx = ctx_for()
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        fresh, suppressed, closed = (
            Call(source_id=src.id, external_id=e, url=f"https://x/{e}", title=e, deadline=d)
            for e, d in (
                ("FRESH", datetime(2099, 1, 1, tzinfo=UTC)),
                ("SUPP", datetime(2099, 1, 1, tzinfo=UTC)),
                ("CLOSED", datetime.now(UTC) - timedelta(days=1)),
            )
        )
        s.add_all([fresh, suppressed, closed])
        s.flush()
        s.add(
            CallRanking(
                call_id=fresh.id,
                profile_id=pid,
                aggregate_score=60,
                rationale="",
                model="m",
                prompt_version="v5.0",
                raw_response={},
                profile_fingerprint=rank_fingerprint(s.get(Profile, pid)),
                call_fingerprint=call_rank_fingerprint(fresh),
                scoring_fingerprint=ctx.scoring.fingerprint(),
            )
        )
        s.add(
            RankingJob(
                call_id=suppressed.id,
                profile_id=pid,
                status=RankingJobStatus.FAILED,
                attempts=5,
                last_error="x",
            )
        )
        s.commit()
        want = {fresh.id, suppressed.id}
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    assert c.post("/api/admin/rerank").status_code == 200
    with pg_session_factory() as s:
        pending = {
            j.call_id
            for j in s.execute(
                select(RankingJob).where(RankingJob.status == RankingJobStatus.PENDING)
            ).scalars()
        }
    assert pending == want


def test_rerank_requires_admin(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="na@umu.se", is_admin=False)
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
        u = User(status="active", email="bounced@umu.se", digest_disabled_at=datetime.now(UTC))
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
        u = User(status="active", email="fine@umu.se", digest_disabled_at=None)
        s.add(u)
        s.commit()
        s.refresh(u)
        tid = u.id
    r = c.post("/api/admin/digest/reenable", json={"user_id": tid})
    # Review 2026-09-02 A6 F3: this asserted only `== 200`, so a "reenable" that
    # unconditionally stamps `digest_disabled_at = now()` on an already-enabled
    # user passed the very test whose name says it is a NO-OP. The sibling above
    # makes exactly this assertion for the disable->enable case.
    assert r.status_code == 200 and r.json() == {"id": tid}
    with pg_session_factory() as s:
        assert s.get(User, tid).digest_disabled_at is None, (
            "reenabling an already-enabled user disabled their digest"
        )


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
