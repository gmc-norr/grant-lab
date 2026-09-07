"""The auto-publish switch, and the preview that stops the cap being silent."""

from __future__ import annotations

import inspect

from grant_lab.db.models import Board
from grant_lab.web.api.boards import (
    MAX_BOARD_ITEMS,
    ORIENTATION_ITEMS_PER_PAGE,
    BoardSettingsIn,
    auto_preview,
)
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

    # IMPORTANT 6 (branch review). The remedy this 409 names -- "remove N
    # first" -- was written when `unapprove_item` was a plain delete. It now
    # writes a `BoardSuppression` per removal, which keeps that call off this
    # board until somebody approves it again; on an auto board that is the
    # only thing that can undo it. An operator who follows this instruction is
    # taking a durable action, and the message has to say so.
    detail = r.json()["error"]["message"]
    assert "remove 16 first" in detail, detail  # 24 approved - (8 x 1) capacity
    assert "suppress" in detail, f"the 409 does not say what removal now costs: {detail!r}"

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


def test_an_auto_board_may_shrink_below_its_item_count(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """The capacity-shrink guard exists to stop a human's hand-picked cards
    being silently dropped by a smaller cap. An auto board has no hand-picked
    cards: `reconcile_auto_boards` rebuilds the item set from the candidate
    query every run and deletes whatever is not in the new, `LIMIT cap`
    selection. So on an auto board the guard refuses a change on behalf of
    rows that tonight's run deletes anyway -- and the remedy it names is
    worse than the disease, because `unapprove_item` writes a
    `BoardSuppression` per removal, permanently blacklisting 16 calls from
    this board to accomplish a trim the job would have done for free.
    """
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="shrink auto")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="G" * 16)
    client = _client_for(app, owner)
    with client:
        # Flip to auto while the board is empty, so the guard cannot fire here
        # and this test's subject is only the second PUT.
        r = client.put(f"/api/boards/{bid}", json={"auto_publish": True})
        assert r.status_code == 200, r.text

        # Landscape defaults: 8 x 3 = 24, full.
        _approve_n(pg_session_factory, board_id=bid, approved_by=owner, n=24, profile_id=pid)

        r = client.put(f"/api/boards/{bid}", json={"max_pages": 1})

    assert r.status_code == 200, f"an auto board was refused a shrink: {r.text}"
    with pg_session_factory() as s:
        board = s.get(Board, bid)
        assert board.max_pages == 1, "the accepted change was not applied"
        assert board.auto_publish is True


def test_turning_auto_OFF_while_shrinking_is_still_refused(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """The trap in the exemption above, and the reason it reads the REQUEST.

    `old_cap` is read off the BOARD and `new_cap` off the REQUEST, because the
    guard compares before with after. The auto flag is the opposite: what
    matters is who will own this board's items AFTER this request. If the
    board ends up auto, the 05:00 job owns them and trims to the cap itself.
    If it ends up MANUAL, a human owns them and the cap must hold them.

    So an exemption keyed on `board.auto_publish` -- the value already stored
    -- skips the guard on exactly the request that needs it most: auto off and
    a smaller cap in one body, which the settings form can send in a single
    save. That leaves a manual board holding 24 cards in a cap of 8, and the
    only way out is 16 removals that each write a suppression.
    """
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="shrink auto off")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="H" * 16)
    client = _client_for(app, owner)
    with client:
        r = client.put(f"/api/boards/{bid}", json={"auto_publish": True})
        assert r.status_code == 200, r.text
        _approve_n(pg_session_factory, board_id=bid, approved_by=owner, n=24, profile_id=pid)

        r = client.put(f"/api/boards/{bid}", json={"auto_publish": False, "max_pages": 1})

    assert r.status_code == 409, (
        f"a board being returned to MANUAL was let through the shrink guard: {r.text}"
    )
    # The advice has to be right for THIS case. Refusing the request refuses
    # the shrink with it, so the capacity is still 24 and the next run trims to
    # 24 -- telling this admin to "just wait" would be wrong, and telling them
    # to "remove 16 first" would cost 16 permanent suppressions. The smaller
    # capacity has to be saved on its own first.
    detail = r.json()["error"]["message"]
    assert "Save the smaller capacity on its own first" in detail, detail
    assert "remove 16 first" not in detail, detail
    with pg_session_factory() as s:
        board = s.get(Board, bid)
        assert board.max_pages == 3, "the refused change was applied anyway"
        assert board.auto_publish is True, "the refused request still flipped the switch"


