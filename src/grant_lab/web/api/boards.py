"""Board CRUD and settings — the authenticated corridor-board management surface.

MATCH-edition module. Nothing here may import from grant_lab.proposal_assist --
tests/architecture/test_match_edition_process_cut.py boots a match process in a
fresh interpreter and asserts CONSTRUCTING the app (create_app()) imports zero
proposal_assist modules. This router is mounted in `_ALWAYS`, so it is eagerly
imported by the web/api package `__init__` in every process; see that file's
own comment.

PERMISSIONS COME FROM ONE PLACE. Spec §4.3: "Boards reuse [profiles.py's edit
rule] exactly, so no second permission model appears." Every route that
creates, reads-for-editing, updates or deletes a board calls
`_profile_for_edit` (borrowed as-is, leading underscore kept on purpose -- see
the plan) or, for `GET /api/boards`, the wider `readable_profile_ids` the same
module already composes for reading. DELETE additionally narrows to the
board's own creator or an admin (§4.3: "Delete a board | its creator, or an
admin" -- strictly narrower than "can edit the profile", so an owner who
inherited the profile via a transfer but did not create THIS board cannot
delete it).

`is_published` is intentionally ABSENT from `BoardSettingsIn`. Flipping
publication is admin-only (§4.3) and this settings route is editor-accessible;
declaring the field here would let any editor publish through the back door.
Pydantic's default `extra="ignore"` means a client that sends `is_published`
anyway has it silently dropped, which is the correct behaviour, not an
oversight -- Task 6 adds the admin-only publish route.

Orientation and `items_per_page`: D7 overrides spec §6 (the renderer no longer
infers orientation from the viewport). The author picks portrait or landscape
at creation and `items_per_page` follows `ORIENTATION_ITEMS_PER_PAGE`
(Task 2); it remains an override afterwards via `PUT`. A flip that would
strand already-approved calls is refused with the exact count to remove,
per spec §6's ban on silently truncating a list an admin approved.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from grant_lab.auth.dependencies import get_current_user
from grant_lab.db.models import ORIENTATION_ITEMS_PER_PAGE, Board, BoardItem, Profile, User
from grant_lab.web.api.profiles import _profile_for_edit
from grant_lab.web.profile_access import readable_profile_ids

router = APIRouter(tags=["boards"])

# One plain sentence, carried on both title fields so it reaches the OpenAPI
# schema and, via codegen, Task 10's form. Open-5: a private profile's board
# MAY be published, and the title is free text an owner could use to name the
# group -- the warning has to land at the point the title is SET.
_TITLE_DESCRIPTION = (
    "The board is publicly visible once published, and the title is the one "
    "piece of free text on it, so it should not name anything the profile's "
    "owner would not put on a corridor wall."
)

# secrets.token_urlsafe(16) -> 16 random bytes, 22 URL-safe characters: over
# the >=16-char floor ck_boards_slug_length enforces and under the model's
# String(64). Not a UUID -- §4.3 asks only for "random, URL-safe, >=16 chars",
# and a corridor URL a human may have to read aloud off a screen is better
# short.
_SLUG_BYTES = 16


class BoardOut(BaseModel):
    id: int
    slug: str
    title: str
    profile_id: int
    orientation: Literal["landscape", "portrait"]
    items_per_page: int
    max_pages: int
    page_seconds: int
    min_score: int
    is_published: bool
    created_by: int
    published_by: int | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # "settings + counts" (Produces block). Task 4's board_queue() answers the
    # PENDING side of that; this is the other half, a straight count of
    # board_items, and Task 5 does not call board_queue -- Task 6 wires that up.
    approved_count: int


class BoardsListOut(BaseModel):
    boards: list[BoardOut]


def _approved_counts(session, board_ids: list[int]) -> dict[int, int]:
    """One query for however many boards, not one query per board."""
    if not board_ids:
        return {}
    rows = session.execute(
        select(BoardItem.board_id, func.count())
        .where(BoardItem.board_id.in_(board_ids))
        .group_by(BoardItem.board_id)
    ).all()
    return dict(rows)


def _to_out(board: Board, approved_count: int) -> BoardOut:
    return BoardOut(
        id=board.id,
        slug=board.slug,
        title=board.title,
        profile_id=board.profile_id,
        orientation=board.orientation,
        items_per_page=board.items_per_page,
        max_pages=board.max_pages,
        page_seconds=board.page_seconds,
        min_score=board.min_score,
        is_published=board.is_published,
        created_by=board.created_by,
        published_by=board.published_by,
        published_at=board.published_at,
        created_at=board.created_at,
        updated_at=board.updated_at,
        approved_count=approved_count,
    )


def _board_for_edit(session, *, board_id: int, user: User) -> Board:
    """The board `user` may EDIT, or the right refusal.

    A board's own edit permission is its PROFILE's edit permission (§4.3), so
    this is a 404 lookup on `boards` followed by `_profile_for_edit`'s own
    three-check refusal -- never a second hand-rolled rule.
    """
    board = session.get(Board, board_id)
    if board is None:
        raise HTTPException(status_code=404, detail="board not found")
    _profile_for_edit(session, profile_id=board.profile_id, user=user)
    return board


@router.get("/boards", response_model=BoardsListOut)
def list_boards(request: Request, user: User = Depends(get_current_user)):  # noqa: B008
    """Boards on profiles the caller can READ -- the wider rule, matching
    `GET /api/profiles`. Editing a board's settings is narrower and enforced
    per-route by `_board_for_edit`.

    `readable_profile_ids` deliberately does not filter `is_active` (its own
    docstring: "Callers already do"); `_profile_for_edit` does filter it, 404ing
    on an archived profile. Without this join's `Profile.is_active` clause, an
    archived profile's boards would keep listing here while their detail, PUT
    and DELETE routes all 404 -- a listed board nothing can open, edit or
    remove.
    """
    sf = request.app.state.session_factory
    with sf() as session:
        boards = list(
            session.execute(
                select(Board)
                .join(Profile, Profile.id == Board.profile_id)
                .where(
                    Board.profile_id.in_(readable_profile_ids(user)),
                    Profile.is_active.is_(True),
                )
                .order_by(Board.created_at.desc())
            ).scalars()
        )
        counts = _approved_counts(session, [b.id for b in boards])
        out = [_to_out(b, counts.get(b.id, 0)) for b in boards]
    return BoardsListOut(boards=out)


class BoardCreateIn(BaseModel):
    title: str = Field(..., min_length=1, description=_TITLE_DESCRIPTION)
    profile_id: int
    orientation: Literal["landscape", "portrait"]


@router.post("/boards", response_model=BoardOut, status_code=201)
def create_board(
    request: Request,
    body: BoardCreateIn,
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Create a board. Any user who can EDIT `body.profile_id` -- so an admin
    may build one on a profile they do not own (§4.3). Always unpublished:
    `is_published` has no input here at all, so nothing is public until an
    admin flips it via Task 6's route."""
    sf = request.app.state.session_factory
    with sf() as session:
        _profile_for_edit(session, profile_id=body.profile_id, user=user)
        board = Board(
            slug=secrets.token_urlsafe(_SLUG_BYTES),
            title=body.title,
            profile_id=body.profile_id,
            orientation=body.orientation,
            items_per_page=ORIENTATION_ITEMS_PER_PAGE[body.orientation],
            created_by=user.id,
        )
        session.add(board)
        session.commit()
        session.refresh(board)
        out = _to_out(board, 0)
    return out


