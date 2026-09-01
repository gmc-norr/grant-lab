from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from call_manager.db.models import (
    Call,
    DigestRun,
    Profile,
    ProfileKind,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from call_manager.main import create_app


def _setup(monkeypatch, pg_url, *, model="bge-m3"):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("EMBEDDING_MODEL", model)
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


def _nonadmin(app, sf, test_client_factory, *, email="alice@ki.se"):
    with sf() as s:
        u = User(status="active", email=email, is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = test_client_factory(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c


def test_dashboard_requires_admin(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _setup(monkeypatch, pg_url)
    c = _nonadmin(app, pg_session_factory, test_client_factory)
    assert c.get("/api/admin/dashboard").status_code == 403


def test_dashboard_requires_auth(monkeypatch, pg_url, test_client_factory):
    app = _setup(monkeypatch, pg_url)
    r = test_client_factory(app).get("/api/admin/dashboard")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_dashboard_lists_sources_with_call_counts(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        src.last_fetched_at = datetime(2026, 4, 16, 3, 15, tzinfo=UTC)
        s.add(Call(source_id=src.id, external_id="X", url="https://x", title="T"))
        s.commit()
    body = c.get("/api/admin/dashboard").json()
    row = next(x for x in body["sources"] if x["slug"] == "vinnova")
    assert row["name"] == "Vinnova"
    assert row["call_count"] == 1
    assert row["last_fetched_at"].startswith("2026-04-16T03:15")


def test_dashboard_queue_counts_default_zero(
    monkeypatch, pg_url, pg_session_factory, profile_factory, test_client_factory
):
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c0 = Call(source_id=src.id, external_id="Q", url="https://x/q", title="T")
        s.add(c0)
        s.flush()
        s.add(RankingJob(call_id=c0.id, profile_id=profile_id, status=RankingJobStatus.PENDING))
        s.commit()
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    counts = c.get("/api/admin/dashboard").json()["queue_counts"]
    assert counts == {"pending": 1, "running": 0, "done": 0, "failed": 0}


def test_dashboard_rerank_estimate_is_product(
    monkeypatch, pg_url, pg_session_factory, profile_factory, test_client_factory
):
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
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    body = c.get("/api/admin/dashboard").json()
    assert body["active_profiles_count"] == 2
    assert body["recent_calls_count"] == 3
    assert body["rerank_estimate"] == 6
    assert body["backfill_days"] == 90


def test_dashboard_digest_panel(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _setup(monkeypatch, pg_url)
    c, uid = _admin(app, pg_session_factory, test_client_factory)
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        s.add(DigestRun(user_id=uid, sent_at=now, included_call_ids=[1, 2]))
        s.add(
            User(status="active", email="older@ki.se", digest_disabled_at=now - timedelta(days=3))
        )
        s.add(
            User(status="active", email="newer@ki.se", digest_disabled_at=now - timedelta(hours=1))
        )
        s.commit()
    d = c.get("/api/admin/dashboard").json()["digest"]
    assert d["runs_this_week"] == 1
    assert d["last_sent_at"] is not None
    assert d["disabled_users"] == 2
    emails = [r["email"] for r in d["disabled_user_rows"]]
    assert emails == ["newer@ki.se", "older@ki.se"]  # most-recent-first
    assert set(d["disabled_user_rows"][0]) == {"id", "email", "disabled_at"}


def test_dashboard_embedding_coverage(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    vec = [0.0] * 1024
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add(
            Call(
                source_id=src.id,
                external_id="c1",
                url="https://x/1",
                title="C1",
                embedding=vec,
                embedding_model="bge-m3",
                published_at=datetime.now(UTC),
            )
        )
        s.add(
            Call(
                source_id=src.id,
                external_id="c2",
                url="https://x/2",
                title="C2",
                embedding=vec,
                embedding_model="bge-old",
                published_at=datetime.now(UTC),
            )
        )
        s.add(
            Call(
                source_id=src.id,
                external_id="c3",
                url="https://x/3",
                title="C3",
                embedding=None,
                embedding_model=None,
                published_at=datetime.now(UTC),
            )
        )
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        s.add(
            Profile(
                name="P1",
                kind=ProfileKind.ORGANIZATIONAL_UNIT,
                description="d",
                structured={},
                created_by=owner.id,
                embedding=vec,
                embedding_model="bge-m3",
            )
        )
        s.add(
            Profile(
                name="P2",
                kind=ProfileKind.ORGANIZATIONAL_UNIT,
                description="d",
                structured={},
                created_by=owner.id,
                embedding=None,
                embedding_model=None,
            )
        )
        s.commit()
    cov = c.get("/api/admin/dashboard").json()["embedding_coverage"]
    assert cov["call_embedded"] == 1 and cov["call_total"] == 3 and cov["call_stale"] == 2
    assert cov["profile_embedded"] == 1 and cov["profile_total"] == 2 and cov["profile_stale"] == 1
    assert cov["current_model"] == "bge-m3"


def test_dashboard_coverage_excludes_blank_profiles_counts_areas_only(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    with pg_session_factory() as s:
        owner = User(status="active", email="owner2@ki.se")
        s.add(owner)
        s.flush()
        # blank description, no areas -> excluded from total entirely
        s.add(
            Profile(
                name="PB",
                kind=ProfileKind.ORGANIZATIONAL_UNIT,
                description="   ",
                structured={},
                created_by=owner.id,
                embedding=None,
                embedding_model=None,
            )
        )
        # areas-only (blank description) -> embeddable -> counted, stale
        s.add(
            Profile(
                name="PA",
                kind=ProfileKind.ORGANIZATIONAL_UNIT,
                description="",
                structured={"areas": ["genomics"]},
                created_by=owner.id,
                embedding=None,
                embedding_model=None,
            )
        )
        s.commit()
    cov = c.get("/api/admin/dashboard").json()["embedding_coverage"]
    assert cov["profile_total"] == 1  # blank excluded, areas-only included
    assert cov["profile_stale"] == 1


def test_dashboard_golden_set_report_null(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    # load_last_report reads a sidecar file whose path is fixed at import time,
    # so monkeypatch the function (the endpoint lazy-imports it) for determinism.
    monkeypatch.setattr("call_manager.ranking.eval.load_last_report", lambda: None)
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    assert c.get("/api/admin/dashboard").json()["golden_set_report"] is None


def test_dashboard_golden_set_report_present(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    from call_manager.ranking.eval import GoldenSetReport

    monkeypatch.setattr(
        "call_manager.ranking.eval.load_last_report",
        lambda: GoldenSetReport(n_fixture=5, n_compared=4, tau=0.812),
    )
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    rep = c.get("/api/admin/dashboard").json()["golden_set_report"]
    assert rep == {"n_fixture": 5, "n_compared": 4, "tau": 0.812}


def test_dashboard_calibration_warning(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    app = _setup(monkeypatch, pg_url, model="mxbai-embed-large")
    assert app.state.embedding_calibration_warning is not None
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    w = c.get("/api/admin/dashboard").json()["calibration_warning"]
    assert w is not None and "EMBEDDING_HARD_FAIL_THRESHOLD" in w


def test_dashboard_no_calibration_warning_known_model(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    app = _setup(monkeypatch, pg_url, model="bge-m3")
    c, _ = _admin(app, pg_session_factory, test_client_factory)
    assert c.get("/api/admin/dashboard").json()["calibration_warning"] is None
