"""Board CRUD, settings, and the D7 flip refusal (Plan C Task 5).

FIXTURES. `pg_session_factory` (tests/conftest.py:151) is a FACTORY --
`with pg_session_factory() as s:`. `profile_factory` (:184) returns a
`(profile_id, user_id)` tuple, takes no `visibility`, and its profiles default
to `visibility='org'` (db/models/profiles.py:121-123) -- every active user can
READ one unless the test flips it to 'private'. The authenticated-request
pattern (`_setup` -> `create_app()`, then a `TestClient` carrying
`app.state.session_signer.make(user_id=uid)` in the `session` cookie) is
copied from tests/test_api_profile_detail.py:8-20.

Permissions come from `_profile_for_edit` (profiles.py:126), borrowed as-is by
boards.py -- so "reader" below means a user on a visibility='org' profile who
is neither its owner nor an admin: they can READ it (403, not 404) but not
edit it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import Board, BoardItem, Call, Profile, Source, User
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


def _set_owner(pg_session_factory, profile_id, new_owner_id):
    """Move ownership directly -- this file tests boards.py, not the Task 3
    transfer route, so it takes the shortcut of writing the column."""
    with pg_session_factory() as s:
        p = s.get(Profile, profile_id)
        p.created_by = new_owner_id
        s.commit()


def _create_board(client, *, profile_id, orientation="landscape", title="Corridor board"):
    return client.post(
        "/api/boards",
        json={"title": title, "profile_id": profile_id, "orientation": orientation},
    )


def _get_board_row(pg_session_factory, board_id) -> Board:
    with pg_session_factory() as s:
        return s.get(Board, board_id)


def _approve_n(pg_session_factory, *, board_id, approved_by, n):
    """Approve `n` distinct calls onto `board_id`."""
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        for i in range(n):
            c = Call(
                source_id=src.id,
                external_id=f"board-{board_id}-{i}",
                url=f"https://example.org/board-{board_id}-{i}",
                title=f"Call {i}",
                deadline=FUTURE,
            )
            s.add(c)
            s.flush()
            s.add(
                BoardItem(
                    board_id=board_id,
                    call_id=c.id,
                    position=i,
                    approved_by=approved_by,
                )
            )
        s.commit()


# --- create + permissions ------------------------------------------------


def test_a_profile_editor_can_create_a_board(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Owner's profile")
    owner = _client_for(app, owner_id)

    r = _create_board(owner, profile_id=pid)

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["profile_id"] == pid
    assert body["created_by"] == owner_id
    row = _get_board_row(pg_session_factory, body["id"])
    assert row is not None
    assert row.profile_id == pid
    assert row.created_by == owner_id


def test_an_admin_can_create_a_board_on_a_profile_they_do_not_own(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Someone else's profile")
    admin_id = _new_user(pg_session_factory, "admin@umu.se", is_admin=True)
    admin = _client_for(app, admin_id)

    r = _create_board(admin, profile_id=pid)

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["profile_id"] == pid
    assert body["created_by"] == admin_id


def test_a_reader_who_cannot_edit_the_profile_gets_403(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    # Default visibility is 'org' -- every active user can READ it.
    pid, owner_id = profile_factory(name="Org-visible profile")
    reader_id = _new_user(pg_session_factory, "reader@umu.se")
    reader = _client_for(app, reader_id)

    r = _create_board(reader, profile_id=pid)

    assert r.status_code == 403, r.text
    assert b"not authorized" in r.content


def test_a_stranger_gets_404_not_403(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Private profile")
    _set_visibility(pg_session_factory, pid, "private")
    stranger_id = _new_user(pg_session_factory, "stranger@umu.se")
    stranger = _client_for(app, stranger_id)

    r = stranger.post(
        "/api/boards", json={"title": "Wall", "profile_id": pid, "orientation": "landscape"}
    )
    assert r.status_code == 404, r.text
    assert b"profile not found" in r.content

    # Positive control: the owner's identical request succeeds, so the 404
    # above is the read-permission check and not a broken route.
    owner = _client_for(app, owner_id)
    r2 = _create_board(owner, profile_id=pid)
    assert r2.status_code == 201, r2.text


def test_a_new_board_is_unpublished(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Fresh board profile")
    owner = _client_for(app, owner_id)

    r = _create_board(owner, profile_id=pid)

    assert r.status_code == 201, r.text
    assert r.json()["is_published"] is False
    row = _get_board_row(pg_session_factory, r.json()["id"])
    assert row.is_published is False
    assert row.published_by is None
    assert row.published_at is None


# --- slug -----------------------------------------------------------------


def test_the_slug_is_at_least_16_chars_and_url_safe(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Slug profile")
    owner = _client_for(app, owner_id)

    r = _create_board(owner, profile_id=pid)

    slug = r.json()["slug"]
    assert len(slug) >= 16
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
    assert set(slug) <= allowed


def test_two_boards_get_different_slugs(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Two boards profile")
    owner = _client_for(app, owner_id)

    r1 = _create_board(owner, profile_id=pid, title="Board 1")
    r2 = _create_board(owner, profile_id=pid, title="Board 2")

    assert r1.json()["slug"] != r2.json()["slug"]


# --- D7: orientation -> items_per_page ------------------------------------


def test_choosing_portrait_sets_items_per_page_to_4(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Portrait profile")
    owner = _client_for(app, owner_id)

    r = _create_board(owner, profile_id=pid, orientation="portrait")

    assert r.status_code == 201, r.text
    assert r.json()["items_per_page"] == 4


def test_choosing_landscape_sets_items_per_page_to_8(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Landscape profile")
    owner = _client_for(app, owner_id)

    r = _create_board(owner, profile_id=pid, orientation="landscape")

    assert r.status_code == 201, r.text
    assert r.json()["items_per_page"] == 8


def test_items_per_page_can_still_be_overridden(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Override profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid, orientation="landscape").json()["id"]

    r = owner.put(f"/api/boards/{board_id}", json={"items_per_page": 6})

    assert r.status_code == 200, r.text
    assert r.json()["items_per_page"] == 6
    assert r.json()["orientation"] == "landscape"
    row = _get_board_row(pg_session_factory, board_id)
    assert row.items_per_page == 6


# --- Open-4: the flip refusal ----------------------------------------------


def test_flipping_to_portrait_over_capacity_is_REFUSED_with_the_count(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Overflow profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid, orientation="landscape").json()["id"]
    # landscape cap = 8 * max_pages(3) = 24; portrait cap = 4 * 3 = 12.
    _approve_n(pg_session_factory, board_id=board_id, approved_by=owner_id, n=15)

    r = owner.put(f"/api/boards/{board_id}", json={"orientation": "portrait"})

    assert r.status_code == 409, r.text
    detail = r.json()["error"]["message"]
    assert "15" in detail
    assert "12" in detail
    assert "3" in detail  # 15 - 12 = 3 to remove
    row = _get_board_row(pg_session_factory, board_id)
    assert row.orientation == "landscape"  # refused, unchanged


def test_flipping_to_portrait_under_capacity_is_allowed(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Fits profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid, orientation="landscape").json()["id"]
    _approve_n(pg_session_factory, board_id=board_id, approved_by=owner_id, n=5)

    r = owner.put(f"/api/boards/{board_id}", json={"orientation": "portrait"})

    assert r.status_code == 200, r.text
    assert r.json()["orientation"] == "portrait"
    assert r.json()["items_per_page"] == 4
    row = _get_board_row(pg_session_factory, board_id)
    assert row.orientation == "portrait"


def test_flipping_to_landscape_is_always_allowed(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Always ok profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid, orientation="portrait").json()["id"]
    # portrait cap = 4 * 3 = 12; approve right up to it.
    _approve_n(pg_session_factory, board_id=board_id, approved_by=owner_id, n=12)

    r = owner.put(f"/api/boards/{board_id}", json={"orientation": "landscape"})

    assert r.status_code == 200, r.text
    assert r.json()["orientation"] == "landscape"
    assert r.json()["items_per_page"] == 8


# --- delete -----------------------------------------------------------------


def test_the_creator_can_delete_a_board(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Delete-by-creator profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]

    r = owner.delete(f"/api/boards/{board_id}")

    assert r.status_code == 204, r.text
    assert _get_board_row(pg_session_factory, board_id) is None


def test_an_admin_can_delete_someone_elses_board(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Delete-by-admin profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]
    admin_id = _new_user(pg_session_factory, "admin2@umu.se", is_admin=True)
    admin = _client_for(app, admin_id)

    r = admin.delete(f"/api/boards/{board_id}")

    assert r.status_code == 204, r.text
    assert _get_board_row(pg_session_factory, board_id) is None


def test_a_non_creator_non_admin_editor_cannot_delete(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """§4.3: delete is narrower than edit. Transfer the PROFILE to a second
    user (a direct column write -- Task 3's route is not under test here) so
    that user can edit the board's settings but did not create the board."""
    app = _setup(monkeypatch, pg_url)
    pid, creator_id = profile_factory(name="Inherited profile")
    creator = _client_for(app, creator_id)
    board_id = _create_board(creator, profile_id=pid).json()["id"]

    new_owner_id = _new_user(pg_session_factory, "new-owner@umu.se")
    _set_owner(pg_session_factory, pid, new_owner_id)
    new_owner = _client_for(app, new_owner_id)

    # Sanity: the new owner really can edit (positive control for the narrower
    # delete check below).
    put_r = new_owner.put(f"/api/boards/{board_id}", json={"title": "Renamed"})
    assert put_r.status_code == 200, put_r.text

    r = new_owner.delete(f"/api/boards/{board_id}")

    assert r.status_code == 403, r.text
    assert _get_board_row(pg_session_factory, board_id) is not None


# --- privilege escalation ---------------------------------------------------


def test_a_non_admin_cannot_publish_through_the_settings_route(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="No sneaky publish profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]

    r = owner.put(f"/api/boards/{board_id}", json={"is_published": True})

    assert r.status_code == 200, r.text
    row = _get_board_row(pg_session_factory, board_id)
    assert row.is_published is False
    assert row.published_by is None


# --- read routes -------------------------------------------------------------
# Not in the brief's 17 -- added on self-review. Every case above reads its
# result off the DB row or off a POST/PUT response body, so GET /api/boards and
# GET /api/boards/{board_id} had zero coverage: a broken `_approved_counts` join,
# or a `list_boards` filter that returned every board regardless of
# `readable_profile_ids`, would have passed the whole file.


def test_get_board_returns_settings_and_the_approved_count(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Get-board profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid, orientation="portrait").json()["id"]
    _approve_n(pg_session_factory, board_id=board_id, approved_by=owner_id, n=2)

    r = owner.get(f"/api/boards/{board_id}")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == board_id
    assert body["profile_id"] == pid
    assert body["orientation"] == "portrait"
    assert body["items_per_page"] == 4
    assert body["approved_count"] == 2


def test_list_boards_only_shows_boards_on_profiles_the_caller_can_read(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    visible_pid, visible_owner_id = profile_factory(name="Visible-to-all profile")
    hidden_pid, hidden_owner_id = profile_factory(name="Hidden profile")
    _set_visibility(pg_session_factory, hidden_pid, "private")

    visible_owner = _client_for(app, visible_owner_id)
    visible_board_id = _create_board(visible_owner, profile_id=visible_pid).json()["id"]
    hidden_owner = _client_for(app, hidden_owner_id)
    hidden_board_id = _create_board(hidden_owner, profile_id=hidden_pid).json()["id"]

    outsider_id = _new_user(pg_session_factory, "outsider@umu.se")
    outsider = _client_for(app, outsider_id)

    r = outsider.get("/api/boards")

    assert r.status_code == 200, r.text
    ids = {b["id"] for b in r.json()["boards"]}
    assert visible_board_id in ids  # org-visible profile: readable by anyone
    assert hidden_board_id not in ids  # private profile: not this caller's