def test_turning_auto_ON_while_shrinking_is_allowed(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """The other half of reading the REQUEST. A board flipped manual -> auto
    already loses its hand-picked items on the first run -- that is
    spec-mandated ("replaces the board's hand-picked items") and the UI warns
    with the count. Shrinking the cap in the same body therefore takes nothing
    the flip was not already taking, so refusing it would guard rows that this
    very request has already condemned.
    """
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="shrink auto on")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="I" * 16)
    _approve_n(pg_session_factory, board_id=bid, approved_by=owner, n=24, profile_id=pid)

    client = _client_for(app, owner)
    with client:
        r = client.put(f"/api/boards/{bid}", json={"auto_publish": True, "max_pages": 1})

    assert r.status_code == 200, f"the manual -> auto flip was refused a shrink: {r.text}"
    with pg_session_factory() as s:
        board = s.get(Board, bid)
        assert board.max_pages == 1
        assert board.auto_publish is True


def _bounds(metadata) -> dict[str, int]:
    """The ge/le a pydantic field or a FastAPI Query actually declares.

    Both keep their constraints as the same `annotated_types` objects, which is
    what lets the preview's parameters and the PUT's model be compared without
    either side being retyped into this test.
    """
    out = {}
    for meta in metadata:
        for attr in ("ge", "le"):
            value = getattr(meta, attr, None)
            if value is not None:
                out[attr] = value
    return out


def test_the_preview_answers_for_a_threshold_that_is_not_saved(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Spec Sec 6 requires this preview to be LIVE -- the admin reads the four
    numbers BEFORE saving. It used to read only the stored board, so an admin
    who had just lowered the minimum score was shown the OLD numbers, and
    `truncated` -- the one number the preview exists to surface -- is exactly
    what a lowered floor changes most.

    Board default min_score is 60 (db/models/boards.py), so of these twenty
    calls ten qualify as saved and all twenty qualify at a floor of 40.
    """
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="preview live")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="J" * 16)
    for i in range(10):
        _ranked_call(pg_session_factory, external_id=f"live-hi-{i}", profile_id=pid, score=70)
    for i in range(10):
        _ranked_call(pg_session_factory, external_id=f"live-lo-{i}", profile_id=pid, score=50)

    client = _client_for(app, owner)
    with client:
        saved = client.get(f"/api/boards/{bid}/auto-preview")
        whatif = client.get(f"/api/boards/{bid}/auto-preview", params={"min_score": 40})

    assert saved.status_code == 200, saved.text
    assert saved.json()["qualifying"] == 10, "a bare call must still describe the SAVED board"
    assert whatif.status_code == 200, whatif.text
    assert whatif.json()["qualifying"] == 20, "the preview ignored the threshold it was given"

    # A GET that persists is the failure mode of doing this by assigning onto
    # the loaded ORM row: it would look right in the response and save on the
    # next flush.
    with pg_session_factory() as s:
        assert s.get(Board, bid).min_score == 60, "a read-only preview wrote to the board"


def test_the_preview_derives_the_page_size_of_an_orientation_it_is_only_asked_about(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """The settings form OMITS `items_per_page` unless the user edited it, so
    the server re-derives it from the new orientation. A preview that echoed
    the stored landscape 8 would describe an 8-up board while Save built a 4-up
    one -- and the client cannot fix that by keeping its own copy of the map,
    because then two copies decide one number.

    So the preview takes the ORIENTATION and runs `_effective_items_per_page`,
    the same function `update_board` applies.
    """
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="preview orientation")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="K" * 16)
    for i in range(30):
        _ranked_call(pg_session_factory, external_id=f"orient-{i}", profile_id=pid, score=90)

    portrait_per_page = ORIENTATION_ITEMS_PER_PAGE["portrait"]
    client = _client_for(app, owner)
    with client:
        r = client.get(f"/api/boards/{bid}/auto-preview", params={"orientation": "portrait"})

    assert r.status_code == 200, r.text
    body = r.json()
    # max_pages stays 3, so the cap follows the DERIVED page size, not the
    # stored 8. Landscape would give 24 displayed and 6 truncated.
    assert body["displayed"] == portrait_per_page * 3
    assert body["truncated"] == 30 - portrait_per_page * 3
    assert body["pages"] == 3
    assert body["displayed"] != 24, "the preview used the stored landscape page size"


def test_the_preview_refuses_the_same_product_the_PUT_refuses(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Both fields cap at 64 and their PRODUCT does not, so without this an
    authenticated caller could ask `board_selection` for a 4096-row LIMIT on a
    GET -- the same unbounded product that made the public board route a
    ~182 MB response (see MAX_BOARD_ITEMS).

    It matters just as much that the refusal MATCHES the PUT's. A preview that
    accepted a combination Save then rejects would show an admin numbers for a
    board they cannot have, from the very screen they consult first.
    """
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="preview product")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="L" * 16)
    client = _client_for(app, owner)
    with client:
        preview = client.get(
            f"/api/boards/{bid}/auto-preview", params={"items_per_page": 64, "max_pages": 64}
        )
        put = client.put(f"/api/boards/{bid}", json={"items_per_page": 64, "max_pages": 64})

    assert preview.status_code == 409, f"a 4096-card preview was accepted: {preview.text}"
    assert put.status_code == 409, put.text
    assert preview.json()["error"]["message"] == put.json()["error"]["message"], (
        "the preview and the PUT refuse the same board with different words"
    )
    assert str(MAX_BOARD_ITEMS) in preview.json()["error"]["message"]