@router.get("/boards/{board_id}", response_model=BoardOut)
def get_board(
    request: Request,
    board_id: int,
    user: User = Depends(get_current_user),  # noqa: B008
):
    sf = request.app.state.session_factory
    with sf() as session:
        board = _board_for_edit(session, board_id=board_id, user=user)
        count = _approved_counts(session, [board.id]).get(board.id, 0)
        out = _to_out(board, count)
    return out


class BoardSettingsIn(BaseModel):
    """A partial update: every field is optional and only a field actually
    sent is applied. `is_published` is deliberately not a field here at all --
    see the module docstring -- so a client that sends it has it dropped by
    pydantic's default `extra="ignore"`, not honoured and not rejected."""

    title: str | None = Field(default=None, min_length=1, description=_TITLE_DESCRIPTION)
    orientation: Literal["landscape", "portrait"] | None = None
    # >= 1, <= 64: the DB floor+ceiling is ck_boards_items_per_page
    # ("items_per_page BETWEEN 1 AND 64", migration 0074). 0 passed pydantic
    # before round 1's fix and reached the (then floor-only) CHECK as an
    # uncaught IntegrityError -> 500. The ceiling is round 2's fix: without
    # it, a value that clears int4's range (e.g. 3_000_000_000) sailed past
    # pydantic and the Python capacity guard and died on the UPDATE as an
    # uncaught out-of-range error -> 500, and a value like 500 sailed all the
    # way through as a silent 200 that corrupted the corridor layout. 64 is a
    # guard-rail against nonsense, not a product limit: spec §6's real grids
    # are 8 (landscape 2x4) and 4 (portrait 1x4), and this field exists only
    # as an override for a screen that measures differently, so 64 is roughly
    # eight times the largest real layout.
    items_per_page: int | None = Field(default=None, ge=1, le=64)
    # 0-100: matched against CallRanking.aggregate_score (boards/queue.py),
    # which ranking/aggregate.py documents as "gates + LLM rubric -> 0-100
    # score" and which calls.py's own `min_score` query param already bounds
    # the same way (`Query(default=None, ge=0, le=100)`, web/api/calls.py:718).
    # Unlike items_per_page and page_seconds, this had NO CHECK constraint at
    # all -- an out-of-range value returned 200 and persisted silently.
    min_score: int | None = Field(default=None, ge=0, le=100)
    # >= 1, <= 3600 (one hour): matched by ck_boards_page_seconds (migration
    # 0074). Had no CHECK at all before round 1's fix -- 0 or a negative value
    # returned 200 and would drive Task 10's rotation timer into a busy-loop
    # or no-op. The ceiling is round 2's fix, for the same reason as
    # items_per_page's: one hour per page is already absurd for a rotating
    # corridor display, so anything longer is a typo, not a setting -- a
    # guard-rail against nonsense, not a product limit.
    page_seconds: int | None = Field(default=None, ge=1, le=3600)


