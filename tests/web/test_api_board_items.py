"""Approve, un-approve, reorder, publish (Plan C Task 6).

FIXTURES. Same shapes as tests/web/test_api_boards_crud.py: `pg_session_factory`
(tests/conftest.py:151) is a FACTORY, `profile_factory` (:184) returns a
`(profile_id, user_id)` tuple with `visibility='org'` by default
(db/models/profiles.py:121-123), and the authenticated-request pattern is a
`TestClient` carrying `app.state.session_signer.make(user_id=uid)` in the
`session` cookie.

PERMISSIONS. Approve/un-approve/reorder come from `_board_for_edit` ->
`_profile_for_edit` -- the SAME rule as Task 5's CRUD, so "editor" below means
a user who can edit the board's profile (its creator, or an admin). Publish is
narrower: admin-only, checked AFTER `_board_for_edit`, exactly the shape
`delete_board` uses for its own creator-or-admin narrowing (Task 5).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import (
    ORIENTATION_ITEMS_PER_PAGE,
    Board,
    BoardItem,
    Call,
    CallDecision,
    CallRanking,
    Profile,
    Source,
    User,
)
from grant_lab.main import create_app

FUTURE = datetime.now(UTC) + timedelta(days=30)


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _client_for(app, uid):
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def _new_user(pg_session_factory, email, *, is_admin=False):
    with pg_session_factory() as s:
        u = User(status="active", email=email, is_admin=is_admin)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _set_visibility(pg_session_factory, profile_id, visibility):
    with pg_session_factory() as s:
        p = s.get(Profile, profile_id)
        p.visibility = visibility
        s.commit()


def _create_board(client, *, profile_id, orientation="landscape", title="Corridor board"):
    return client.post(
        "/api/boards",
        json={"title": title, "profile_id": profile_id, "orientation": orientation},
    )


def _get_board_row(pg_session_factory, board_id) -> Board:
    with pg_session_factory() as s:
        return s.get(Board, board_id)


def _make_call(pg_session_factory, *, external_id, deadline=FUTURE):
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id=external_id,
            url=f"https://example.org/{external_id}",
            title=f"Call {external_id}",
            deadline=deadline,
        )
        s.add(c)
        s.commit()
        return c.id


def _rank(pg_session_factory, *, call_id, profile_id, score):
    with pg_session_factory() as s:
        s.add(
            CallRanking(
                call_id=call_id,
                profile_id=profile_id,
                aggregate_score=score,
                rationale="r",
                model="gemma4:e4b-q8",
                prompt_version="v2.0",
            )
        )
        s.commit()


def _approve_n(pg_session_factory, *, board_id, approved_by, n, profile_id=None, min_score=90):
    """Approve `n` distinct calls onto `board_id` directly (this file's
    shortcut for pre-seeding state, mirroring test_api_boards_crud.py's own
    `_approve_n` -- not testing the approve ROUTE, which each test below
    exercises separately)."""
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        for i in range(n):
            c = Call(
                source_id=src.id,
                external_id=f"seed-{board_id}-{i}",
                url=f"https://example.org/seed-{board_id}-{i}",
                title=f"Seed call {i}",
                deadline=FUTURE,
            )
            s.add(c)
            s.flush()
            s.add(BoardItem(board_id=board_id, call_id=c.id, position=i, approved_by=approved_by))
            if profile_id is not None:
                s.add(
                    CallRanking(
                        call_id=c.id,
                        profile_id=profile_id,
                        aggregate_score=min_score,
                        rationale="r",
                        model="gemma4:e4b-q8",
                        prompt_version="v2.0",
                    )
                )
        s.commit()


def _items(pg_session_factory, board_id):
    with pg_session_factory() as s:
        rows = (
            s.execute(
                select(BoardItem).where(BoardItem.board_id == board_id).order_by(BoardItem.position)
            )
            .scalars()
            .all()
        )
        return rows


# --- approve: writes, bulk-by-ids, bulk-by-score ----------------------------


def test_approving_writes_approved_by_and_approved_at(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Approve-writes profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]
    call_id = _make_call(pg_session_factory, external_id="writes-approved-by")
    _rank(pg_session_factory, call_id=call_id, profile_id=pid, score=90)

    r = owner.post(f"/api/boards/{board_id}/items", json={"call_ids": [call_id]})

    assert r.status_code == 200, r.text
    rows = _items(pg_session_factory, board_id)
    assert len(rows) == 1
    assert rows[0].call_id == call_id
    assert rows[0].approved_by == owner_id
    assert rows[0].approved_at is not None


def test_bulk_approve_by_call_ids(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Bulk by ids profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]
    ids = [_make_call(pg_session_factory, external_id=f"bulk-ids-{i}") for i in range(3)]
    for cid in ids:
        _rank(pg_session_factory, call_id=cid, profile_id=pid, score=90)

    r = owner.post(f"/api/boards/{board_id}/items", json={"call_ids": ids})

    assert r.status_code == 200, r.text
    body_ids = {item["call_id"] for item in r.json()["items"]}
    assert body_ids == set(ids)
    rows = _items(pg_session_factory, board_id)
    assert {row.call_id for row in rows} == set(ids)
    # Sequential, gap-free positions for a fresh board.
    assert sorted(row.position for row in rows) == [0, 1, 2]


def test_approve_everything_at_or_above_N(monkeypatch, pg_url, pg_session_factory, profile_factory):
    """§4.3 bulk: "an 'approve everything at or above N' action". N here (80)
    is a tighter floor than the board's OWN min_score (default 60, so all
    three candidates already clear the queue's own gate) -- only the request's
    own floor should decide what gets approved."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Approve at or above N profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]
    high_id = _make_call(pg_session_factory, external_id="score-90")
    mid_id = _make_call(pg_session_factory, external_id="score-75")
    low_id = _make_call(pg_session_factory, external_id="score-65")
    _rank(pg_session_factory, call_id=high_id, profile_id=pid, score=90)
    _rank(pg_session_factory, call_id=mid_id, profile_id=pid, score=75)
    _rank(pg_session_factory, call_id=low_id, profile_id=pid, score=65)

    r = owner.post(f"/api/boards/{board_id}/items", json={"min_score": 80})

    assert r.status_code == 200, r.text
    rows = _items(pg_session_factory, board_id)
    assert {row.call_id for row in rows} == {high_id}


