"""The one unauthenticated route: `GET /api/public/boards/{slug}` (Plan C Task 7).

FIXTURES. `pg_session_factory` (tests/conftest.py:151) is a FACTORY --
`with pg_session_factory() as s:`. `profile_factory` (:184) returns a
`(profile_id, user_id)` tuple. There is no `client_unauth` fixture -- the
unauthenticated client here is a plain `TestClient(app)` with NO cookie set at
all, never a copy of an authenticated helper with the cookie line removed (a
leftover cookie would prove nothing about userlessness).

D1: this endpoint's query is genuinely userless -- no `CallDecision`, no
`CallRanking`, no `user_id`. `test_the_OWNERS_hides_do_NOT_suppress_a_card`
and `test_an_ADMINS_hides_do_NOT_suppress_a_card` are the direct inverse of
`tests/test_boards_queue.py`'s hide tests, pinning that asymmetry from this
side.

Every 404 test pairs with a positive control (a published board that returns
200 in the SAME test) -- a missing/unregistered route also answers 404, so a
status-only assertion would be green before the endpoint exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from grant_lab.db.models import Board, BoardItem, Call, CallDecision, Source, User
from grant_lab.main import create_app
from grant_lab.web.filters import deadline_local_date
from tests.qr_path_test_helpers import matrix_from_qr_path as _matrix_from_path

FUTURE = datetime.now(UTC) + timedelta(days=30)
PAST = datetime.now(UTC) - timedelta(days=1)

_ITEM_KEYS = {
    "title",
    "source_name",
    "deadline_local_date",
    "budget_min",
    "budget_max",
    "currency",
    "url",
    "qr_modules",
    "qr_path",
}
_TOP_KEYS = {
    "title",
    "generated_at",
    "orientation",
    "items_per_page",
    "max_pages",
    "page_seconds",
    "items",
}


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _new_user(pg_session_factory, email, *, is_admin=False):
    with pg_session_factory() as s:
        u = User(status="active", email=email, is_admin=is_admin)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _make_board(
    pg_session_factory,
    *,
    profile_id,
    created_by,
    slug,
    is_published=True,
    orientation="landscape",
    items_per_page=8,
    max_pages=3,
    page_seconds=30,
):
    with pg_session_factory() as s:
        b = Board(
            slug=slug,
            title="Corridor board",
            profile_id=profile_id,
            created_by=created_by,
            is_published=is_published,
            orientation=orientation,
            items_per_page=items_per_page,
            max_pages=max_pages,
            page_seconds=page_seconds,
        )
        s.add(b)
        s.commit()
        return b.id


def _make_call(
    pg_session_factory,
    *,
    external_id,
    deadline=FUTURE,
    title=None,
    url=None,
    budget_min=None,
    budget_max=None,
    currency=None,
    source_slug="vinnova",
):
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == source_slug)).scalar_one()
        c = Call(
            source_id=src.id,
            external_id=external_id,
            url=url or f"https://funder.example/{external_id}",
            title=title or f"Call {external_id}",
            deadline=deadline,
            budget_min=budget_min,
            budget_max=budget_max,
            currency=currency,
        )
        s.add(c)
        s.commit()
        return c.id


def _approve(pg_session_factory, *, board_id, call_id, approved_by, position=0):
    with pg_session_factory() as s:
        s.add(
            BoardItem(
                board_id=board_id, call_id=call_id, position=position, approved_by=approved_by
            )
        )
        s.commit()


def _decide(pg_session_factory, *, user_id, profile_id, call_id, state):
    with pg_session_factory() as s:
        s.add(CallDecision(user_id=user_id, profile_id=profile_id, call_id=call_id, state=state))
        s.commit()


def _set_title(pg_session_factory, call_id, title):
    with pg_session_factory() as s:
        c = s.get(Call, call_id)
        c.title = title
        s.commit()


def test_a_published_board_returns_its_cards(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Publishing profile")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="a" * 16)
    call_id = _make_call(
        pg_session_factory,
        external_id="card-1",
        title="Cancer immunotherapy call",
        budget_min=100_000,
        budget_max=500_000,
        currency="SEK",
    )
    _approve(pg_session_factory, board_id=board_id, call_id=call_id, approved_by=owner_id)

    with TestClient(app) as c:
        r = c.get(f"/api/public/boards/{'a' * 16}")

    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == _TOP_KEYS
    assert body["title"] == "Corridor board"
    assert body["orientation"] == "landscape"
    assert body["items_per_page"] == 8
    assert body["max_pages"] == 3
    assert body["page_seconds"] == 30
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert set(item.keys()) == _ITEM_KEYS
    assert item["title"] == "Cancer immunotherapy call"
    assert item["source_name"] == "Vinnova"
    assert item["budget_min"] == 100_000
    assert item["budget_max"] == 500_000
    assert item["currency"] == "SEK"
    assert item["url"] == "https://funder.example/card-1"
    assert item["qr_modules"] > 0
    assert item["qr_path"]


def test_an_unpublished_board_is_404(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Unpublished profile")
    _make_board(
        pg_session_factory, profile_id=pid, created_by=owner_id, slug="b" * 16, is_published=False
    )
    # Positive control, in the SAME test: a SIBLING board that IS published,
    # so a mutant that 404s every slug (or an unregistered route) cannot pass
    # this test by accident.
    control_slug = "b" + "1" * 15
    _make_board(
        pg_session_factory,
        profile_id=pid,
        created_by=owner_id,
        slug=control_slug,
        is_published=True,
    )

    with TestClient(app) as c:
        r = c.get(f"/api/public/boards/{'b' * 16}")
        control = c.get(f"/api/public/boards/{control_slug}")

    assert r.status_code == 404, r.text
    assert r.json() == {"error": {"code": "not_found", "message": "board not found"}}
    assert control.status_code == 200, control.text


def test_a_nonexistent_slug_is_the_SAME_404(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Compare-404 profile")
    _make_board(
        pg_session_factory, profile_id=pid, created_by=owner_id, slug="c" * 16, is_published=False
    )
    # Positive control, in the SAME test.
    control_slug = "c" + "2" * 15
    _make_board(
        pg_session_factory,
        profile_id=pid,
        created_by=owner_id,
        slug=control_slug,
        is_published=True,
    )

    with TestClient(app) as c:
        unpublished = c.get(f"/api/public/boards/{'c' * 16}")
        nonexistent = c.get(f"/api/public/boards/{'z' * 16}")
        control = c.get(f"/api/public/boards/{control_slug}")

    assert unpublished.status_code == 404
    assert nonexistent.status_code == 404
    assert control.status_code == 200, control.text
    # BYTE-EQUAL bodies: two differently-worded 404s would let a client tell
    # "not published" apart from "doesn't exist" and make the slug field
    # BoardOut already exposes (GET /api/boards) into a public oracle.
    assert unpublished.content == nonexistent.content
    assert unpublished.json() == {"error": {"code": "not_found", "message": "board not found"}}


def test_no_authentication_is_required(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="No-auth profile")
    _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="d" * 16)

    # A BARE client: no cookie, no header, never a copy of an authenticated
    # helper with the cookie line stripped out.
    client = TestClient(app)
    assert client.cookies.get("session") is None
    with client:
        r = client.get(f"/api/public/boards/{'d' * 16}")

    assert r.status_code == 200, r.text


def test_a_past_deadline_call_is_absent(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Past deadline profile")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="e" * 16)
    call_id = _make_call(pg_session_factory, external_id="expired", deadline=PAST)
    _approve(pg_session_factory, board_id=board_id, call_id=call_id, approved_by=owner_id)

    with TestClient(app) as c:
        r = c.get(f"/api/public/boards/{'e' * 16}")

    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


def test_the_payload_carries_no_score_or_rationale(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="No score profile")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="f" * 16)
    # NOT "score-check": the call's title is "Call {external_id}" and the
    # forbidden-substring check below would then match its OWN fixture data,
    # not a leak from the endpoint.
    call_id = _make_call(pg_session_factory, external_id="closed-list-check")
    _approve(pg_session_factory, board_id=board_id, call_id=call_id, approved_by=owner_id)

    with TestClient(app) as c:
        r = c.get(f"/api/public/boards/{'f' * 16}")

    body = r.json()
    assert set(body["items"][0].keys()) == _ITEM_KEYS  # closed list: no room for score/rationale
    for forbidden in (
        "score",
        "rationale",
        "aggregate_score",
        "criterion",
        "quoted_evidence",
        "applicant_notes",
        "min_score",
    ):
        assert forbidden not in r.text, f"{forbidden!r} leaked into the public payload"


def test_the_payload_carries_no_profile_identity(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Oncology Research Group Seven")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="g" * 16)
    call_id = _make_call(pg_session_factory, external_id="identity-check")
    _approve(pg_session_factory, board_id=board_id, call_id=call_id, approved_by=owner_id)

    with TestClient(app) as c:
        r = c.get(f"/api/public/boards/{'g' * 16}")

    # IMPORTANT 3: without a real 200 body to inspect, the substring-absence
    # checks below pass just as well on a 404/429/500/empty-items response --
    # this endpoint's own RED run (a missing route) proved exactly that
    # (this test was the ONE pass out of "15 failed, 1 passed"). Anchor the
    # assertions to an actual card first.
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 1

    assert "Oncology Research Group Seven" not in r.text
    assert "profile_id" not in r.text
    assert "profile" not in r.text.lower()


def test_the_payload_carries_no_call_id(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="No call id profile")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="h" * 16)
    call_id = _make_call(pg_session_factory, external_id="call-id-check")
    _approve(pg_session_factory, board_id=board_id, call_id=call_id, approved_by=owner_id)

    with TestClient(app) as c:
        r = c.get(f"/api/public/boards/{'h' * 16}")

    body = r.json()
    assert "call_id" not in r.text
    assert set(body["items"][0].keys()) == _ITEM_KEYS  # no "id" field of any kind


def test_items_come_back_in_position_order(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Position order profile")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="i" * 16)
    call_a = _make_call(pg_session_factory, external_id="pos-a", title="Third")
    call_b = _make_call(pg_session_factory, external_id="pos-b", title="First")
    call_c = _make_call(pg_session_factory, external_id="pos-c", title="Second")
    # Approve OUT of position order, so a query that (wrongly) orders by
    # approval time or call id would still pass a naively-ordered test.
    _approve(
        pg_session_factory, board_id=board_id, call_id=call_a, approved_by=owner_id, position=2
    )
    _approve(
        pg_session_factory, board_id=board_id, call_id=call_b, approved_by=owner_id, position=0
    )
    _approve(
        pg_session_factory, board_id=board_id, call_id=call_c, approved_by=owner_id, position=1
    )

    with TestClient(app) as c:
        r = c.get(f"/api/public/boards/{'i' * 16}")

    titles = [item["title"] for item in r.json()["items"]]
    assert titles == ["First", "Second", "Third"]


def test_the_OWNERS_hides_do_NOT_suppress_a_card(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """D1's asymmetry from the public side: `board_queue` (Task 4) DOES
    suppress a call the profile owner hid; this endpoint must NOT -- a board
    is the institution's published list, not any individual's inbox."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Owner hides profile")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="j" * 16)
    call_id = _make_call(pg_session_factory, external_id="owner-hidden")
    _approve(pg_session_factory, board_id=board_id, call_id=call_id, approved_by=owner_id)
    _decide(pg_session_factory, user_id=owner_id, profile_id=pid, call_id=call_id, state="hidden")

    with TestClient(app) as c:
        r = c.get(f"/api/public/boards/{'j' * 16}")

    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 1


