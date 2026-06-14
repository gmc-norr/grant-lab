from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Call, CallRanking, Source, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _logged_in(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="user@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def _top_matches_value(html: str) -> str:
    """Extract the numeric text of the 'Top matches' summary-card value.
    The value is whitespace-wrapped in the template, so a bare '>N</div>'
    substring check can't pin it — parse the card structurally instead."""
    label = html.index("Top matches")
    value_start = html.index('class="value', label)
    gt = html.index(">", value_start)
    end = html.index("</div>", gt)
    return html[gt + 1 : end].strip()


def _seed(pg_session_factory, profile_id):
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c1 = Call(
            source_id=src.id,
            external_id="A",
            url="https://x/a",
            title="High score",
            published_at=datetime(2026, 4, 1, tzinfo=UTC),
        )
        c2 = Call(
            source_id=src.id,
            external_id="B",
            url="https://x/b",
            title="Low score",
            published_at=datetime(2026, 4, 2, tzinfo=UTC),
        )
        c3 = Call(
            source_id=src.id,
            external_id="C",
            url="https://x/c",
            title="Unranked",
            published_at=datetime(2026, 4, 3, tzinfo=UTC),
        )
        s.add_all([c1, c2, c3])
        s.flush()
        s.add(
            CallRanking(
                call_id=c1.id,
                profile_id=profile_id,
                aggregate_score=85,
                rationale="Great",
                model="gemma4:e4b-q8",
                prompt_version="v2.0",
            )
        )
        s.add(
            CallRanking(
                call_id=c2.id,
                profile_id=profile_id,
                aggregate_score=15,
                rationale="Poor",
                model="gemma4:e4b-q8",
                prompt_version="v2.0",
            )
        )
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


def test_profile_view_closed_call_never_outranks_open(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """A high-scored CLOSED call is hidden by default; even when shown it
    sorts BELOW a lower-scored open call (structural closed-last sort)."""
    profile_id, _ = profile_factory()
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        closed = Call(
            source_id=src.id,
            external_id="CL",
            url="https://x/cl",
            title="Closed high",
            published_at=datetime(2026, 4, 1, tzinfo=UTC),
            deadline=now - timedelta(days=5),
        )
        openc = Call(
            source_id=src.id,
            external_id="OP",
            url="https://x/op",
            title="Open low",
            published_at=datetime(2026, 4, 2, tzinfo=UTC),
            deadline=now + timedelta(days=20),
        )
        s.add_all([closed, openc])
        s.flush()
        s.add(
            CallRanking(
                call_id=closed.id,
                profile_id=profile_id,
                aggregate_score=95,
                rationale="x",
                model="gemma4:e4b-q8",
                prompt_version="v2.0",
            )
        )
        s.add(
            CallRanking(
                call_id=openc.id,
                profile_id=profile_id,
                aggregate_score=20,
                rationale="x",
                model="gemma4:e4b-q8",
                prompt_version="v2.0",
            )
        )
        s.commit()
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        default = client.get(f"/?profile={profile_id}")
        shown = client.get(f"/?profile={profile_id}&show_closed=1")
    assert "Closed high" not in default.text  # hidden despite score 95
    assert "Open low" in default.text
    assert "Closed high" in shown.text and "Open low" in shown.text
    assert shown.text.index("Open low") < shown.text.index("Closed high")


def test_top_matches_badge_excludes_closed_high_scorer(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """A closed call scoring >=70 must not be counted as a 'Top match'."""
    profile_id, _ = profile_factory()
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        closed = Call(
            source_id=src.id,
            external_id="CH",
            url="https://x/ch",
            title="Closed top",
            published_at=datetime(2026, 4, 1, tzinfo=UTC),
            deadline=now - timedelta(days=3),
        )
        openc = Call(
            source_id=src.id,
            external_id="OH",
            url="https://x/oh",
            title="Open top",
            published_at=datetime(2026, 4, 2, tzinfo=UTC),
            deadline=now + timedelta(days=20),
        )
        s.add_all([closed, openc])
        s.flush()
        s.add(
            CallRanking(
                call_id=closed.id,
                profile_id=profile_id,
                aggregate_score=95,
                rationale="x",
                model="gemma4:e4b-q8",
                prompt_version="v2.0",
            )
        )
        s.add(
            CallRanking(
                call_id=openc.id,
                profile_id=profile_id,
                aggregate_score=75,
                rationale="x",
                model="gemma4:e4b-q8",
                prompt_version="v2.0",
            )
        )
        s.commit()
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        default = client.get(f"/?profile={profile_id}")
        shown = client.get(f"/?profile={profile_id}&show_closed=1")
    # Top matches = 1 (the open 75), not 2, in BOTH views. An impl that counted
    # high_score over the toggled list_filters would exclude the closed 95 by
    # default but wrongly count it when show_closed=1 → a badge value of 2.
    # Top matches = 1 (the open 75), not 2, in BOTH toggle states. Extract the
    # card value structurally: the value is whitespace-wrapped, so a ">2</div>"
    # substring check would never match the Top-matches card and thus could not
    # catch a high_score=2 regression.
    assert _top_matches_value(default.text) == "1"
    assert _top_matches_value(shown.text) == "1"


def test_root_inactive_profile_degrades_to_unranked(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Spec: inactive profiles are removed from feeds. ?profile=<inactive>
    MUST NOT show the stored ranking scores for that profile — otherwise
    a soft-delete wouldn't actually remove audit data from the UI."""
    from call_manager.db.models import CallRanking, Profile

    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id="I-1",
            url="https://x/i",
            title="Should be hidden",
            published_at=datetime(2026, 4, 1, tzinfo=UTC),
        )
        s.add(c)
        s.flush()
        s.add(
            CallRanking(
                call_id=c.id,
                profile_id=profile_id,
                aggregate_score=99,
                rationale="hidden",
                model="gemma4:e4b-q8",
                prompt_version="v2.0",
            )
        )
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
