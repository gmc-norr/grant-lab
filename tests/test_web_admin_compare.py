from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import Call, CallRanking, Source, User
from call_monitor.main import create_app


def _setup_admin(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", "admin@ki.se")
    app = create_app()
    with TestClient(app):  # trigger lifespan → bootstrap admin
        pass
    return app


def _admin_cookie(app, pg_session_factory):
    with pg_session_factory() as s:
        u = s.execute(select(User).where(User.email == "admin@ki.se")).scalar_one()
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_compare_view_renders_dual_scheme_rows(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="Compare profile")
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="C", url="https://x", title="CompareCall",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(CallRanking(
            call_id=c.id, profile_id=pid, scheme="v1",
            aggregate_score=70, rationale="v1 rationale",
            model="m", prompt_version="v1", ranked_at=datetime.now(UTC),
        ))
        s.add(CallRanking(
            call_id=c.id, profile_id=pid, scheme="v2",
            aggregate_score=85, rationale="Topic 4/5 · Eligibility: pass · Exclusions: pass",
            model="m", prompt_version="v2.0", ranked_at=datetime.now(UTC),
        ))
        s.commit()

    app = _setup_admin(monkeypatch, pg_url)
    client = _admin_cookie(app, pg_session_factory)
    with client:
        r = client.get(f"/admin/ranking-compare?profile={pid}")
    assert r.status_code == 200
    assert "CompareCall" in r.text
    assert "70" in r.text
    assert "85" in r.text
    assert "v1 rationale" in r.text
    assert "Topic 4/5" in r.text
    # Δ column
    assert "15" in r.text  # |85-70|


def test_compare_view_requires_admin(monkeypatch, pg_url, pg_session_factory):
    """Non-admin user gets 403 (or 401)."""
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()
    with pg_session_factory() as s:
        u = User(email="notadmin@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.get("/admin/ranking-compare?profile=1")
    assert r.status_code in (401, 403)


def test_compare_view_missing_profile_param_returns_400(
    monkeypatch, pg_url, pg_session_factory
):
    app = _setup_admin(monkeypatch, pg_url)
    client = _admin_cookie(app, pg_session_factory)
    with client:
        r = client.get("/admin/ranking-compare")
    assert r.status_code == 400


def test_compare_view_unknown_profile_returns_404(
    monkeypatch, pg_url, pg_session_factory
):
    """profile=<nonexistent id> → 404."""
    app = _setup_admin(monkeypatch, pg_url)
    client = _admin_cookie(app, pg_session_factory)
    with client:
        r = client.get("/admin/ranking-compare?profile=999999")
    assert r.status_code == 404


def test_compare_view_uses_display_score_with_setwise_offset(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """C1 regression: ranking-compare must show v2's display_score (offset-
    adjusted) and compute the v1↔v2 delta against display_score, not bare
    aggregate. Otherwise the compare view would silently disagree with
    every other surface (calls index, call detail, digest)."""
    pid, _ = profile_factory(name="Compare offset")
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="CMP-OFFSET", url="https://x/cmp",
            title="CompareOffsetCall",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(CallRanking(
            call_id=c.id, profile_id=pid, scheme="v1",
            aggregate_score=70, setwise_offset=0,  # v1 always 0
            rationale="v1 baseline",
            model="m", prompt_version="v1", ranked_at=datetime.now(UTC),
        ))
        s.add(CallRanking(
            call_id=c.id, profile_id=pid, scheme="v2",
            aggregate_score=55, setwise_offset=5,  # display = 60
            rationale="v2 borderline lifted",
            model="m", prompt_version="v2.0", ranked_at=datetime.now(UTC),
        ))
        s.commit()

    app = _setup_admin(monkeypatch, pg_url)
    client = _admin_cookie(app, pg_session_factory)
    with client:
        r = client.get(f"/admin/ranking-compare?profile={pid}")
    assert r.status_code == 200
    # v2 column shows the OFFSET-ADJUSTED score (60), not the bare 55.
    import re
    row = re.search(
        r"CompareOffsetCall.*?>70<.*?>60<.*?>10<",
        r.text, re.DOTALL,
    )
    assert row, (
        "compare view must show v1=70, v2=60 (=55+5), |Δ|=10 (=|70-60|). "
        "If we read the bare aggregate, v2 would be 55 and Δ would be 15."
    )


def test_compare_view_renders_em_dash_when_only_one_scheme(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """A call with only a v1 ranking shows em-dash for v2 score and delta;
    a call with only a v2 ranking shows em-dash for v1 score and delta."""
    pid, _ = profile_factory(name="Single scheme")
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        # Call A: v1 only
        ca = Call(
            source_id=src.id, external_id="A", url="https://x/a", title="OnlyV1",
            published_at=datetime.now(UTC),
        )
        s.add(ca)
        s.flush()
        s.add(CallRanking(
            call_id=ca.id, profile_id=pid, scheme="v1", aggregate_score=42,
            rationale="just v1", model="m", prompt_version="v1",
            ranked_at=datetime.now(UTC),
        ))
        # Call B: v2 only
        cb = Call(
            source_id=src.id, external_id="B", url="https://x/b", title="OnlyV2",
            published_at=datetime.now(UTC),
        )
        s.add(cb)
        s.flush()
        s.add(CallRanking(
            call_id=cb.id, profile_id=pid, scheme="v2", aggregate_score=88,
            rationale="just v2", model="m", prompt_version="v2.0",
            ranked_at=datetime.now(UTC),
        ))
        s.commit()

    app = _setup_admin(monkeypatch, pg_url)
    client = _admin_cookie(app, pg_session_factory)
    with client:
        r = client.get(f"/admin/ranking-compare?profile={pid}")
    assert r.status_code == 200
    assert "OnlyV1" in r.text
    assert "OnlyV2" in r.text
    # Tighten: assert each row has em-dash in the v2/v1 score and delta cells.
    # Use a regex anchored to the call title.
    import re
    # OnlyV1 row: 42 in v1 col, em-dash in v2 col, em-dash in delta col
    onlyv1 = re.search(
        r'OnlyV1.*?>42<.*?—.*?—',
        r.text, re.DOTALL,
    )
    assert onlyv1, "OnlyV1 row missing em-dashes for v2 score and delta"
    onlyv2 = re.search(
        r'OnlyV2.*?—.*?>88<.*?—',
        r.text, re.DOTALL,
    )
    assert onlyv2, "OnlyV2 row missing em-dashes for v1 score and delta"