def test_an_ADMINS_hides_do_NOT_suppress_a_card(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Admin hides profile")
    admin_id = _new_user(pg_session_factory, "admin-hides@umu.se", is_admin=True)
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=admin_id, slug="k" * 16)
    call_id = _make_call(pg_session_factory, external_id="admin-hidden")
    _approve(pg_session_factory, board_id=board_id, call_id=call_id, approved_by=admin_id)
    _decide(pg_session_factory, user_id=admin_id, profile_id=pid, call_id=call_id, state="hidden")

    with TestClient(app) as c:
        r = c.get(f"/api/public/boards/{'k' * 16}")

    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 1


def test_a_call_edited_after_approval_shows_the_NEW_title(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """D8: the board shows LIVE call data, not a snapshot taken at approval."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Live data profile")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="l" * 16)
    call_id = _make_call(pg_session_factory, external_id="edited-after", title="Old title")
    _approve(pg_session_factory, board_id=board_id, call_id=call_id, approved_by=owner_id)
    _set_title(pg_session_factory, call_id, "New title")

    with TestClient(app) as c:
        r = c.get(f"/api/public/boards/{'l' * 16}")

    assert r.json()["items"][0]["title"] == "New title"


def test_deadline_local_date_matches_web_filters(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Deadline conversion profile")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="m" * 16)
    # 23:30 UTC on New Year's Eve is 00:30 the NEXT day in Stockholm (UTC+1 in
    # winter) -- chosen so a naive UTC .date() call would disagree with the
    # shared helper, catching a hand-rolled second conversion.
    deadline = datetime(2026, 12, 31, 23, 30, tzinfo=UTC)
    call_id = _make_call(pg_session_factory, external_id="tz-check", deadline=deadline)
    _approve(pg_session_factory, board_id=board_id, call_id=call_id, approved_by=owner_id)

    with TestClient(app) as c:
        r = c.get(f"/api/public/boards/{'m' * 16}")

    got = r.json()["items"][0]["deadline_local_date"]
    assert got == deadline_local_date(deadline)
    assert got == "2027-01-01"


def test_every_item_carries_a_qr_path(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="QR path profile")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="n" * 16)
    for i in range(3):
        call_id = _make_call(pg_session_factory, external_id=f"qr-{i}")
        _approve(
            pg_session_factory, board_id=board_id, call_id=call_id, approved_by=owner_id, position=i
        )

    with TestClient(app) as c:
        r = c.get(f"/api/public/boards/{'n' * 16}")

    items = r.json()["items"]
    assert len(items) == 3
    for item in items:
        assert item["qr_modules"] > 0
        assert item["qr_path"]


def test_the_qr_points_at_the_funder_url_not_this_app(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """§6: 'A QR that dead-ends on a login screen is worse than no QR.'

    Rebuilds the matrix from the emitted `qr_path` and compares it to a FRESH
    `segno.make_qr(call.url, error="m").matrix` -- a re-encode, not a decode
    (segno has no decoder; see `tests/qr_path_test_helpers.py`'s
    `matrix_from_qr_path` docstring, MINOR 6 -- shared with
    `tests/test_boards_qr.py` so the two copies cannot diverge again).
    """
    import segno

    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="QR target profile")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="o" * 16)
    call_id = _make_call(
        pg_session_factory, external_id="qr-target", url="https://real-funder.example/apply"
    )
    _approve(pg_session_factory, board_id=board_id, call_id=call_id, approved_by=owner_id)

    with TestClient(app) as c:
        r = c.get(f"/api/public/boards/{'o' * 16}")

    item = r.json()["items"][0]
    expected = [
        list(row) for row in segno.make_qr("https://real-funder.example/apply", error="m").matrix
    ]
    assert _matrix_from_path(item["qr_path"], item["qr_modules"]) == expected


def test_an_over_long_url_costs_one_qr_not_the_whole_board(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """IMPORTANT 1: `Call.url` is `Text NOT NULL` with no length bound
    (db/models/calls.py) and is populated by external ingestion adapters --
    a funder URL over segno's error-M budget (measured: 2331 characters
    fits, 2332 overflows, see tests/test_boards_qr.py) must not 500 the
    entire corridor board. `qr_for` swallows `segno.DataOverflowError` and
    returns the sentinel (qr_modules=0, qr_path=""); this pins that the
    OTHER card on the same board is unaffected.
    """
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Overlong url profile")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="r" * 16)
    ok_call = _make_call(pg_session_factory, external_id="overlong-ok", title="Fine card")
    bad_call = _make_call(
        pg_session_factory,
        external_id="overlong-bad",
        title="Overlong card",
        url="https://funder.example/" + "x" * 3000,
    )
    _approve(
        pg_session_factory, board_id=board_id, call_id=ok_call, approved_by=owner_id, position=0
    )
    _approve(
        pg_session_factory, board_id=board_id, call_id=bad_call, approved_by=owner_id, position=1
    )

    with TestClient(app) as c:
        r = c.get(f"/api/public/boards/{'r' * 16}")

    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 2
    assert items[0]["title"] == "Fine card"
    assert items[0]["qr_modules"] > 0
    assert items[0]["qr_path"]
    assert items[1]["title"] == "Overlong card"
    assert items[1]["qr_modules"] == 0
    assert items[1]["qr_path"] == ""


def test_the_full_board_payload_size_stays_under_budget(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """MINOR 4: this is a PER-ITEM ENCODING COST regression detector, not a
    bound on what the endpoint can actually emit. 24 items is the brief's
    fixture count, not a cap -- `approve_items` allows up to
    `items_per_page * max_pages` (`web/api/boards.py`), and `items_per_page`
    alone accepts up to 64 (`Field(..., le=64)`) with `max_pages` defaulting
    to 3, so a legitimately configured board holds up to 192 items. At this
    fixture's own 129-character URL that is ~1.75 MB (measured in
    `test_the_full_board_payload_at_the_true_capacity_ceiling` below), 3.4x
    the 512 KB this test asserts. This test's 512 KB budget is still a
    useful regression detector for the per-item cost `qr_for` produces --
    it would catch a change that doubles that cost -- it just does not bound
    the payload a real 192-item board can produce; see the ceiling test for
    that.

    The fixture URL: base = "https://www.vinnova.se/en/calls-for-proposals/"
    is 46 characters; `+ "a" * 83` makes it 129 -- asserted below so the
    fixture cannot silently drift. Re-measured 2026-09-05 with THIS module's
    `qr_for`: 129 chars -> 49 modules -> 9100 bytes of path per item, so
    218,400 bytes of path data for 24 items, and 226,040 bytes for the whole
    serialized body. 512 KB is 2.3x that -- loose enough not to fail on a
    reasonable change, tight enough to catch one that doubles the cost.
    """
    base = "https://www.vinnova.se/en/calls-for-proposals/"
    long_url = base + "a" * 83
    assert len(long_url) == 129

    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Payload size profile")
    board_id = _make_board(
        pg_session_factory,
        profile_id=pid,
        created_by=owner_id,
        slug="q" * 16,
        orientation="landscape",
        items_per_page=8,
        max_pages=3,
    )
    for i in range(24):
        call_id = _make_call(
            pg_session_factory,
            external_id=f"payload-size-{i}",
            title=f"Realistic funding call title number {i}",
            url=long_url,
        )
        _approve(
            pg_session_factory, board_id=board_id, call_id=call_id, approved_by=owner_id, position=i
        )

    with TestClient(app) as c:
        r = c.get(f"/api/public/boards/{'q' * 16}")

    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 24
    size = len(r.content)
    print(f"full 24-item board payload: {size} bytes")
    assert size < 512 * 1024, f"payload grew to {size} bytes -- over the 512 KB budget"


def test_the_full_board_payload_at_the_true_capacity_ceiling(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """MINOR 4: the OTHER test above is a per-item cost regression detector,
    not a bound on what the endpoint can actually be asked to emit. The real
    ceiling is `items_per_page * max_pages` (`web/api/boards.py`'s
    `approve_items`): `items_per_page` accepts up to 64
    (`Field(..., le=64)`) and `max_pages` defaults to 3, so 192 is a
    legitimately configured board's maximum, not a hypothetical. This
    exercises that real ceiling with the same 129-character fixture URL and
    records the real byte count -- deliberately with NO tight budget, since
    3.4x the other test's own number is not "a doubling", it is the
    documented cap; a future change that doubles THIS number is still worth
    someone's attention, so the bound here is loose (10 MB) rather than
    absent.
    """
    base = "https://www.vinnova.se/en/calls-for-proposals/"
    long_url = base + "a" * 83
    assert len(long_url) == 129

    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Ceiling payload profile")
    board_id = _make_board(
        pg_session_factory,
        profile_id=pid,
        created_by=owner_id,
        slug="s" * 16,
        orientation="landscape",
        items_per_page=64,
        max_pages=3,
    )
    ceiling = 64 * 3
    assert ceiling == 192
    for i in range(ceiling):
        call_id = _make_call(
            pg_session_factory,
            external_id=f"ceiling-{i}",
            title=f"Realistic funding call title number {i}",
            url=long_url,
        )
        _approve(
            pg_session_factory, board_id=board_id, call_id=call_id, approved_by=owner_id, position=i
        )

    with TestClient(app) as c:
        r = c.get(f"/api/public/boards/{'s' * 16}")

    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == ceiling
    size = len(r.content)
    print(f"full {ceiling}-item (true capacity) board payload: {size} bytes")
    assert size < 10 * 1024 * 1024, (
        f"payload grew to {size} bytes at the real 192-item ceiling -- "
        "over the 10 MB loose regression bound"
    )


def test_the_rate_limiter_refuses_the_61st_request_in_a_window(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Rate limited profile")
    _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="p" * 16)

    with TestClient(app) as c:
        for i in range(60):
            r = c.get(f"/api/public/boards/{'p' * 16}")
            assert r.status_code == 200, f"request {i + 1} unexpectedly failed: {r.text}"
        r = c.get(f"/api/public/boards/{'p' * 16}")

    assert r.status_code == 429, r.text
