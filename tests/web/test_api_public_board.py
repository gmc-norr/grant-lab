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

    Fix round 2: this 3000-character URL is now also well past the new,
    much smaller `_MAX_URL_BYTES` (180) legibility guard, so it returns the
    sentinel via THAT branch, not the `DataOverflowError` one -- either way
    the guarantee this test pins (one bad card, not a 500) holds. See
    `test_a_url_just_over_the_legibility_bound_costs_one_qr_not_the_whole_board`
    below for a test that lands exactly on the new, tighter boundary.
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


def test_a_url_just_over_the_legibility_bound_costs_one_qr_not_the_whole_board(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """CRITICAL, fix round 2: `_MAX_URL_BYTES` (180 UTF-8 bytes, derived from
    spec Sec6's 170px legibility floor at ~3 px/module -- see `qr.py`) is a
    much tighter ceiling than the `DataOverflowError` one the test above
    exercises. One byte past IT must cost one missing QR, not the whole
    board -- the same guarantee, at the boundary that actually governs real
    funder URLs.
    """
    from grant_lab.boards.qr import _MAX_URL_BYTES

    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Just-over-bound profile")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="u" * 16)
    base = "https://funder.example/"
    over_url = base + "x" * (_MAX_URL_BYTES + 1 - len(base))
    assert len(over_url.encode("utf-8")) == _MAX_URL_BYTES + 1
    ok_call = _make_call(pg_session_factory, external_id="just-over-ok", title="Fine card")
    bad_call = _make_call(
        pg_session_factory, external_id="just-over-bad", title="Just-over card", url=over_url
    )
    _approve(
        pg_session_factory, board_id=board_id, call_id=ok_call, approved_by=owner_id, position=0
    )
    _approve(
        pg_session_factory, board_id=board_id, call_id=bad_call, approved_by=owner_id, position=1
    )

    with TestClient(app) as c:
        r = c.get(f"/api/public/boards/{'u' * 16}")

    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 2
    assert items[0]["title"] == "Fine card"
    assert items[0]["qr_modules"] > 0
    assert items[0]["qr_path"]
    assert items[1]["title"] == "Just-over card"
    assert items[1]["qr_modules"] == 0
    assert items[1]["qr_path"] == ""


def test_a_url_at_the_legibility_bound_still_gets_a_real_qr(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """The inverse of the test above: a URL AT `_MAX_URL_BYTES` (180 bytes,
    exactly) is still within the legibility floor and must get a real,
    scannable QR (measured: 53 modules, segno version 9), not the sentinel.
    """
    from grant_lab.boards.qr import _MAX_URL_BYTES

    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="At-bound profile")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="v" * 16)
    base = "https://funder.example/"
    at_bound_url = base + "x" * (_MAX_URL_BYTES - len(base))
    assert len(at_bound_url.encode("utf-8")) == _MAX_URL_BYTES
    call_id = _make_call(pg_session_factory, external_id="at-bound", url=at_bound_url)
    _approve(pg_session_factory, board_id=board_id, call_id=call_id, approved_by=owner_id)

    with TestClient(app) as c:
        r = c.get(f"/api/public/boards/{'v' * 16}")

    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert item["qr_modules"] == 53
    assert item["qr_path"]


