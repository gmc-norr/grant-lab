from datetime import UTC, datetime

from fastapi.testclient import TestClient

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _admin_client(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="root@ki.se", is_admin=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_reenable_clears_digest_disabled_at(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        bounced = User(email="bounced@ki.se", digest_disabled_at=datetime.now(UTC))
        s.add(bounced)
        s.commit()
        s.refresh(bounced)
        target_id = bounced.id

    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.post(
            "/admin/digest/reenable",
            data={"user_id": str(target_id)},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?ok=reenable"
    with pg_session_factory() as s:
        refreshed = s.get(User, target_id)
        assert refreshed.digest_disabled_at is None


def test_reenable_already_enabled_is_noop_ok(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        enabled = User(email="fine@ki.se", digest_disabled_at=None)
        s.add(enabled)
        s.commit()
        s.refresh(enabled)
        target_id = enabled.id

    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.post(
            "/admin/digest/reenable",
            data={"user_id": str(target_id)},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?ok=reenable"


def test_reenable_unknown_user_404(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.post(
            "/admin/digest/reenable",
            data={"user_id": "999999"},
            follow_redirects=False,
        )
    assert r.status_code == 404


def test_reenable_requires_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="nonadmin@ki.se", is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.post("/admin/digest/reenable", data={"user_id": "1"})
    assert r.status_code == 403


class _FakeJob:
    def __init__(self) -> None:
        self.next_run_time = "UNSET"

    def modify(self, **kwargs) -> None:
        self.next_run_time = kwargs["next_run_time"]


class _FakeScheduler:
    """Minimal stand-in for the APScheduler instance on app.state.scheduler.

    A no-op shutdown() so lifespan teardown is harmless even if a test forgets
    to restore — but tests DO restore (swap-then-restore) so the real scheduler
    is the one that actually shuts down."""

    def __init__(self, jobs: dict) -> None:
        self._jobs = dict(jobs)

    def get_job(self, job_id: str):
        return self._jobs.get(job_id)

    def shutdown(self, wait: bool = True) -> None:
        return None


def test_fetch_source_nudges_ingest_job(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        real = app.state.scheduler
        job = _FakeJob()
        app.state.scheduler = _FakeScheduler({"ingest:vinnova": job})
        try:
            r = client.post("/admin/sources/vinnova/fetch", follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"] == "/admin?ok=fetch"
            assert isinstance(job.next_run_time, datetime)
        finally:
            app.state.scheduler = real


def test_fetch_unknown_source_404(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.post("/admin/sources/does-not-exist/fetch", follow_redirects=False)
    assert r.status_code == 404


def test_fetch_err_when_job_missing(monkeypatch, pg_url, pg_session_factory):
    # Valid source row, but no ingest job in the (fake) scheduler — e.g. a
    # stale DB row whose source was removed from the registry.
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        real = app.state.scheduler
        app.state.scheduler = _FakeScheduler({})
        try:
            r = client.post("/admin/sources/vinnova/fetch", follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"] == "/admin?err=fetch"
        finally:
            app.state.scheduler = real


def test_send_digest_nudges_weekly_job(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        real = app.state.scheduler
        job = _FakeJob()
        app.state.scheduler = _FakeScheduler({"digest:weekly": job})
        try:
            r = client.post("/admin/digest/send", follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"] == "/admin?ok=digest"
            assert isinstance(job.next_run_time, datetime)
        finally:
            app.state.scheduler = real


def test_send_digest_unconfigured_err(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        real = app.state.scheduler
        app.state.scheduler = _FakeScheduler({})  # no digest:weekly job
        try:
            r = client.post("/admin/digest/send", follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"] == "/admin?err=digest_unconfigured"
        finally:
            app.state.scheduler = real


def test_fetch_requires_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="nonadmin2@ki.se", is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.post("/admin/sources/vinnova/fetch")
    assert r.status_code == 403


def test_rerank_redirects_with_ok_key(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.post("/admin/rerank", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?ok=rerank"


def test_ok_fetch_renders_success_banner(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin?ok=fetch")
    assert r.status_code == 200
    assert "banner-ok" in r.text
    assert "Fetch triggered" in r.text


def test_err_digest_unconfigured_renders_warning_banner(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin?err=digest_unconfigured")
    assert r.status_code == 200
    assert "banner-warn" in r.text
    assert "isn&#39;t configured" in r.text


def test_unknown_banner_key_renders_no_banner(monkeypatch, pg_url, pg_session_factory):
    # A tampered/garbage key must NOT be echoed and must NOT render a banner.
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin?ok=<script>alert(1)</script>")
    assert r.status_code == 200
    assert "banner-ok" not in r.text
    assert "alert(1)" not in r.text  # raw param never echoed
