from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

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
        u = User(email="root@ki.se", is_admin=True, status="active")
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
        u = User(email="nonadmin@ki.se", is_admin=False, status="active")
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
            assert r.headers["location"] == "/admin?ok=fetch&src=vinnova"
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
            assert r.headers["location"] == "/admin?err=fetch&src=vinnova"
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
        u = User(email="nonadmin2@ki.se", is_admin=False, status="active")
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


@pytest.mark.parametrize(
    ("param", "css_class", "snippet"),
    [
        ("ok=fetch", "banner-ok", "Fetch triggered"),
        ("ok=digest", "banner-ok", "Weekly digest triggered"),
        ("ok=reenable", "banner-ok", "User re-enabled"),
        ("ok=rerank", "banner-ok", "Re-ranking enqueued"),
        ("err=fetch", "banner-warn", "Couldn&#39;t trigger the fetch"),
        ("err=digest_unconfigured", "banner-warn", "isn&#39;t configured"),
    ],
)
def test_every_banner_key_renders_its_message(
    monkeypatch, pg_url, pg_session_factory, param, css_class, snippet
):
    """Every redirect key a lever route emits maps to a rendered banner with
    the right class and an autoescaped message; locks the key→message contract."""
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get(f"/admin?{param}")
    assert r.status_code == 200
    assert css_class in r.text
    assert snippet in r.text


def test_unknown_banner_key_renders_no_banner(monkeypatch, pg_url, pg_session_factory):
    # A tampered/garbage key must NOT be echoed and must NOT render a banner.
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin?ok=<script>alert(1)</script>")
    assert r.status_code == 200
    assert "banner-ok" not in r.text
    assert "alert(1)" not in r.text  # raw param never echoed


@pytest.mark.parametrize("key", ["ok=fetch", "err=fetch"])
def test_fetch_banner_names_the_source(monkeypatch, pg_url, pg_session_factory, key):
    # The fetch banner names the source via a DB lookup of the `src` slug —
    # the rendered text is the trusted Source.name, never the param itself.
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get(f"/admin?{key}&src=vinnova")
    assert r.status_code == 200
    assert '<span class="muted">(Vinnova)</span>' in r.text


def test_fetch_banner_never_echoes_unknown_src(monkeypatch, pg_url, pg_session_factory):
    # A tampered `src` matches no source row: the base message still renders,
    # the suffix is absent, and the raw value never appears in the page.
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin?ok=fetch&src=<script>alert(1)</script>")
    assert r.status_code == 200
    assert "Fetch triggered" in r.text
    assert "alert(1)" not in r.text  # raw src never echoed


def test_source_rows_have_fetch_forms(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    # vinnova is a migration-seeded source; its row carries a fetch form.
    assert 'action="/admin/sources/vinnova/fetch"' in r.text
    assert "Fetch now" in r.text


def test_digest_panel_lists_disabled_user_with_reenable_form(
    monkeypatch, pg_url, pg_session_factory
):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        s.add(User(email="bounced2@ki.se", digest_disabled_at=datetime.now(UTC)))
        s.commit()
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    # The disabled user's email is listed (not just a count)…
    assert "bounced2@ki.se" in r.text
    # …with a re-enable form targeting the lever route.
    assert 'action="/admin/digest/reenable"' in r.text
    assert "Re-enable" in r.text
    # And the send-digest lever is present.
    assert 'action="/admin/digest/send"' in r.text
    assert "Send digest now" in r.text


def test_digest_panel_shows_none_when_no_disabled_users(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    assert "No disabled users." in r.text


def test_disabled_users_listed_most_recent_first(monkeypatch, pg_url, pg_session_factory):
    from datetime import timedelta

    app = _setup(monkeypatch, pg_url)
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        # older bounce
        s.add(User(email="older@ki.se", digest_disabled_at=now - timedelta(days=3)))
        # newer bounce
        s.add(User(email="newer@ki.se", digest_disabled_at=now - timedelta(hours=1)))
        s.commit()
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    # Both listed…
    assert "older@ki.se" in r.text
    assert "newer@ki.se" in r.text
    # …and the most-recently-disabled user comes first (desc order).
    assert r.text.index("newer@ki.se") < r.text.index("older@ki.se")


def test_rerank_button_shows_estimate_and_confirm(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    from datetime import timedelta

    from call_manager.db.models import Call, Source

    # Two active profiles × three recent calls ⇒ estimate of 6 (pins the
    # multiplication: a stray + / max / single-factor bug would not give 6).
    profile_factory()
    profile_factory(name="Group B")
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        for i in range(3):
            s.add(
                Call(
                    source_id=src.id,
                    external_id=f"E{i}",
                    url=f"https://x/e{i}",
                    title="T",
                    published_at=datetime.now(UTC) - timedelta(days=5),
                )
            )
        s.commit()

    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    # The destructive form carries a confirm prompt…
    assert "data-confirm=" in r.text
    # …and the page states the upper-bound job count (2 profiles × 3 calls = 6).
    assert "up to ~6" in r.text
    # …with the breakdown rendered.
    assert "2 active profiles × 3 recent calls" in r.text