def test_the_full_board_payload_size_stays_under_budget(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Fix round 2, IMPORTANT: re-derived against the `_MAX_URL_BYTES`
    legibility guard (`qr.py`) -- the earlier version of this test used a
    fixed 129-character fixture and never varied URL length, so it could
    not see that a DEFAULT board (`items_per_page=8, max_pages=3` -> 24
    items, no configuration change) with long ingested URLs breached the
    512 KB budget by 6.3x before the guard existed (24 x 134,073-byte paths
    at the old 2331-byte `DataOverflowError` boundary = 3.2 MB).

    Both axes stated explicitly, per that finding:
    - ITEM COUNT: 24 -- this board's own `items_per_page * max_pages`
      (8 * 3), i.e. a board nobody configured beyond the defaults.
    - URL LENGTH: `_MAX_URL_BYTES` (180 bytes) exactly -- the WORST url the
      guard still permits through to a real QR encode. Anything longer than
      this returns the free `(modules=0, path="")` sentinel instead, so 180
      bytes is now the true per-item worst case, not an arbitrary fixture
      length.

    So this is no longer merely a per-item-cost regression detector (as the
    prior revision candidly admitted it was) -- with URL length now bounded
    on this axis too, 24 items x the guard's own worst-case URL IS the
    default board's true payload ceiling. Measured (this file's own `-s`
    run, `uv run pytest tests/web/test_api_public_board.py -q -k
    payload_size -s`): a 180-byte URL is 53 modules / 11,152 bytes of path
    per item, 267,648 bytes of path data for 24 items, and **276,916 bytes**
    (270 KB) for the whole serialized body -- comfortably under the 512 KB
    (524,288 byte) budget (1.9x headroom), so 512 KB HOLDS for a default
    board at the worst permitted URL length and is left unchanged.
    """
    from grant_lab.boards.qr import _MAX_URL_BYTES

    base = "https://www.vinnova.se/en/calls-for-proposals/"
    long_url = base + "a" * (_MAX_URL_BYTES - len(base))
    assert len(long_url.encode("utf-8")) == _MAX_URL_BYTES

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


def test_the_full_board_payload_at_the_true_worst_case_ceiling(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Fix round 2, IMPORTANT: renamed from
    `..._at_the_true_capacity_ceiling`. That name was false: it and the
    512 KB test above it both used the SAME fixed 129-character fixture, so
    neither varied URL length, and the "true capacity ceiling" it claimed to
    pin was really only an ITEM-COUNT ceiling at one arbitrary URL length --
    before this round's guard, the real ceiling was 25.74 MB (a
    2331-character URL x 192 items), 2.6x over the 10 MB this test asserted.
    A reader trusting the old name would have concluded the response was
    bounded at 10 MB when it was not.

    With `_MAX_URL_BYTES` (`qr.py`) now capping the OTHER axis too, this
    test uses that bound's own worst-permitted URL (not the old arbitrary
    129-character one) together with the real item-count ceiling, so the
    name can now honestly claim to be the true worst case:
    - ITEM COUNT: `items_per_page(<=64) * max_pages(default 3)` = 192,
      `web/api/boards.py`'s `approve_items` ceiling -- a legitimately
      configured board's maximum, not a hypothetical.
    - URL LENGTH: `_MAX_URL_BYTES` (180 bytes) exactly -- anything longer
      returns the free sentinel instead of a real QR, so this is the most
      any single item can ever cost.

    Both axes are now bounded by something the CODE enforces (`approve_items`
    for the first, `qr_for`'s own guard for the second), not by a fixture
    choice, so the measured number below is the actual maximum this endpoint
    can ever emit, not a heuristic sample. Measured (this file's own `-s`
    run, `uv run pytest tests/web/test_api_public_board.py -q -k
    worst_case_ceiling -s`): **2,214,385 bytes** (2.11 MB) -- 192 items x
    (53-module / 11,152-byte-path QR + title/JSON overhead). The bound
    asserted is TIGHT (3 MB, not the old loose 10 MB) because it no longer
    needs headroom for a URL length nobody can reach; a future change that
    grows this by even ~35% is worth someone's attention.
    """
    from grant_lab.boards.qr import _MAX_URL_BYTES

    base = "https://www.vinnova.se/en/calls-for-proposals/"
    long_url = base + "a" * (_MAX_URL_BYTES - len(base))
    assert len(long_url.encode("utf-8")) == _MAX_URL_BYTES

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
    print(f"full {ceiling}-item true worst-case board payload: {size} bytes")
    assert size < 3 * 1024 * 1024, (
        f"payload grew to {size} bytes at the true 192-item / "
        f"{_MAX_URL_BYTES}-byte-URL worst case -- over the re-derived 3 MB bound"
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