def test_the_previews_bounds_are_the_settings_PUTs_bounds():
    """Retyping `ge=0, le=100` onto the preview is how these drift: the two
    live in different statements, and the day someone widens the PUT nothing
    goes red. Narrower, an admin cannot preview a board they may save; WIDER,
    they preview one Save refuses -- numbers that look right under a button
    that fails.

    This reads what the route actually declares, so it fails whether the bound
    was hard-coded or `_settings_bound` started returning the wrong thing.
    """
    declared = inspect.signature(auto_preview).parameters
    for field in ("min_score", "items_per_page", "max_pages", "page_seconds"):
        got = _bounds(declared[field].default.metadata)
        expected = _bounds(BoardSettingsIn.model_fields[field].metadata)
        assert expected, f"BoardSettingsIn.{field} declares no bounds for the preview to mirror"
        assert got == expected, f"preview {field} bounds {got} != the PUT's {expected}"


def test_handing_an_over_capacity_auto_board_back_to_manual_is_refused(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """REPRODUCED by an outside review, in TWO requests that are each fine.

    The auto exemption made an over-capacity board a legitimate, reachable
    state -- the 05:00 job trims it. But that makes the moment control returns
    to MANUAL the moment nothing is left to trim it, and the shrink guard could
    not see that moment: request two shrinks nothing, so `new_cap < old_cap` is
    False and the guard never ran.

        1. auto board, 24 items, {"max_pages": 1}      -> 200 (the exemption)
        2. {"auto_publish": false}, on its own         -> was 200, now 409

    The single-request form (`{"auto_publish": false, "max_pages": 1}`) was
    already refused. Splitting it in two walked around the guard, which is why
    a gate written against ONE save proved less than its own comment claimed.
    """
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="handback")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="M" * 16)
    client = _client_for(app, owner)
    with client:
        assert client.put(f"/api/boards/{bid}", json={"auto_publish": True}).status_code == 200
        _approve_n(pg_session_factory, board_id=bid, approved_by=owner, n=24, profile_id=pid)

        shrink = client.put(f"/api/boards/{bid}", json={"max_pages": 1})
        handback = client.put(f"/api/boards/{bid}", json={"auto_publish": False})

    assert shrink.status_code == 200, f"the auto exemption stopped working: {shrink.text}"
    assert handback.status_code == 409, (
        f"a MANUAL board was left holding 24 cards in a cap of 8: {handback.text}"
    )

    # The remedy has to be the CHEAP one. Auto-fill is still on at this moment,
    # so tonight's run trims for free; "remove 16 first" would cost 16 permanent
    # suppressions to buy the same thing.
    detail = handback.json()["error"]["message"]
    assert "nightly run" in detail, f"the 409 does not name the free remedy: {detail!r}"
    assert "remove 16 first" not in detail, f"the 409 prescribes 16 suppressions: {detail!r}"

    with pg_session_factory() as s:
        board = s.get(Board, bid)
        assert board.auto_publish is True, "the refused request turned auto-fill off anyway"


def test_turning_auto_off_on_a_board_that_FITS_is_still_allowed(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """The other side of the guard above, and the reason it is not simply
    'refuse every hand-back'. A board whose items fit its capacity has nothing
    for the nightly job to fix, so returning it to manual breaks no invariant.

    Without this, the fix for the bypass would be indistinguishable from
    banning the auto -> manual flip outright -- which the settings form offers
    as an ordinary toggle.
    """
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="handback fits")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="N" * 16)
    client = _client_for(app, owner)
    with client:
        assert client.put(f"/api/boards/{bid}", json={"auto_publish": True}).status_code == 200
        # 4 items against the landscape default capacity of 8 x 3 = 24.
        _approve_n(pg_session_factory, board_id=bid, approved_by=owner, n=4, profile_id=pid)
        r = client.put(f"/api/boards/{bid}", json={"auto_publish": False})

    assert r.status_code == 200, f"a board that fits was refused the flip to manual: {r.text}"
    with pg_session_factory() as s:
        assert s.get(Board, bid).auto_publish is False
