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


def test_list_boards_excludes_a_board_on_an_archived_profile(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Review round 1, IMPORTANT 2. `readable_profile_ids` deliberately does
    NOT filter `is_active` (profile_access.py's own docstring: "Callers
    already do"); `_profile_for_edit` DOES filter it, 404ing on an archived
    profile. Without list_boards filtering it too, an archived profile's board
    would keep listing here while GET/PUT/DELETE on it all 404 -- a listed
    board nothing can open, edit or remove."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Archived profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]

    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        p.is_active = False
        s.commit()

    r = owner.get("/api/boards")

    assert r.status_code == 200, r.text
    ids = {b["id"] for b in r.json()["boards"]}
    assert board_id not in ids

    # Positive control: the same board on the same (still-active) profile
    # DOES list before archiving -- proves the assertion above is the archive
    # filter biting, not some other reason the board never listed at all.
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        p.is_active = True
        s.commit()
    r2 = owner.get("/api/boards")
    assert board_id in {b["id"] for b in r2.json()["boards"]}


# --- review round 1, IMPORTANT 1: settings bounds ---------------------------
# items_per_page had a DB CHECK (ck_boards_items_per_page) but no pydantic
# bound, so 0 reached it as an uncaught IntegrityError -> 500. min_score and
# page_seconds had NEITHER a pydantic bound NOR a DB CHECK, so an out-of-range
# value returned 200 and persisted silently. Each case below is checked at
# both ends: the HTTP status, and the row itself where a silent persist is the
# failure mode a status-only assertion would miss.


def test_creating_a_board_with_an_empty_title_is_422(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Empty create-title profile")
    owner = _client_for(app, owner_id)

    r = _create_board(owner, profile_id=pid, title="")

    assert r.status_code == 422, r.text


def test_settings_title_empty_string_is_422_and_does_not_persist(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Empty settings-title profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid, title="Original title").json()["id"]

    r = owner.put(f"/api/boards/{board_id}", json={"title": ""})

    assert r.status_code == 422, r.text
    row = _get_board_row(pg_session_factory, board_id)
    assert row.title == "Original title"


def test_settings_items_per_page_zero_is_422_not_500(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Items per page zero profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]

    r = owner.put(f"/api/boards/{board_id}", json={"items_per_page": 0})

    assert r.status_code == 422, r.text
    row = _get_board_row(pg_session_factory, board_id)
    assert row.items_per_page == 8  # unchanged -- landscape's default


def test_settings_items_per_page_boundary_one_is_accepted(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Items per page boundary profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]

    r = owner.put(f"/api/boards/{board_id}", json={"items_per_page": 1})

    assert r.status_code == 200, r.text
    assert r.json()["items_per_page"] == 1
    row = _get_board_row(pg_session_factory, board_id)
    assert row.items_per_page == 1


def test_settings_min_score_below_zero_is_422_and_does_not_persist(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Min score negative profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]

    r = owner.put(f"/api/boards/{board_id}", json={"min_score": -50})

    assert r.status_code == 422, r.text
    row = _get_board_row(pg_session_factory, board_id)
    assert row.min_score == 60  # unchanged -- the model's own default


def test_settings_min_score_above_hundred_is_422_and_does_not_persist(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Min score too high profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]

    r = owner.put(f"/api/boards/{board_id}", json={"min_score": 101})

    assert r.status_code == 422, r.text
    row = _get_board_row(pg_session_factory, board_id)
    assert row.min_score == 60


def test_settings_min_score_boundaries_zero_and_hundred_are_accepted(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Min score boundary profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]

    r0 = owner.put(f"/api/boards/{board_id}", json={"min_score": 0})
    assert r0.status_code == 200, r0.text
    assert r0.json()["min_score"] == 0

    r100 = owner.put(f"/api/boards/{board_id}", json={"min_score": 100})
    assert r100.status_code == 200, r100.text
    assert r100.json()["min_score"] == 100
    row = _get_board_row(pg_session_factory, board_id)
    assert row.min_score == 100


def test_settings_page_seconds_zero_is_422_and_does_not_persist(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Page seconds zero profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]

    r = owner.put(f"/api/boards/{board_id}", json={"page_seconds": 0})

    assert r.status_code == 422, r.text
    row = _get_board_row(pg_session_factory, board_id)
    assert row.page_seconds == 30  # unchanged -- the model's own default


def test_settings_page_seconds_boundary_one_is_accepted(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Page seconds boundary profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]

    r = owner.put(f"/api/boards/{board_id}", json={"page_seconds": 1})

    assert r.status_code == 200, r.text
    assert r.json()["page_seconds"] == 1
    row = _get_board_row(pg_session_factory, board_id)
    assert row.page_seconds == 1


def test_the_title_field_carries_the_open5_publish_warning_in_the_schema(monkeypatch, pg_url):
    """Open-5: a private profile's board MAY be published, and the title is
    free text that may name the group. The plan pins this task to carry the
    warning at the point the title is SET so it reaches the OpenAPI schema --
    and, via codegen, Task 10's form. Checking the model class directly would
    not prove that; FastAPI's generated schema is what codegen actually
    reads."""
    app = _setup(monkeypatch, pg_url)
    schema = app.openapi()
    create_title = schema["components"]["schemas"]["BoardCreateIn"]["properties"]["title"]
    settings_title = schema["components"]["schemas"]["BoardSettingsIn"]["properties"]["title"]
    for prop in (create_title, settings_title):
        assert "description" in prop, prop
        assert "publicly visible" in prop["description"]


# --- review round 1, MINOR 3 + MINOR 4: the 409 guard -----------------------


def test_the_409_does_not_blame_orientation_when_items_per_page_alone_shrinks_it(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """MINOR 3. A pure `{"items_per_page": 6}` request on an unchanged
    landscape board must not report "landscape holds 18" -- landscape's own
    default holds 24; the shrink came from items_per_page, not the
    orientation."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Items-per-page shrink profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid, orientation="landscape").json()["id"]
    # landscape cap = 8*3 = 24; shrinking items_per_page to 6 -> cap 18 < 20.
    _approve_n(pg_session_factory, board_id=board_id, approved_by=owner_id, n=20)

    r = owner.put(f"/api/boards/{board_id}", json={"items_per_page": 6})

    assert r.status_code == 409, r.text
    detail = r.json()["error"]["message"]
    assert "landscape" not in detail.lower()
    assert "20" in detail
    assert "18" in detail
    assert "remove 2 first" in detail  # 20 - 18 = 2 to remove; "2" alone
    # can't fail once "20" already passed -- this is what the round-2 review
    # flagged as a vacuous assertion.
    row = _get_board_row(pg_session_factory, board_id)
    assert row.items_per_page == 8  # refused, unchanged


def test_a_bare_title_rename_is_allowed_on_a_board_already_over_capacity(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """MINOR 4. `new_cap < approved_count` alone would refuse every subsequent
    PUT on a board that is already over capacity, including one that touches
    neither orientation nor items_per_page. The guard must fire only when THIS
    request reduces the cap."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Already over-capacity profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid, orientation="portrait").json()["id"]
    # portrait cap = 4*3 = 12; approve past it directly (this file's shortcut,
    # standing in for calls approved under a since-lowered max_pages).
    _approve_n(pg_session_factory, board_id=board_id, approved_by=owner_id, n=15)

    r = owner.put(f"/api/boards/{board_id}", json={"title": "Renamed corridor"})

    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Renamed corridor"
    row = _get_board_row(pg_session_factory, board_id)
    assert row.title == "Renamed corridor"
    assert row.orientation == "portrait"
    assert row.items_per_page == 4


# --- review round 2 ----------------------------------------------------------
# Findings against commits c1e5b961 + 2656d356.


def test_settings_items_per_page_above_64_is_422_and_does_not_persist(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """IMPORTANT: items_per_page had a floor (round 1) but no ceiling. A
    value like 500 sailed through as a silent 200 that corrupted the corridor
    grid -- the same "200 that plants a live corridor-display defect" shape
    round 1 used to justify bounding page_seconds at all."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Items-per-page ceiling profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]

    r = owner.put(f"/api/boards/{board_id}", json={"items_per_page": 65})

    assert r.status_code == 422, r.text
    row = _get_board_row(pg_session_factory, board_id)
    assert row.items_per_page == 8  # unchanged -- the model's own default


def test_settings_items_per_page_absurdly_large_is_422_not_500(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """IMPORTANT: the exact round-2 reproduction. A value that clears int4's
    range entirely must be rejected by pydantic before it ever reaches an
    UPDATE, not sail through the Python capacity guard and die as an
    uncaught out-of-range DB error -- byte-identical in symptom to the
    items_per_page: 0 500 that round 1 was asked to close."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Items-per-page overflow profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]

    r = owner.put(f"/api/boards/{board_id}", json={"items_per_page": 3_000_000_000})

    assert r.status_code == 422, r.text
    row = _get_board_row(pg_session_factory, board_id)
    assert row.items_per_page == 8


def test_settings_items_per_page_boundary_64_is_accepted(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Items-per-page ceiling-ok profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]

    r = owner.put(f"/api/boards/{board_id}", json={"items_per_page": 64})

    assert r.status_code == 200, r.text
    assert r.json()["items_per_page"] == 64
    row = _get_board_row(pg_session_factory, board_id)
    assert row.items_per_page == 64


def test_settings_page_seconds_above_3600_is_422_and_does_not_persist(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """IMPORTANT: page_seconds had a floor (round 1) but no ceiling. A value
    like 100000 sailed through as a silent 200 that would drive Task 10's
    rotation timer into an effectively-frozen page."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Page seconds ceiling profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]

    r = owner.put(f"/api/boards/{board_id}", json={"page_seconds": 3601})

    assert r.status_code == 422, r.text
    row = _get_board_row(pg_session_factory, board_id)
    assert row.page_seconds == 30  # unchanged -- the model's own default


def test_settings_page_seconds_boundary_3600_is_accepted(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Page seconds ceiling-ok profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]

    r = owner.put(f"/api/boards/{board_id}", json={"page_seconds": 3600})

    assert r.status_code == 200, r.text
    assert r.json()["page_seconds"] == 3600
    row = _get_board_row(pg_session_factory, board_id)
    assert row.page_seconds == 3600


def test_a_settings_change_advances_updated_at_past_created_at(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """IMPORTANT: `Board.updated_at` had no `onupdate=func.now()`, so it
    permanently equalled `created_at` even after a settings change --
    update_board is this column's only writer and get_board/list_boards are
    its only readers, so this is Task 5's bug to fix."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Updated-at profile")
    owner = _client_for(app, owner_id)
    created = _create_board(owner, profile_id=pid).json()
    board_id = created["id"]
    assert created["updated_at"] == created["created_at"]

    r = owner.put(f"/api/boards/{board_id}", json={"title": "Renamed"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert datetime.fromisoformat(body["updated_at"]) > datetime.fromisoformat(body["created_at"])
    row = _get_board_row(pg_session_factory, board_id)
    assert row.updated_at > row.created_at


def test_the_409_does_not_blame_orientation_when_an_items_per_page_override_accompanies_the_flip(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """MINOR (round 2): a request that flips orientation AND overrides
    items_per_page in the same body must not blame the flip for the
    resulting cap. The prior code branched on "an orientation was present in
    the body" rather than on what actually determined items_per_page; a
    portrait/4/12-approved board sent
    {"orientation": "landscape", "items_per_page": 3} got "landscape holds
    9", though landscape's own default holds 24."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Flip-plus-override profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid, orientation="portrait").json()["id"]
    # portrait cap = 4*3 = 12; approve up to it.
    _approve_n(pg_session_factory, board_id=board_id, approved_by=owner_id, n=12)

    r = owner.put(
        f"/api/boards/{board_id}",
        json={"orientation": "landscape", "items_per_page": 3},
    )

    # new cap = 3*3 = 9 < 12 approved -- refused.
    assert r.status_code == 409, r.text
    detail = r.json()["error"]["message"]
    assert "landscape" not in detail.lower()
    assert "the new settings hold 9" in detail
    assert "remove 3 first" in detail  # 12 - 9 = 3
    row = _get_board_row(pg_session_factory, board_id)
    assert row.orientation == "portrait"  # refused, unchanged
    assert row.items_per_page == 4


def test_deleting_a_board_with_approved_items_removes_them_too(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """MINOR (round 2): `delete_board` issues a bare `session.delete(board)`,
    safe only because BoardItem.board_id is `ondelete="CASCADE"` with no ORM
    `relationship()` intervening. Both existing delete tests use empty
    boards, so a later change of that ondelete to RESTRICT would ship a 500
    with the suite green."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Delete-with-items profile")
    owner = _client_for(app, owner_id)
    board_id = _create_board(owner, profile_id=pid).json()["id"]
    _approve_n(pg_session_factory, board_id=board_id, approved_by=owner_id, n=3)

    r = owner.delete(f"/api/boards/{board_id}")

    assert r.status_code == 204, r.text
    assert _get_board_row(pg_session_factory, board_id) is None
    with pg_session_factory() as s:
        remaining = (
            s.execute(select(BoardItem).where(BoardItem.board_id == board_id)).scalars().all()
        )
    assert remaining == []
