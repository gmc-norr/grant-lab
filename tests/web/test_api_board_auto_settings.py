"""The auto-publish switch, and the preview that stops the cap being silent."""

from __future__ import annotations

from grant_lab.db.models import Board
from grant_lab.web.api.boards import MAX_BOARD_ITEMS
from tests.test_board_auto_selection import _ranked_call
from tests.test_boards_qr_at_approval import _client_for, _make_board, _setup
from tests.web.test_api_board_items import _approve_n

# `_approve_n` seeds N approved BoardItems directly (and their CallRankings
# when `profile_id` is given). It is the only helper that can build the
# at-capacity board gate 4 needs. It does NOT clash with anything above:
# `_make_call` is deliberately NOT imported here.


def test_the_switch_can_be_set(monkeypatch, pg_url, pg_session_factory, profile_factory):
    """GATE 1."""
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="switch gate1")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="A" * 16)
    client = _client_for(app, owner)
    with client:
        r = client.put(f"/api/boards/{bid}", json={"auto_publish": True})
    assert r.status_code == 200, r.text
    assert r.json()["auto_publish"] is True


def test_max_pages_can_be_raised(monkeypatch, pg_url, pg_session_factory, profile_factory):
    """GATE 2. Without this an admin cannot fix a truncated auto board."""
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="switch gate2")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="B" * 16)
    client = _client_for(app, owner)
    with client:
        r = client.put(f"/api/boards/{bid}", json={"max_pages": 18})
    assert r.status_code == 200, r.text
    assert r.json()["max_pages"] == 18


def test_the_preview_separates_qualifying_from_displayed(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 3 -- the load-bearing one. A preview that returns ONE number cannot
    show truncation, and truncation is the failure this exists to surface.

    Board defaults are items_per_page=8, max_pages=3 (db/models/boards.py:93-101),
    so 30 qualifying calls give 24 displayed and 6 truncated."""
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="preview gate3")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="C" * 16)
    for i in range(30):
        _ranked_call(pg_session_factory, external_id=f"prev-{i}", profile_id=pid, score=90)
    client = _client_for(app, owner)
    with client:
        r = client.get(f"/api/boards/{bid}/auto-preview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["qualifying"] == 30
    assert body["displayed"] == 24
    assert body["truncated"] == 6
    assert body["pages"] == 3
    assert body["loop_seconds"] == 90  # 3 pages x page_seconds 30


def test_shrinking_max_pages_below_approved_count_is_refused(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 4 -- the one that stops this task shipping a reachable capacity
    bypass. Exposing `max_pages` disarms `update_board`'s guard unless the
    guard is changed in the same breath. REPRODUCED in the dry run: without
    the Step 3 fix this PUT returns 200, all 24 rows survive, and the board's
    real capacity is now 8. Any profile editor can do it.

    `approve_items`' own docstring calls this shape 'exactly the silent
    truncation spec Sec 6 forbids, just performed by the server instead of the
    display'."""
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="switch gate4")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="D" * 16)
    # Landscape defaults: items_per_page 8 x max_pages 3 = capacity 24, full.
    _approve_n(pg_session_factory, board_id=bid, approved_by=owner, n=24, profile_id=pid)

    client = _client_for(app, owner)
    with client:
        r = client.put(f"/api/boards/{bid}", json={"max_pages": 1})
    assert r.status_code == 409, f"a capacity-shrinking max_pages was accepted: {r.text}"

    with pg_session_factory() as s:
        board = s.get(Board, bid)
        assert board.max_pages == 3, "the refused change was applied anyway"


def test_orientation_is_not_blamed_for_a_max_pages_shrink(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """MINOR 1 (fix round 1). `update_board`'s 409 message named the
    orientation as the cause of a capacity shrink whenever
    `orientation_changed` was true, even when `body.max_pages` -- not the
    orientation's own default -- is what actually produced the smaller cap.

    Reachable in a single save from the real UI: the settings form always
    sends both `orientation` and `max_pages` together. A portrait 4x3 board
    (items_per_page 4 x max_pages 3 = 12, the board's real capacity) with 12
    approved calls, flipped to landscape with max_pages: 1, was refused with
    'landscape holds 8' -- though landscape's own default (8 x 3 = 24) does
    not shrink anything; the max_pages override does.

    The numbers and the refusal are correct; only the named cause was wrong.
    """
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="switch minor1")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="F" * 16)
    client = _client_for(app, owner)

    with client:
        # Flip to portrait first (no approved calls yet, so the guard cannot
        # fire): items_per_page follows the orientation default, 4.
        r = client.put(f"/api/boards/{bid}", json={"orientation": "portrait"})
        assert r.status_code == 200, r.text
        assert r.json()["items_per_page"] == 4

        # Fill the portrait board to its real capacity: 4 x 3 = 12.
        _approve_n(pg_session_factory, board_id=bid, approved_by=owner, n=12, profile_id=pid)

        # Flip back to landscape AND shrink max_pages in the same body -- the
        # shape the settings form always sends.
        r = client.put(f"/api/boards/{bid}", json={"orientation": "landscape", "max_pages": 1})

    assert r.status_code == 409, f"expected the shrink to be refused: {r.text}"
    detail = r.json()["error"]["message"]
    assert "landscape holds" not in detail, (
        f"orientation was blamed for a max_pages-caused shrink: {detail!r}"
    )
    assert "the new settings hold 8" in detail, detail