# --- the queue's waiting_count -----------------------------------------------


def test_the_queue_reports_a_waiting_count(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Waiting count profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]
    approved_id = _make_call(pg_session_factory, external_id="already-approved-q")
    waiting_id = _make_call(pg_session_factory, external_id="waiting-q")
    _rank(pg_session_factory, call_id=approved_id, profile_id=pid, score=90)
    _rank(pg_session_factory, call_id=waiting_id, profile_id=pid, score=70)
    owner.post(f"/api/boards/{board_id}/items", json={"call_ids": [approved_id]})

    r = owner.get(f"/api/boards/{board_id}/queue")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["waiting_count"] == 1
    call_ids_in_queue = {item["call_id"] for item in body["items"]}
    assert call_ids_in_queue == {approved_id, waiting_id}
    flagged = {item["call_id"]: item["already_approved"] for item in body["items"]}
    assert flagged[approved_id] is True
    assert flagged[waiting_id] is False


# --- capacity: Gate A --------------------------------------------------------


def test_approving_past_capacity_is_refused_and_names_the_overflow(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Overflow profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid, orientation="landscape").json()["id"]
    # landscape cap = 8 * max_pages(3) = 24.
    _approve_n(pg_session_factory, board_id=board_id, approved_by=owner_id, n=24)
    extra_id = _make_call(pg_session_factory, external_id="the-25th")
    _rank(pg_session_factory, call_id=extra_id, profile_id=pid, score=90)

    r = owner.post(f"/api/boards/{board_id}/items", json={"call_ids": [extra_id]})

    assert r.status_code == 409, r.text
    detail = r.json()["error"]["message"]
    assert "drop 1" in detail  # names the overflow: 1 to drop. A substring
    # of just "1" is also satisfied by "approving 1 more" and would stay
    # green even if the "drop N" clause were dropped from the message
    # entirely (MINOR 4).
    rows = _items(pg_session_factory, board_id)
    assert len(rows) == 24


