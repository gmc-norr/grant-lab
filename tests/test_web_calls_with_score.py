from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import Call, CallRanking, Source, User
from call_monitor.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _logged_in(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="user@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def _seed(pg_session_factory, profile_id):
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c1 = Call(
            source_id=src.id, external_id="A", url="https://x/a", title="High score",
            published_at=datetime(2026, 4, 1, tzinfo=UTC),
        )
        c2 = Call(
            source_id=src.id, external_id="B", url="https://x/b", title="Low score",
            published_at=datetime(2026, 4, 2, tzinfo=UTC),
        )
        c3 = Call(
            source_id=src.id, external_id="C", url="https://x/c", title="Unranked",
            published_at=datetime(2026, 4, 3, tzinfo=UTC),
        )
        s.add_all([c1, c2, c3])
        s.flush()
        s.add(CallRanking(call_id=c1.id, profile_id=profile_id, aggregate_score=85,
                          rationale="Great", model="gemma4:e4b-q8", prompt_version="v1"))
        s.add(CallRanking(call_id=c2.id, profile_id=profile_id, aggregate_score=15,
                          rationale="Poor", model="gemma4:e4b-q8", prompt_version="v1"))
        s.commit()


def test_root_with_profile_orders_by_score_desc(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    profile_id, _ = profile_factory()
    app = _setup(monkeypatch, pg_url)
    _seed(pg_session_factory, profile_id)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get(f"/?profile={profile_id}")
    assert r.status_code == 200
    body = r.text
    # High score first, then low, then unranked (NULL → last)
    assert body.index("High score") < body.index("Low score") < body.index("Unranked")
    # Score column appears for the ranked rows
    assert "85" in body
    assert "15" in body


def test_root_without_profile_still_uses_published_at(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    profile_id, _ = profile_factory()
    app = _setup(monkeypatch, pg_url)
    _seed(pg_session_factory, profile_id)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/")
    assert r.status_code == 200
    body = r.text
    # No ?profile → published-at desc → C, B, A
    assert body.index("Unranked") < body.index("Low score") < body.index("High score")


def test_root_unknown_profile_degrades_to_unranked(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Bogus ?profile=<id> falls back to the unranked view (no Score
    column). Spec requires inactive profiles to be absent from feeds,
    and an unknown id is treated the same way so stale bookmarks don't
    leak audit data or error out."""
    profile_factory()  # create a real one but request a different id
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add(Call(source_id=src.id, external_id="U-1", url="https://x", title="Visible call"))
        s.commit()

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?profile=99999")
    assert r.status_code == 200
    # Score column HEADER must NOT be present — this is the unranked view.
    assert "<th>Score</th>" not in r.text
    assert "Visible call" in r.text


def test_root_inactive_profile_degrades_to_unranked(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Spec: inactive profiles are removed from feeds. ?profile=<inactive>
    MUST NOT show the stored ranking scores for that profile — otherwise
    a soft-delete wouldn't actually remove audit data from the UI."""
    from call_monitor.db.models import CallRanking, Profile
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="I-1", url="https://x/i",
            title="Should be hidden",
            published_at=datetime(2026, 4, 1, tzinfo=UTC),
        )
        s.add(c)
        s.flush()
        s.add(CallRanking(
            call_id=c.id, profile_id=profile_id, aggregate_score=99,
            rationale="hidden", model="gemma4:e4b-q8", prompt_version="v1",
        ))
        # Now soft-delete the profile
        s.get(Profile, profile_id).is_active = False
        s.commit()

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get(f"/?profile={profile_id}")
    assert r.status_code == 200
    # Unranked view — no Score column header
    assert "<th>Score</th>" not in r.text
    # The stored score must NOT leak into the response
    assert "99" not in r.text
    # The call itself still renders (it exists regardless of profile state)
    assert "Should be hidden" in r.text


def test_root_orders_by_display_score_with_setwise_offset(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """C1 regression: when two v2 borderline calls have equal aggregate_score
    but different setwise_offset, the +offset row must sort ahead of the
    -offset row in the calls index. Without display_score in the ORDER BY,
    setwise's daily ±5 nudge would be invisible to users.

    Also asserts the displayed score in the Score column is the offset-
    adjusted value, not the bare aggregate.
    """
    from call_monitor.db.models import CallRanking, Profile
    pid, _ = profile_factory(name="V2 ordered")
    with pg_session_factory() as s:
        # Profile must be on v2 — calls index joins on the active scheme.
        s.get(Profile, pid).ranking_scheme = "v2"
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        # Two calls, both aggregate=55. cA gets +5, cB gets -5 → display
        # 60 vs 50 → cA must sort first.
        cA = Call(
            source_id=src.id, external_id="setwise-A", url="https://x/a",
            title="OffsetPlus", published_at=datetime(2026, 4, 1, tzinfo=UTC),
        )
        cB = Call(
            source_id=src.id, external_id="setwise-B", url="https://x/b",
            title="OffsetMinus", published_at=datetime(2026, 4, 2, tzinfo=UTC),
        )
        s.add_all([cA, cB])
        s.flush()
        s.add(CallRanking(
            call_id=cA.id, profile_id=pid, scheme="v2",
            aggregate_score=55, setwise_offset=5,
            rationale="r", model="m", prompt_version="v2.0",
            ranked_at=datetime.now(UTC),
        ))
        s.add(CallRanking(
            call_id=cB.id, profile_id=pid, scheme="v2",
            aggregate_score=55, setwise_offset=-5,
            rationale="r", model="m", prompt_version="v2.0",
            ranked_at=datetime.now(UTC),
        ))
        s.commit()

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get(f"/?profile={pid}")
    assert r.status_code == 200
    body = r.text
    # OffsetPlus (display 60) sorts before OffsetMinus (display 50).
    assert body.index("OffsetPlus") < body.index("OffsetMinus"), (
        "display_score not driving ORDER BY: OffsetPlus (display 60) "
        "should sort ahead of OffsetMinus (display 50) when bare "
        "aggregates are tied at 55."
    )
    # Displayed score column shows the offset-adjusted value.
    assert ">60<" in body, "expected display_score 60 (=55+5) in Score column"
    assert ">50<" in body, "expected display_score 50 (=55-5) in Score column"


def test_dashboard_ranked_feed_filters_by_profile_ranking_scheme(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """If a (call, profile) pair has both v1 and v2 CallRanking rows, the
    dashboard for that profile must show only the row matching the profile's
    active ranking_scheme."""
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from call_monitor.auth.session_cookie import SessionSigner
    from call_monitor.db.models import Call, CallRanking, Source, User
    from call_monitor.main import create_app

    pid, _ = profile_factory(name="ProfileX")
    with pg_session_factory() as s:
        # Profile is on v1 by default.
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="DUAL", url="https://x", title="DualScheme",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(CallRanking(
            call_id=c.id, profile_id=pid, scheme="v1",
            aggregate_score=77, rationale="v1 says 77",
            model="m", prompt_version="v1", ranked_at=datetime.now(UTC),
        ))
        s.add(CallRanking(
            call_id=c.id, profile_id=pid, scheme="v2",
            aggregate_score=33, rationale="v2 says 33",
            model="m", prompt_version="v2.0", ranked_at=datetime.now(UTC),
        ))
        s.commit()

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()

    with pg_session_factory() as s:
        u = User(email="dashboard@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.get(f"/?profile={pid}")
    assert r.status_code == 200
    # v1 row (score 77) wins because profile.ranking_scheme defaults to 'v1'.
    assert "77" in r.text
    # v2 row (score 33) must NOT appear.
    assert "33" not in r.text