def test_a_partial_last_page_still_counts_as_a_page(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 5 -- added because a mutation proved gate 3 could not see this.

    Gate 3's board is exactly full: 24 displayed at 8 per page. Ceiling and
    FLOOR division agree on 24/8, so replacing `-(-displayed //
    items_per_page)` with `displayed // items_per_page` left gate 3 GREEN
    (mutation M8, measured). That mutant is not hypothetical: an auto board
    that is not full is the ordinary case, and on 10 displayed at 8 per page
    it reports 1 page instead of 2 -- the 10th card never reaches the wall and
    `loop_seconds` under-reports by a third, which is precisely the silent
    truncation this preview exists to make visible.

    10 qualifying, well under the 8 x 3 = 24 cap, so nothing is truncated and
    the only thing under test is the page arithmetic."""
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="preview gate5")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="E" * 16)
    for i in range(10):
        _ranked_call(pg_session_factory, external_id=f"part-{i}", profile_id=pid, score=90)
    client = _client_for(app, owner)
    with client:
        r = client.get(f"/api/boards/{bid}/auto-preview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["qualifying"] == 10
    assert body["displayed"] == 10
    assert body["truncated"] == 0
    assert body["pages"] == 2, "a partial last page is still a page"
    assert body["loop_seconds"] == 60  # 2 pages x page_seconds 30


def test_the_product_of_items_per_page_and_max_pages_is_capped(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 6 -- the branch review's CRITICAL 1.

    `items_per_page` and `max_pages` are each bounded at 64; their PRODUCT was
    not. `{"items_per_page": 64, "max_pages": 64}` is 4096 cards, 21x the 192
    every cost measurement on the unauthenticated `/api/public/boards/{slug}`
    route was taken at, and `boards/public.py` serves every row with no limit
    of its own -- at the measured 44.4 KB/item that is a ~182 MB response
    behind only a 60-req/15-min per-IP limiter. Any profile EDITOR could send
    this PUT; on an auto board the 05:00 job then fills it with no further
    human action.

    Both halves are asserted: the refusal, AND a re-read from the database
    proving NEITHER field was applied. A 409 that had already written the row
    would be the same defect with a different status code."""
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="switch gate6")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="G" * 16)
    client = _client_for(app, owner)
    with client:
        r = client.put(f"/api/boards/{bid}", json={"items_per_page": 64, "max_pages": 64})

    assert r.status_code == 409, f"a 4096-item board was accepted: {r.text}"
    detail = r.json()["error"]["message"]
    assert "4096" in detail, detail
    assert str(MAX_BOARD_ITEMS) in detail, detail

    with pg_session_factory() as s:
        board = s.get(Board, bid)
        assert board.items_per_page == 8, "the refused items_per_page was applied anyway"
        assert board.max_pages == 3, "the refused max_pages was applied anyway"
        assert board.items_per_page * board.max_pages <= MAX_BOARD_ITEMS


def test_a_large_board_inside_the_cap_is_still_accepted(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 7 -- the over-refusal half of GATE 6.

    A total-item guard that refused everything would pass GATE 6 and break the
    feature. The spec's own worked example tops out at `max_pages: 18`, which
    at `items_per_page: 8` is 144 -- comfortably inside 192 and the largest
    shape any document on this branch asks for. It must still return 200 and
    persist both fields."""
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="switch gate7")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="H" * 16)
    client = _client_for(app, owner)
    with client:
        r = client.put(f"/api/boards/{bid}", json={"items_per_page": 8, "max_pages": 18})

    assert r.status_code == 200, f"a legitimate 144-item board was refused: {r.text}"
    assert MAX_BOARD_ITEMS >= 8 * 18, "this gate must stay inside the cap it is not testing"
    body = r.json()
    assert body["items_per_page"] == 8
    assert body["max_pages"] == 18
    with pg_session_factory() as s:
        board = s.get(Board, bid)
        assert (board.items_per_page, board.max_pages) == (8, 18)