@router.put("/boards/{board_id}", response_model=BoardOut)
def update_board(
    request: Request,
    board_id: int,
    body: BoardSettingsIn,
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Update settings, refusing a capacity-REDUCING change.

    `items_per_page` only resets to the new orientation's default when
    orientation is ACTUALLY changing in this request. Sending back the same
    orientation a settings form already had (with no `items_per_page` in the
    body) must not silently clobber a previously chosen override -- only a
    real flip re-derives the default, per D7 ("remains an override").

    The guard fires only when THIS request would make the cap smaller than it
    already is (`new_cap < old_cap`) and that shrunk cap can't hold what is
    already approved. Firing on `new_cap < approved_count` alone -- with no
    comparison to the cap the board already had -- would refuse an unrelated
    edit (a bare title rename) on a board that was already over capacity, and
    would blame `new_orientation` for a shrink that `items_per_page` alone
    caused.
    """
    sf = request.app.state.session_factory
    with sf() as session:
        board = _board_for_edit(session, board_id=board_id, user=user)
        approved_count = _approved_counts(session, [board.id]).get(board.id, 0)

        orientation_changed = body.orientation is not None and body.orientation != board.orientation
        new_orientation = body.orientation if body.orientation is not None else board.orientation
        if body.items_per_page is not None:
            new_items_per_page = body.items_per_page
        elif orientation_changed:
            new_items_per_page = ORIENTATION_ITEMS_PER_PAGE[new_orientation]
        else:
            new_items_per_page = board.items_per_page

        old_cap = board.items_per_page * board.max_pages
        new_cap = new_items_per_page * board.max_pages
        if new_cap < old_cap and new_cap < approved_count:
            # Only name the orientation when it is what DETERMINED
            # new_items_per_page -- i.e. the same condition that branch above
            # uses to derive it from ORIENTATION_ITEMS_PER_PAGE. Branching on
            # "an orientation was present in the body" instead (round 1's
            # bug) misattributes a request that flips orientation AND
            # overrides items_per_page in the same body: a
            # portrait/4/12-approved board sent
            # {"orientation":"landscape","items_per_page":3} got "landscape
            # holds 9", though landscape's own default holds 24 -- the shrink
            # came from the items_per_page override, not the flip.
            if orientation_changed and body.items_per_page is None:
                cap_description = f"{new_orientation} holds {new_cap}"
            else:
                cap_description = f"the new settings hold {new_cap}"
            raise HTTPException(
                status_code=409,
                detail=(
                    f"this board has {approved_count} approved calls; "
                    f"{cap_description} — remove "
                    f"{approved_count - new_cap} first"
                ),
            )

        if body.title is not None:
            board.title = body.title
        if orientation_changed:
            board.orientation = new_orientation
        if body.items_per_page is not None or orientation_changed:
            board.items_per_page = new_items_per_page
        if body.min_score is not None:
            board.min_score = body.min_score
        if body.page_seconds is not None:
            board.page_seconds = body.page_seconds

        session.commit()
        session.refresh(board)
        out = _to_out(board, approved_count)
    return out


@router.delete("/boards/{board_id}", status_code=204)
def delete_board(
    request: Request,
    board_id: int,
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Creator or admin ONLY -- narrower than "can edit" (§4.3). An owner who
    inherited the profile through a transfer (Task 3) can edit this board's
    settings but must not be able to delete a board somebody else built."""
    sf = request.app.state.session_factory
    with sf() as session:
        board = _board_for_edit(session, board_id=board_id, user=user)
        if board.created_by != user.id and not user.is_admin:
            raise HTTPException(
                status_code=403,
                detail="only the board's creator or an admin may delete it",
            )
        session.delete(board)
        session.commit()
    return Response(status_code=204)
