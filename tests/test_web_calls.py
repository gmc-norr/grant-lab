from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Call, Source, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _logged_in(app, pg_session_factory, email="alice@ki.se"):
    with pg_session_factory() as s:
        u = User(email=email, status="active")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def _seed_calls(pg_session_factory):
    # Deadlines are relative to "now" so the suite is robust to wall-clock and
    # all three calls stay OPEN — the closed-call filter would otherwise hide
    # fixed past-2026 dates. Relative order preserved (and all > 30 days out, so
    # none land in the "deadline within 30 d" badge): Vin (soonest) < EU two < EU one.
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        eu = s.execute(select(Source).where(Source.slug == "eu_portal")).scalar_one()
        vn = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add_all(
            [
                # EU one: published April 1, latest deadline.
                Call(
                    source_id=eu.id,
                    external_id="E-1",
                    url="u1",
                    title="EU one",
                    published_at=datetime(2026, 4, 1, tzinfo=UTC),
                    deadline=now + timedelta(days=105),
                ),
                # Vin one: published April 5 (newest), soonest deadline.
                Call(
                    source_id=vn.id,
                    external_id="V-1",
                    url="u2",
                    title="Vin one",
                    published_at=datetime(2026, 4, 5, tzinfo=UTC),
                    deadline=now + timedelta(days=45),
                ),
                # EU two: published March 1 (oldest), middle deadline.
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


def _show_closed_input(html: str) -> str:
    """Return just the show_closed <input ...> tag, for state assertions."""
    start = html.index('name="show_closed"')
    open_lt = html.rfind("<", 0, start)
    close_gt = html.index(">", start)
    return html[open_lt : close_gt + 1]


def _add_closed_call(
    pg_session_factory, *, title="Closed call", external_id="C-1", published_at=None
):
    """Insert one call whose deadline is in the past (closed). `published_at`
    defaults to a date older than the open seed calls; pass a newer one to
    test that closed calls still sort last under the default 'newest' sort."""
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


def test_root_requires_auth(monkeypatch, pg_url, pg_session_factory):
    # pg_session_factory pulls in migrated_engine, so the shared testcontainer
    # is migrated before the app's startup scheduler runs — otherwise this
    # (first, pg_url-only) test crashes on the not-yet-created references table.
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 401


def test_root_lists_calls_newest_first(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/")
    assert r.status_code == 200
    body = r.text
    # Newest published_at first
    assert body.index("Vin one") < body.index("EU one") < body.index("EU two")


def test_root_filter_by_source(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?source=vinnova")
    assert r.status_code == 200
    assert "Vin one" in r.text
    assert "EU one" not in r.text


def test_root_unknown_source_degrades_to_all(monkeypatch, pg_url, pg_session_factory):
    """A stale/unknown ?source= slug shows all calls (selection cleared),
    not a confusing empty feed."""
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?source=does-not-exist")
    assert r.status_code == 200
    assert "Vin one" in r.text and "EU one" in r.text


def test_root_summary_counts_full_dataset_despite_paging(monkeypatch, pg_url, pg_session_factory):
    """With limit=1 only one row renders, but the 'Open calls' badge must
    reflect all 3 seeded calls (summary is computed over the whole dataset,
    not the visible page), and a Next link is offered."""
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?limit=1")
    assert r.status_code == 200
    # Only one call row on the page...
    assert r.text.count('class="call-row"') == 1
    # ...but the summary badge reports the full total of 3.
    assert ">3</div>" in r.text
    # ...and pagination offers a next page.
    assert "page=2" in r.text


def test_root_sort_by_deadline(monkeypatch, pg_url, pg_session_factory):
    """sort=deadline orders soonest-deadline first (irrespective of
    publication date). Vin one → EU two → EU one (relative future deadlines)."""
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?sort=deadline")
    assert r.status_code == 200
    body = r.text
    assert body.index("Vin one") < body.index("EU two") < body.index("EU one")


def test_root_treats_empty_profile_as_unfiltered(monkeypatch, pg_url, pg_session_factory):
    """Browsers submit `?profile=` (empty string) when the profile <select>
    is set to its 'all' option. The route must treat that the same as the
    param being absent — not 422 with int-parsing error."""
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?profile=")
    assert r.status_code == 200


def test_call_detail_treats_empty_profile_as_unfiltered(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with pg_session_factory() as s:
        cid = s.execute(select(Call.id).where(Call.external_id == "V-1")).scalar_one()
    with client:
        r = client.get(f"/calls/{cid}?profile=")
    assert r.status_code == 200


def test_root_rejects_invalid_sort(monkeypatch, pg_url, pg_session_factory):
    """FastAPI Literal type rejects unknown sort values with 422."""
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?sort=junk")
    assert r.status_code == 422


def test_call_detail_shows_metadata(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with pg_session_factory() as s:
        cid = s.execute(select(Call.id).where(Call.external_id == "V-1")).scalar_one()
    with client:
        r = client.get(f"/calls/{cid}")
    assert r.status_code == 200
    assert "Vin one" in r.text


def test_call_detail_404_for_unknown_id(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/calls/99999")
    assert r.status_code == 404


def test_default_feed_hides_closed_calls(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)  # 3 open
    _add_closed_call(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/")
    assert r.status_code == 200
    assert "Closed call" not in r.text
    assert "Vin one" in r.text  # open calls still shown


def test_null_deadline_call_always_visible(monkeypatch, pg_url, pg_session_factory):
    """A call with no deadline (rolling, e.g. Vinnova) is OPEN and must appear
    in the default feed; only the past-deadline call is hidden."""
    app = _setup(monkeypatch, pg_url)
    _add_closed_call(pg_session_factory)  # 1 closed (past deadline)
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
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/")
    assert "Rolling call" in r.text
    assert "Closed call" not in r.text


def test_show_closed_reveals_closed_calls(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    _add_closed_call(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?show_closed=1")
    assert r.status_code == 200
    assert "Closed call" in r.text
    assert "Vin one" in r.text


def test_closed_calls_sort_last_unranked(monkeypatch, pg_url, pg_session_factory):
    """Even with sort=deadline (which would pull a past deadline to the top),
    a shown closed call must appear AFTER every open call."""
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    _add_closed_call(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?show_closed=1&sort=deadline")
    body = r.text
    assert body.index("Closed call") > body.index("Vin one")
    assert body.index("Closed call") > body.index("EU two")
    assert body.index("Closed call") > body.index("EU one")


def test_closed_calls_sort_last_under_newest(monkeypatch, pg_url, pg_session_factory):
    """Under the DEFAULT 'newest' sort, a recently-published closed call must
    still sort AFTER older open calls (guards the closed-last key in the
    unranked branch, not just the deadline sort)."""
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)  # open calls published Mar–Apr 2026
    # Closed but published more recently than any open call → would sort FIRST
    # under newest-desc without the closed-last key.
    _add_closed_call(
        pg_session_factory, external_id="C-NEW", published_at=datetime(2026, 6, 1, tzinfo=UTC)
    )
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?show_closed=1")  # default sort=newest
    body = r.text
    assert body.index("Closed call") > body.index("Vin one")
    assert body.index("Closed call") > body.index("EU one")
    assert body.index("Closed call") > body.index("EU two")


def test_pagination_total_tracks_toggle(monkeypatch, pg_url, pg_session_factory):
    """With 3 open + 1 closed and limit=3, the default view's pagination total
    must count only the 3 open rows (no Next page); show_closed=1 counts all 4
    and offers page 2. Guards against `total` counting closed rows while the
    list hides them — which would link to an empty Next page."""
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)  # 3 open
    _add_closed_call(pg_session_factory)  # 1 closed
    client = _logged_in(app, pg_session_factory)
    with client:
        default = client.get("/?limit=3")
        shown = client.get("/?show_closed=1&limit=3")
    assert "page=2" not in default.text  # 3 open rows, total=3, no Next
    assert "page=2" in shown.text  # 4 rows, total=4, Next offered


def test_open_calls_badge_excludes_closed_regardless_of_toggle(
    monkeypatch, pg_url, pg_session_factory
):
    """The 'Open calls' badge counts only open calls (3), whether or not the
    closed call is being shown."""
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)  # 3 open
    _add_closed_call(pg_session_factory)  # 1 closed
    client = _logged_in(app, pg_session_factory)
    with client:
        default = client.get("/")
        shown = client.get("/?show_closed=1")
    # Open count is 3 in both; the 4-row total is never shown as a badge.
    assert ">3</div>" in default.text
    assert ">3</div>" in shown.text
    assert ">4</div>" not in shown.text


def test_closed_deadline_renders_closed_label(monkeypatch, pg_url, pg_session_factory):
    """A shown closed call's deadline reads grey 'Closed', not red 'deadline-soon'."""
    app = _setup(monkeypatch, pg_url)
    _add_closed_call(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?show_closed=1")
    body = r.text
    assert "deadline-closed" in body
    assert "Closed ·" in body  # the rendered label prefix, not the call title
    # The closed row must NOT be styled as the urgent (red) deadline.
    assert "deadline-soon" not in body


def test_show_closed_toggle_checkbox_present_and_reflects_state(
    monkeypatch, pg_url, pg_session_factory
):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        off = client.get("/")
        on = client.get("/?show_closed=1")
    assert 'name="show_closed"' in off.text
    assert "checked" not in _show_closed_input(off.text)
    assert "checked" in _show_closed_input(on.text)


def test_pagination_preserves_show_closed(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)  # 3 open
    _add_closed_call(pg_session_factory)  # 1 closed → 4 rows when shown
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?show_closed=1&limit=1")
    assert "show_closed=1" in r.text  # Next link carries the toggle
    assert "page=2" in r.text


def test_app_js_autosubmit_selector_is_generalized():
    """The auto-submit handler must target [data-autosubmit] (both the filter
    selects AND the show-closed checkbox), not select[data-autosubmit] only —
    otherwise the toggle silently won't submit. Static guard: there is no JS
    runtime in the pytest suite, so this asserts the source was generalized."""
    from pathlib import Path

    import call_manager

    js = (Path(call_manager.__file__).parent / "web" / "static" / "app.js").read_text()
    assert "[data-autosubmit]" in js
    assert "select[data-autosubmit]" not in js