def test_capacity_uses_the_STORED_items_per_page_not_a_default(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """D7: `items_per_page` remains an OVERRIDE after creation, applied via
    `PUT`. A board created straight through the route (as the old version of
    this test did) has `items_per_page` exactly equal to
    `ORIENTATION_ITEMS_PER_PAGE[orientation]` -- the DEFAULT -- so it cannot
    tell a cap that reads the board's own STORED column apart from one that
    falls back to `ORIENTATION_ITEMS_PER_PAGE[board.orientation]` and ignores
    the column entirely (IMPORTANT 3): both compute the same number. This
    test PUTs the board to a non-default `items_per_page` first, then proves
    the cap follows THAT stored value: seeded up to the (now stale) default
    cap, one more approval should SUCCEED because the true, overridden cap
    is higher -- a cap that fell back to the default would wrongly REFUSE it.

    Also derives every count from the imported `ORIENTATION_ITEMS_PER_PAGE`
    and the board's own `max_pages`, never restating either as a literal."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Stored override capacity profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid, orientation="portrait").json()["id"]
    default_items_per_page = ORIENTATION_ITEMS_PER_PAGE["portrait"]
    overridden_items_per_page = default_items_per_page + 1  # a non-default override
    put_r = owner.put(f"/api/boards/{board_id}", json={"items_per_page": overridden_items_per_page})
    assert put_r.status_code == 200, put_r.text
    board = _get_board_row(pg_session_factory, board_id)
    default_cap = default_items_per_page * board.max_pages
    true_cap = overridden_items_per_page * board.max_pages
    assert true_cap > default_cap  # the override must actually raise capacity

    # Seed right up to the (stale) DEFAULT cap directly -- a cap that fell
    # back to the default would refuse the next approval; the true,
    # overridden cap must allow the remaining room it actually has.
    _approve_n(pg_session_factory, board_id=board_id, approved_by=owner_id, n=default_cap)
    extra_ids = [
        _make_call(pg_session_factory, external_id=f"stored-override-{i}")
        for i in range(true_cap - default_cap)
    ]
    for cid in extra_ids:
        _rank(pg_session_factory, call_id=cid, profile_id=pid, score=90)

    r = owner.post(f"/api/boards/{board_id}/items", json={"call_ids": extra_ids})

    assert r.status_code == 200, r.text
    rows = _items(pg_session_factory, board_id)
    assert len(rows) == true_cap


def test_a_bulk_approve_that_would_exceed_capacity_approves_NONE(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """A partial bulk approve is the silent truncation §6 forbids, just
    performed by the server instead of the display."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Approves none profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid, orientation="portrait").json()["id"]
    # portrait cap = 12; pre-approve 10, leaving room for exactly 2 more.
    _approve_n(pg_session_factory, board_id=board_id, approved_by=owner_id, n=10)
    new_ids = [_make_call(pg_session_factory, external_id=f"bulk-overflow-{i}") for i in range(5)]
    for cid in new_ids:
        _rank(pg_session_factory, call_id=cid, profile_id=pid, score=90)

    r = owner.post(f"/api/boards/{board_id}/items", json={"call_ids": new_ids})

    assert r.status_code == 409, r.text
    rows = _items(pg_session_factory, board_id)
    assert len(rows) == 10  # NONE of the 5 landed -- not "2 fit, 3 refused"
    assert {row.call_id for row in rows}.isdisjoint(set(new_ids))


def test_approving_zero_call_ids_is_a_noop_even_over_capacity(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """MINOR 6: an empty selection changes nothing, so it must succeed even
    on a board that somehow already sits OVER its own cap (e.g. legacy data,
    or a cap that was never enforced on a direct-write path). `approved_count
    + len(new_ids) > cap` with `new_ids == []` reduces to `approved_count >
    cap` -- true here by construction -- a 409 for a request that would not
    have changed anything. (A board merely AT its cap, `approved_count ==
    cap`, does NOT trigger the pre-fix bug: `cap > cap` is false either way,
    so the test must put the board strictly OVER cap to distinguish the two
    behaviours -- seeding directly via `_approve_n`, which bypasses the
    route's own cap check, same as a legacy/over-cap board would arise.)"""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Zero call_ids over capacity profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid, orientation="landscape").json()["id"]
    board = _get_board_row(pg_session_factory, board_id)
    cap = board.items_per_page * board.max_pages
    _approve_n(pg_session_factory, board_id=board_id, approved_by=owner_id, n=cap + 1)

    r = owner.post(f"/api/boards/{board_id}/items", json={"call_ids": []})

    assert r.status_code == 200, r.text
    rows = _items(pg_session_factory, board_id)
    assert len(rows) == cap + 1  # unchanged


def test_approving_an_id_outside_the_queues_candidate_set_is_allowed(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """MINOR 5, pinned deliberately -- NOT a bug being fixed here.
    `approve_items` validates `call_ids` only against `Call` existing at all;
    it never checks that a call is a CANDIDATE in this board's own queue
    (`board_queue`'s filters: a `CallRanking` row for this profile,
    `aggregate_score >= board.min_score`, and a future deadline). A call with
    NO `CallRanking` row at all for this profile -- so it would never even
    appear in `GET .../queue` -- can still be approved directly by id. D3
    treats approval and the queue as deliberately different states; this
    test pins the current behaviour so a later change to it is a deliberate
    product decision, not an accident."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Candidate bypass profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]
    # Deliberately no _rank() call -- no CallRanking row for `pid` at all.
    unranked_id = _make_call(pg_session_factory, external_id="no-ranking-at-all")

    r = owner.post(f"/api/boards/{board_id}/items", json={"call_ids": [unranked_id]})

    assert r.status_code == 200, r.text
    rows = _items(pg_session_factory, board_id)
    assert [row.call_id for row in rows] == [unranked_id]


# --- permissions: approve/un-approve are editor-level, publish is admin ------


def test_an_editor_who_is_not_admin_can_approve(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Editor can approve profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]
    call_id = _make_call(pg_session_factory, external_id="editor-approve")
    _rank(pg_session_factory, call_id=call_id, profile_id=pid, score=90)

    r = owner.post(f"/api/boards/{board_id}/items", json={"call_ids": [call_id]})

    assert r.status_code == 200, r.text
    rows = _items(pg_session_factory, board_id)
    assert [row.call_id for row in rows] == [call_id]


def test_an_editor_who_is_not_admin_can_UNapprove(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Editor can unapprove profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]
    _approve_n(pg_session_factory, board_id=board_id, approved_by=owner_id, n=1)
    call_id = _items(pg_session_factory, board_id)[0].call_id

    r = owner.delete(f"/api/boards/{board_id}/items/{call_id}")

    assert r.status_code == 204, r.text
    assert _items(pg_session_factory, board_id) == []


# --- IMPORTANT 2: _board_for_edit itself, pinned on the two mutating routes --
# Before this, no test in this file could tell `_board_for_edit(session,
# board_id=board_id, user=user)` apart from `session.get(Board, board_id)` on
# ANY of the six new routes -- swapping it in `approve_items` or
# `unapprove_item` left all 17 (then) tests green, meaning any authenticated
# org user could approve onto or strip any board. Each test below follows
# tests/web/test_api_boards_crud.py's own shape for the identical check on
# `POST /api/boards` (asserting the response BODY, not just the status code --
# a missing route also answers 404/403-shaped errors) and pairs the refusal
# with a positive control: the owner's IDENTICAL request, in the same test,
# succeeding -- so a 404 that only means "route doesn't exist" cannot pass.


def test_a_reader_who_cannot_edit_the_profile_gets_403_on_approve(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """A reader is a user who CAN read the board's profile (default
    visibility='org') but does not own it and is not an admin -- so
    `_profile_for_edit` refuses with 403 "not authorized", not 404."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Reader cannot approve profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]
    # A REAL call id, not []: with an empty list `_items(...) == []` below
    # holds whether or not the refusal worked, so the state assertion would be
    # decorative. This way the mutant is caught writing a row onto a board it
    # has no rights to.
    call_id = _make_call(pg_session_factory, external_id="reader-approve-target")
    _rank(pg_session_factory, call_id=call_id, profile_id=pid, score=90)
    reader_id = _new_user(pg_session_factory, "reader-approve@umu.se")
    reader = _client_for(app, reader_id)

    r = reader.post(f"/api/boards/{board_id}/items", json={"call_ids": [call_id]})

    assert r.status_code == 403, r.text
    assert b"not authorized" in r.content
    assert _items(pg_session_factory, board_id) == []

    # Positive control: the owner's identical request succeeds, so the 403
    # above is the edit-permission check and not a broken route.
    r2 = owner.post(f"/api/boards/{board_id}/items", json={"call_ids": [call_id]})
    assert r2.status_code == 200, r2.text
    assert [i.call_id for i in _items(pg_session_factory, board_id)] == [call_id]


def test_a_stranger_gets_404_not_403_on_approve(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """A stranger has NO access at all (private profile, not owner, not
    admin) -- `_profile_for_edit` refuses with 404 "profile not found", never
    revealing the profile exists."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Stranger cannot approve profile")
    _set_visibility(pg_session_factory, pid, "private")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]
    # A REAL call id, not []: with an empty list `_items(...) == []` below
    # holds whether or not the refusal worked, so the state assertion would be
    # decorative. This way the mutant is caught writing a row onto a board it
    # has no rights to.
    call_id = _make_call(pg_session_factory, external_id="stranger-approve-target")
    _rank(pg_session_factory, call_id=call_id, profile_id=pid, score=90)
    stranger_id = _new_user(pg_session_factory, "stranger-approve@umu.se")
    stranger = _client_for(app, stranger_id)

    r = stranger.post(f"/api/boards/{board_id}/items", json={"call_ids": [call_id]})

    assert r.status_code == 404, r.text
    assert b"profile not found" in r.content
    assert _items(pg_session_factory, board_id) == []

    # Positive control: the owner's identical request succeeds.
    r2 = owner.post(f"/api/boards/{board_id}/items", json={"call_ids": [call_id]})
    assert r2.status_code == 200, r2.text
    assert [i.call_id for i in _items(pg_session_factory, board_id)] == [call_id]


def test_a_reader_who_cannot_edit_the_profile_gets_403_on_unapprove(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Reader cannot unapprove profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]
    _approve_n(pg_session_factory, board_id=board_id, approved_by=owner_id, n=1)
    call_id = _items(pg_session_factory, board_id)[0].call_id
    reader_id = _new_user(pg_session_factory, "reader-unapprove@umu.se")
    reader = _client_for(app, reader_id)

    r = reader.delete(f"/api/boards/{board_id}/items/{call_id}")

    assert r.status_code == 403, r.text
    assert b"not authorized" in r.content
    assert [row.call_id for row in _items(pg_session_factory, board_id)] == [call_id]

    # Positive control: the owner's identical request succeeds.
    r2 = owner.delete(f"/api/boards/{board_id}/items/{call_id}")
    assert r2.status_code == 204, r2.text
    assert _items(pg_session_factory, board_id) == []


def test_a_stranger_gets_404_not_403_on_unapprove(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Stranger cannot unapprove profile")
    _set_visibility(pg_session_factory, pid, "private")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]
    _approve_n(pg_session_factory, board_id=board_id, approved_by=owner_id, n=1)
    call_id = _items(pg_session_factory, board_id)[0].call_id
    stranger_id = _new_user(pg_session_factory, "stranger-unapprove@umu.se")
    stranger = _client_for(app, stranger_id)

    r = stranger.delete(f"/api/boards/{board_id}/items/{call_id}")

    assert r.status_code == 404, r.text
    assert b"profile not found" in r.content
    assert [row.call_id for row in _items(pg_session_factory, board_id)] == [call_id]

    # Positive control: the owner's identical request succeeds.
    r2 = owner.delete(f"/api/boards/{board_id}/items/{call_id}")
    assert r2.status_code == 204, r2.text
    assert _items(pg_session_factory, board_id) == []


def test_an_editor_who_is_not_admin_CANNOT_publish(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Editor cannot publish profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]

    r = owner.put(f"/api/boards/{board_id}/publish", json={"is_published": True})

    assert r.status_code == 403, r.text
    row = _get_board_row(pg_session_factory, board_id)
    assert row.is_published is False


def test_an_admin_can_publish(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Admin publishes profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]
    admin_id = _new_user(pg_session_factory, "publish-admin@umu.se", is_admin=True)
    admin = _client_for(app, admin_id)

    r = admin.put(f"/api/boards/{board_id}/publish", json={"is_published": True})

    assert r.status_code == 200, r.text
    assert r.json()["is_published"] is True


def test_publishing_records_published_by_and_published_at(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Publish records profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]
    admin_id = _new_user(pg_session_factory, "records-admin@umu.se", is_admin=True)
    admin = _client_for(app, admin_id)

    r = admin.put(f"/api/boards/{board_id}/publish", json={"is_published": True})

    assert r.status_code == 200, r.text
    row = _get_board_row(pg_session_factory, board_id)
    assert row.published_by == admin_id
    assert row.published_at is not None


def test_unpublishing_is_admin_only_too(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Unpublish admin only profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]
    admin_id = _new_user(pg_session_factory, "unpub-admin@umu.se", is_admin=True)
    admin = _client_for(app, admin_id)
    pub_r = admin.put(f"/api/boards/{board_id}/publish", json={"is_published": True})
    assert pub_r.status_code == 200, pub_r.text
    published_at_after_publish = _get_board_row(pg_session_factory, board_id).published_at

    editor_r = owner.put(f"/api/boards/{board_id}/publish", json={"is_published": False})
    assert editor_r.status_code == 403, editor_r.text
    assert _get_board_row(pg_session_factory, board_id).is_published is True

    admin_r = admin.put(f"/api/boards/{board_id}/publish", json={"is_published": False})
    assert admin_r.status_code == 200, admin_r.text
    row = _get_board_row(pg_session_factory, board_id)
    assert row.is_published is False
    # Unpublishing LEAVES published_by/published_at -- they record who made it
    # public and when, which survives the board coming back down.
    assert row.published_by == admin_id
    assert row.published_at == published_at_after_publish


def test_publishing_a_board_on_a_PRIVATE_profile_is_allowed(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Open-5: a private profile's board MAY be published; publication is
    allowed regardless of Profile.visibility."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Private profile can publish")
    _set_visibility(pg_session_factory, pid, "private")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]
    admin_id = _new_user(pg_session_factory, "private-admin@umu.se", is_admin=True)
    admin = _client_for(app, admin_id)

    r = admin.put(f"/api/boards/{board_id}/publish", json={"is_published": True})

    assert r.status_code == 200, r.text
    assert r.json()["is_published"] is True


# --- reorder ------------------------------------------------------------------


def test_reorder_rewrites_positions_and_rejects_an_unknown_call_id(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Reorder profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]
    _approve_n(pg_session_factory, board_id=board_id, approved_by=owner_id, n=3)
    ids_in_position_order = [row.call_id for row in _items(pg_session_factory, board_id)]
    reversed_order = list(reversed(ids_in_position_order))

    r = owner.put(f"/api/boards/{board_id}/items", json={"call_ids": reversed_order})

    assert r.status_code == 200, r.text
    new_order = [item["call_id"] for item in r.json()["items"]]
    assert new_order == reversed_order
    rows = _items(pg_session_factory, board_id)
    assert [row.call_id for row in rows] == reversed_order

    # All three real ids PLUS one unknown one -- isolates "contains an
    # unrecognized call_id" from "is missing an approved one" (dropping a
    # real id instead would trigger both refusals at once and this status-
    # only assertion could not tell which one fired).
    unknown_id = 9_999_999
    bad_order = [*reversed_order, unknown_id]
    r2 = owner.put(f"/api/boards/{board_id}/items", json={"call_ids": bad_order})

    assert r2.status_code == 422, r2.text
    # Refused -- order from the first (successful) reorder is unchanged.
    rows_after = _items(pg_session_factory, board_id)
    assert [row.call_id for row in rows_after] == reversed_order


# --- D3: a hide never removes an already-approved item ----------------------


def test_a_hide_after_approval_does_NOT_remove_the_item(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Hide after approval profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]
    call_id = _make_call(pg_session_factory, external_id="hidden-after-approval")
    _rank(pg_session_factory, call_id=call_id, profile_id=pid, score=90)
    approve_r = owner.post(f"/api/boards/{board_id}/items", json={"call_ids": [call_id]})
    assert approve_r.status_code == 200, approve_r.text

    with pg_session_factory() as s:
        s.add(CallDecision(user_id=owner_id, profile_id=pid, call_id=call_id, state="hidden"))
        s.commit()

    r = owner.get(f"/api/boards/{board_id}/items")

    assert r.status_code == 200, r.text
    assert call_id in {item["call_id"] for item in r.json()["items"]}


# --- Open-5, repeated on the publish path -----------------------------------
# Not in the brief's 15 numbered tests -- added on self-review. Task 5 put the
# warning on the two INPUT models (BoardCreateIn, BoardSettingsIn); Task 6's
# BoardOut.title change is what makes it reachable from the PUBLISH path (the
# response model PUT /publish returns), so this is the one schema location
# that actually proves Open-5's "not only in a Python comment" -- without it
# the warning could be silently dropped from BoardOut and every existing test
# would stay green.


def test_the_publish_response_schema_also_carries_the_open5_warning(monkeypatch, pg_url):
    app = _setup(monkeypatch, pg_url)
    schema = app.openapi()
    board_out_title = schema["components"]["schemas"]["BoardOut"]["properties"]["title"]
    assert "description" in board_out_title, board_out_title
    assert "publicly visible" in board_out_title["description"]
