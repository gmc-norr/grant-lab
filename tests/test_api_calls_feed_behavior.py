"""Feed-behavior parity coverage for the JSON ``GET /api/calls`` endpoint.

These assertions were PORTED from the legacy Jinja ``GET /`` dashboard tests
(``test_web_calls*.py``, ``test_ingestion_e2e.py``) when the server-rendered
dashboard handler (``calls_index``) was retired in favour of the React SPA.

``/api/calls`` reuses the SAME read-model service (``build_dashboard_view`` with
``persist=False``) the Jinja handler used, so these guard the exact ordering /
filtering / summary / pagination behaviors the HTML tests used to prove — only
now asserted on the JSON ``items`` order and the typed ``summary`` / ``pagination``
/ ``effective`` containers instead of HTML substring positions. Behaviors already
covered at the read-model layer (``test_dashboard_readmodel.py``) or by the
sticky-default WRITE path (``POST /api/me/default-profile`` —
``test_api_calls.py``) are not duplicated here; the GET path is read-only.
"""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.db.models import (
    Call,
    CallRanking,
    Profile,
    ProfileKind,
    Source,
    User,
    UserCallDismissal,
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


def _login(app, pg_session_factory, *, profile=False, profile_active=True, set_default=False):
    """Active user + optional active/inactive profile. Returns (client, uid, pid)
    (pid is None when profile=False). Profiles need not be OWNED by the user —
    the read-model resolves ``?profile`` by is_active alone."""
    with pg_session_factory() as s:
        u = User(status="active", email="u@ki.se")
        s.add(u)
        s.flush()
        pid = None
        if profile:
            p = Profile(
                name="Grp",
                kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
                level="group",
                description="d",
                structured={},
                created_by=u.id,
                is_active=profile_active,
            )
            s.add(p)
            s.flush()
            pid = p.id
            if set_default:
                u.default_profile_id = pid
        s.commit()
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid, pid


def _seed_open_calls(pg_session_factory):
    """Three OPEN calls (deadlines relative to now so they never close under
    wall-clock drift, and all > 30 days out so none land in the soon-deadline
    badge). Published order: EU two (Mar 1) < EU one (Apr 1) < Vin one (Apr 5).
    Deadline order: Vin one (45d) < EU two (75d) < EU one (105d). Mirrors the
    legacy ``_seed_calls`` so the ported ordering assertions match exactly."""
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        eu = s.execute(select(Source).where(Source.slug == "eu_portal")).scalar_one()
        vn = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add_all(
            [
                Call(
                    source_id=eu.id,
                    external_id="E-1",
                    url="u1",
                    title="EU one",
                    published_at=datetime(2026, 4, 1, tzinfo=UTC),
                    deadline=now + timedelta(days=105),
                ),
                Call(
                    source_id=vn.id,
                    external_id="V-1",
                    url="u2",
                    title="Vin one",
                    published_at=datetime(2026, 4, 5, tzinfo=UTC),
                    deadline=now + timedelta(days=45),
                ),
                Call(
                    source_id=eu.id,
                    external_id="E-2",
                    url="u3",
                    title="EU two",
                    published_at=datetime(2026, 3, 1, tzinfo=UTC),
                    deadline=now + timedelta(days=75),
                ),
            ]
        )
        s.commit()


def _add_closed_call(
    pg_session_factory, *, title="Closed call", external_id="C-1", published_at=None
):
    """One call whose deadline is in the past (closed). ``published_at`` defaults
    older than the open seeds; pass a newer date to prove closed calls still sort
    last under the default 'newest' sort."""
    now = datetime.now(UTC)
    if published_at is None:
        published_at = datetime(2026, 4, 2, tzinfo=UTC)
    with pg_session_factory() as s:
        eu = s.execute(select(Source).where(Source.slug == "eu_portal")).scalar_one()
        s.add(
            Call(
                source_id=eu.id,
                external_id=external_id,
                url="uc",
                title=title,
                published_at=published_at,
                deadline=now - timedelta(days=5),
            )
        )
        s.commit()


def _titles(body) -> list[str]:
    return [item["title"] for item in body["items"]]


def _scores(body) -> dict[str, int | None]:
    return {item["title"]: item["aggregate_score"] for item in body["items"]}


# --- auth -------------------------------------------------------------------


def test_api_calls_requires_auth(monkeypatch, pg_url, pg_session_factory):
    # pg_session_factory pulls in migrated_engine, so the shared testcontainer is
    # migrated before the app's startup scheduler runs (mirrors the legacy
    # test_root_requires_auth note).
    app = _app(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.get("/api/calls")
    assert r.status_code == 401


# --- ordering (unranked) ----------------------------------------------------


def test_lists_calls_newest_first(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _seed_open_calls(pg_session_factory)
    c, *_ = _login(app, pg_session_factory)
    with c:
        body = c.get("/api/calls").json()
    # Newest published_at first: Vin one (Apr 5) > EU one (Apr 1) > EU two (Mar 1).
    assert _titles(body) == ["Vin one", "EU one", "EU two"]


def test_sort_by_deadline(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _seed_open_calls(pg_session_factory)
    c, *_ = _login(app, pg_session_factory)
    with c:
        body = c.get("/api/calls?sort=deadline").json()
    # Soonest deadline first: Vin one (45d) < EU two (75d) < EU one (105d).
    assert _titles(body) == ["Vin one", "EU two", "EU one"]


def test_invalid_sort_is_422(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, *_ = _login(app, pg_session_factory)
    with c:
        r = c.get("/api/calls?sort=junk")
    assert r.status_code == 422


# --- source filter ----------------------------------------------------------


def test_filter_by_source(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _seed_open_calls(pg_session_factory)
    c, *_ = _login(app, pg_session_factory)
    with c:
        body = c.get("/api/calls?source=vinnova").json()
    assert _titles(body) == ["Vin one"]
    assert body["effective"]["source"] == "vinnova"


def test_unknown_source_degrades_to_all(monkeypatch, pg_url, pg_session_factory):
    """A stale/unknown ?source slug shows all calls (selection cleared), not a
    confusing empty feed — and effective.source is reported back as null."""
    app = _app(monkeypatch, pg_url)
    _seed_open_calls(pg_session_factory)
    c, *_ = _login(app, pg_session_factory)
    with c:
        body = c.get("/api/calls?source=does-not-exist").json()
    assert set(_titles(body)) == {"EU one", "Vin one", "EU two"}
    assert body["effective"]["source"] is None


# --- pagination + summary ---------------------------------------------------


def test_summary_counts_full_dataset_despite_paging(monkeypatch, pg_url, pg_session_factory):
    """With limit=1 only one item is returned, but the open_count summary and the
    pagination total must reflect all 3 seeded calls (computed over the whole
    filtered set, not the page), and a next page is offered."""
    app = _app(monkeypatch, pg_url)
    _seed_open_calls(pg_session_factory)
    c, *_ = _login(app, pg_session_factory)
    with c:
        body = c.get("/api/calls?limit=1").json()
    assert len(body["items"]) == 1
    assert body["summary"]["open_count"] == 3
    assert body["pagination"]["total"] == 3
    assert body["pagination"]["has_next"] is True
    assert body["pagination"]["has_prev"] is False


# --- profile resolution -----------------------------------------------------


def test_empty_profile_is_unfiltered(monkeypatch, pg_url, pg_session_factory):
    """`?profile=` (empty string, the <select> 'all' option) is treated as
    no-selection, not an int-parse 422; effective.profile_id is null."""
    app = _app(monkeypatch, pg_url)
    _seed_open_calls(pg_session_factory)
    c, *_ = _login(app, pg_session_factory)
    with c:
        r = c.get("/api/calls?profile=")
    assert r.status_code == 200
    assert r.json()["effective"]["profile_id"] is None


def test_bare_request_surfaces_stored_default(monkeypatch, pg_url, pg_session_factory):
    """A bare GET (no ?profile) honours the user's stored default and surfaces it
    as the resolved effective.profile_id — the SPA reads this to set the profile
    <select> (read-only; the GET never persists)."""
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _login(app, pg_session_factory, profile=True, set_default=True)
    with c:
        body = c.get("/api/calls").json()
    assert body["effective"]["profile_id"] == pid
    # Read-only: the stored default is untouched by the GET.
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id == pid


def test_unknown_profile_degrades_to_unranked(monkeypatch, pg_url, pg_session_factory):
    """A stale/bogus ?profile id falls back to the unranked view (no scores),
    effective.profile_id null — so a stale bookmark neither errors nor leaks."""
    app = _app(monkeypatch, pg_url)
    _seed_open_calls(pg_session_factory)
    c, *_ = _login(app, pg_session_factory)
    with c:
        body = c.get("/api/calls?profile=99999").json()
    assert body["effective"]["profile_id"] is None
    assert set(_titles(body)) == {"EU one", "Vin one", "EU two"}
    assert all(v is None for v in _scores(body).values())


def test_inactive_profile_degrades_to_unranked(monkeypatch, pg_url, pg_session_factory):
    """Spec: inactive profiles are removed from feeds. ?profile=<inactive> must
    NOT surface the stored ranking score for that profile (a soft-delete must
    actually remove audit data from the response)."""
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _login(app, pg_session_factory, profile=True, profile_active=False)
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        call = Call(
            source_id=src.id,
            external_id="I-1",
            url="https://x/i",
            title="Should be unranked",
            published_at=datetime(2026, 4, 1, tzinfo=UTC),
        )
        s.add(call)
        s.flush()
        s.add(
            CallRanking(
                call_id=call.id,
                profile_id=pid,
                aggregate_score=99,
                rationale="hidden",
                model="gemma4:e4b-q8",
                prompt_version="v2.0",
            )
        )
        s.commit()
    with c:
        body = c.get(f"/api/calls?profile={pid}").json()
    assert body["effective"]["profile_id"] is None
    assert _scores(body) == {"Should be unranked": None}  # score 99 must not leak


# --- ordering (ranked, profile selected) ------------------------------------


def _seed_scored(pg_session_factory, profile_id):
    """Three rolling (no-deadline → open) calls: High score (85), Low score (15),
    Unranked (no CallRanking row). Published Apr 1/2/3."""
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


def test_profile_orders_by_score_desc(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _login(app, pg_session_factory, profile=True)
    _seed_scored(pg_session_factory, pid)
    with c:
        body = c.get(f"/api/calls?profile={pid}").json()
    # Score desc, NULL last: High (85) > Low (15) > Unranked (null).
    assert _titles(body) == ["High score", "Low score", "Unranked"]
    assert _scores(body) == {"High score": 85, "Low score": 15, "Unranked": None}
    assert body["effective"]["profile_id"] == pid


def test_without_profile_uses_published_at(monkeypatch, pg_url, pg_session_factory):
    """Even when ranking rows exist, NO ?profile means the unranked published-at
    sort and no scores surface (Unranked Apr 3 > Low Apr 2 > High Apr 1)."""
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _login(app, pg_session_factory, profile=True)
    _seed_scored(pg_session_factory, pid)
    with c:
        body = c.get("/api/calls").json()
    assert _titles(body) == ["Unranked", "Low score", "High score"]
    assert all(v is None for v in _scores(body).values())


def test_ranked_ties_broken_by_cosine(monkeypatch, pg_url, pg_session_factory):
    """Two calls both scored 80 for the profile: the higher embed_similarity
    (cosine) sorts first."""
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _login(app, pg_session_factory, profile=True)
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c_high = Call(
            source_id=src.id,
            external_id="H",
            url="https://x/h",
            title="High Cosine",
            published_at=datetime(2026, 4, 1, tzinfo=UTC),
        )
        c_low = Call(
            source_id=src.id,
            external_id="L",
            url="https://x/l",
            title="Low Cosine",
            published_at=datetime(2026, 4, 1, tzinfo=UTC),
        )
        s.add_all([c_high, c_low])
        s.flush()
        for call, cos in ((c_high, 0.90), (c_low, 0.40)):
            s.add(
                CallRanking(
                    call_id=call.id,
                    profile_id=pid,
                    aggregate_score=80,
                    rationale="r",
                    model="m",
                    prompt_version="v4.1",
                    embed_similarity=cos,
                )
            )
        s.commit()
    with c:
        body = c.get(f"/api/calls?profile={pid}").json()
    assert _titles(body) == ["High Cosine", "Low Cosine"]


# --- closed-call handling ---------------------------------------------------


def test_default_feed_hides_closed(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _seed_open_calls(pg_session_factory)  # 3 open
    _add_closed_call(pg_session_factory)
    c, *_ = _login(app, pg_session_factory)
    with c:
        body = c.get("/api/calls").json()
    titles = _titles(body)
    assert "Closed call" not in titles
    assert "Vin one" in titles


def test_null_deadline_call_always_visible(monkeypatch, pg_url, pg_session_factory):
    """A call with no deadline (rolling) is OPEN and stays in the default feed;
    only the past-deadline call is hidden."""
    app = _app(monkeypatch, pg_url)
    _add_closed_call(pg_session_factory)
    with pg_session_factory() as s:
        eu = s.execute(select(Source).where(Source.slug == "eu_portal")).scalar_one()
        s.add(
            Call(
                source_id=eu.id,
                external_id="R-1",
                url="ur",
                title="Rolling call",
                published_at=datetime(2026, 4, 1, tzinfo=UTC),
                deadline=None,
            )
        )
        s.commit()
    c, *_ = _login(app, pg_session_factory)
    with c:
        titles = _titles(c.get("/api/calls").json())
    assert "Rolling call" in titles
    assert "Closed call" not in titles


def test_show_closed_reveals_closed(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _seed_open_calls(pg_session_factory)
    _add_closed_call(pg_session_factory)
    c, *_ = _login(app, pg_session_factory)
    with c:
        titles = _titles(c.get("/api/calls?show_closed=1").json())
    assert "Closed call" in titles
    assert "Vin one" in titles


def test_closed_sorts_last_under_deadline(monkeypatch, pg_url, pg_session_factory):
    """Even under sort=deadline (which would pull a past deadline to the top), a
    shown closed call sorts AFTER every open call."""
    app = _app(monkeypatch, pg_url)
    _seed_open_calls(pg_session_factory)
    _add_closed_call(pg_session_factory)
    c, *_ = _login(app, pg_session_factory)
    with c:
        titles = _titles(c.get("/api/calls?show_closed=1&sort=deadline").json())
    assert titles[-1] == "Closed call"


def test_closed_sorts_last_under_newest(monkeypatch, pg_url, pg_session_factory):
    """Under the default 'newest' sort, a recently-published closed call still
    sorts AFTER older open calls (guards the closed-last key in the unranked
    branch, not just the deadline sort)."""
    app = _app(monkeypatch, pg_url)
    _seed_open_calls(pg_session_factory)
    _add_closed_call(
        pg_session_factory, external_id="C-NEW", published_at=datetime(2026, 6, 1, tzinfo=UTC)
    )
    c, *_ = _login(app, pg_session_factory)
    with c:
        titles = _titles(c.get("/api/calls?show_closed=1").json())
    assert titles[-1] == "Closed call"


def test_profile_view_closed_never_outranks_open(monkeypatch, pg_url, pg_session_factory):
    """A high-scored CLOSED call is hidden by default and, when shown, sorts
    BELOW a lower-scored OPEN call (structural closed-last sort beats score)."""
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _login(app, pg_session_factory, profile=True)
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
                profile_id=pid,
                aggregate_score=95,
                rationale="x",
                model="gemma4:e4b-q8",
                prompt_version="v2.0",
            )
        )
        s.add(
            CallRanking(
                call_id=openc.id,
                profile_id=pid,
                aggregate_score=20,
                rationale="x",
                model="gemma4:e4b-q8",
                prompt_version="v2.0",
            )
        )
        s.commit()
    with c:
        default = c.get(f"/api/calls?profile={pid}").json()
        shown = c.get(f"/api/calls?profile={pid}&show_closed=1").json()
    assert "Closed high" not in _titles(default)  # hidden despite score 95
    assert "Open low" in _titles(default)
    assert _titles(shown) == ["Open low", "Closed high"]  # open low outranks closed 95


# --- summary badges ---------------------------------------------------------


def test_open_count_excludes_closed_regardless_of_toggle(monkeypatch, pg_url, pg_session_factory):
    """summary.open_count counts only open calls (3), whether or not the closed
    call is being shown — the toggle never inflates the open badge to 4."""
    app = _app(monkeypatch, pg_url)
    _seed_open_calls(pg_session_factory)  # 3 open
    _add_closed_call(pg_session_factory)  # 1 closed
    c, *_ = _login(app, pg_session_factory)
    with c:
        default = c.get("/api/calls").json()
        shown = c.get("/api/calls?show_closed=1").json()
    assert default["summary"]["open_count"] == 3
    assert shown["summary"]["open_count"] == 3
    # And the visible/total tracks the toggle (3 vs 4) — never the open badge.
    assert default["pagination"]["total"] == 3
    assert shown["pagination"]["total"] == 4


def test_soon_deadline_badge_counts_open_within_30d_only(monkeypatch, pg_url, pg_session_factory):
    """summary.soon_deadline counts OPEN calls whose deadline is within 30 days
    and EXCLUDES both >30d-out calls and past-deadline (closed) ones — and, like
    open_count, is independent of the show_closed toggle."""
    app = _app(monkeypatch, pg_url)
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        eu = s.execute(select(Source).where(Source.slug == "eu_portal")).scalar_one()
        s.add_all(
            [
                Call(
                    source_id=eu.id,
                    external_id="SOON",
                    url="us",
                    title="Soon deadline",
                    published_at=datetime(2026, 4, 1, tzinfo=UTC),
                    deadline=now + timedelta(days=10),  # within 30d → counts
                ),
                Call(
                    source_id=eu.id,
                    external_id="FAR",
                    url="uf",
                    title="Far deadline",
                    published_at=datetime(2026, 4, 1, tzinfo=UTC),
                    deadline=now + timedelta(days=60),  # >30d → excluded
                ),
                Call(
                    source_id=eu.id,
                    external_id="PAST",
                    url="up",
                    title="Past deadline",
                    published_at=datetime(2026, 4, 1, tzinfo=UTC),
                    deadline=now - timedelta(days=3),  # closed → excluded
                ),
            ]
        )
        s.commit()
    c, *_ = _login(app, pg_session_factory)
    with c:
        default = c.get("/api/calls").json()
        shown = c.get("/api/calls?show_closed=1").json()
    assert default["summary"]["soon_deadline"] == 1
    assert shown["summary"]["soon_deadline"] == 1


def test_pagination_total_tracks_toggle(monkeypatch, pg_url, pg_session_factory):
    """3 open + 1 closed, limit=3: default total=3 (no next page); show_closed=1
    total=4 (next page offered). Guards against `total` counting closed rows
    while the list hides them — which would link to an empty next page."""
    app = _app(monkeypatch, pg_url)
    _seed_open_calls(pg_session_factory)  # 3 open
    _add_closed_call(pg_session_factory)  # 1 closed
    c, *_ = _login(app, pg_session_factory)
    with c:
        default = c.get("/api/calls?limit=3").json()
        shown = c.get("/api/calls?show_closed=1&limit=3").json()
    assert default["pagination"]["total"] == 3
    assert default["pagination"]["has_next"] is False
    assert shown["pagination"]["total"] == 4
    assert shown["pagination"]["has_next"] is True


def test_high_score_badge_excludes_closed(monkeypatch, pg_url, pg_session_factory):
    """A closed call scoring >=70 is NOT counted as a top match. high_score must
    be 1 (the open 75), not 2, in BOTH toggle states — an impl that counted over
    the toggled list_filters would wrongly count the closed 95 when show_closed=1."""
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _login(app, pg_session_factory, profile=True)
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
                profile_id=pid,
                aggregate_score=95,
                rationale="x",
                model="gemma4:e4b-q8",
                prompt_version="v2.0",
            )
        )
        s.add(
            CallRanking(
                call_id=openc.id,
                profile_id=pid,
                aggregate_score=75,
                rationale="x",
                model="gemma4:e4b-q8",
                prompt_version="v2.0",
            )
        )
        s.commit()
    with c:
        default = c.get(f"/api/calls?profile={pid}").json()
        shown = c.get(f"/api/calls?profile={pid}&show_closed=1").json()
    assert default["summary"]["high_score"] == 1
    assert shown["summary"]["high_score"] == 1


# --- dismissed calls --------------------------------------------------------


def test_dismissed_calls_omitted(monkeypatch, pg_url, pg_session_factory):
    """A call the user hid (POST /calls/{id}/hide → UserCallDismissal) is omitted
    from that user's /api/calls feed. The hide POST itself (redirect/idempotency/
    404/auth) stays covered in test_web_calls_hide.py."""
    app = _app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        visible = Call(
            source_id=src.id,
            external_id="VIS",
            url="https://x/v",
            title="VisibleCall",
            published_at=datetime(2026, 4, 1, tzinfo=UTC),
        )
        hidden = Call(
            source_id=src.id,
            external_id="HID",
            url="https://x/h",
            title="HiddenCall",
            published_at=datetime(2026, 4, 2, tzinfo=UTC),
        )
        s.add_all([visible, hidden])
        s.flush()
        hidden_id = hidden.id
        s.commit()
    c, uid, _ = _login(app, pg_session_factory)
    with c:
        c.post(f"/calls/{hidden_id}/hide", follow_redirects=False)
        body = c.get("/api/calls").json()
    titles = _titles(body)
    assert "VisibleCall" in titles
    assert "HiddenCall" not in titles
    # The dismissal row was actually written for this user.
    with pg_session_factory() as s:
        assert (
            s.execute(
                select(UserCallDismissal).where(
                    UserCallDismissal.user_id == uid, UserCallDismissal.call_id == hidden_id
                )
            ).scalar_one_or_none()
            is not None
        )
