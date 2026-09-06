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
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm.exc import ObjectDeletedError

from grant_lab.auth.dependencies import get_current_user
from grant_lab.boards.alerts import owner_undeliverable_map
from grant_lab.boards.qr import qr_for
from grant_lab.boards.queue import board_queue
from grant_lab.db.models import ORIENTATION_ITEMS_PER_PAGE, Board, BoardItem, Call, Profile, User
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
    # Open-5, repeated here (Task 6): the input models (BoardCreateIn,
    # BoardSettingsIn) already carried this description, but BoardOut is what
    # GET, list, create, update AND the publish route all return -- Task 10's
    # publish confirmation dialog reads the CURRENT title back off this model,
    # not off an input schema, so the caution has to reach the OUTPUT side too
    # or the one dialog that most needs it never sees it.
    title: str = Field(..., description=_TITLE_DESCRIPTION)
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
    # D9 / Open-3 half 2: whether a board alert on THIS board's profile would
    # currently be escalated away from its owner -- matches
    # `owner_undeliverable_map`'s own docstring term for term (Task 8 review
    # round 2, Important 4): the owner cannot be resolved, OR the owner has
    # no Subscription row for this profile (D10 -- a convention, not a
    # constraint, so `boards/alerts.py` asserts it rather than trusting it),
    # OR `User.status != 'active'` or `digest_disabled_at IS NOT NULL` (see
    # `boards/alerts.py`). Independent of `board_alerts_enabled` in all three
    # cases: a muted owner is still reachable, so this is never true merely
    # because alerts are off. "A delivery the system knows it suppressed is
    # the one case where an in-app surface is not optional."
    owner_undeliverable: bool


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


def _to_out(board: Board, approved_count: int, owner_undeliverable: bool) -> BoardOut:
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
        owner_undeliverable=owner_undeliverable,
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


def _lock_board_row(session, board: Board) -> None:
    """`SELECT ... FOR UPDATE` on `board`'s row, refreshing its attributes
    from that locked read -- shared by every capacity decision (`update_board`,
    `approve_items`) so both take the SAME lock before counting, per MINOR 7 /
    IMPORTANT 1's fix.

    Narrow window: `board` was already confirmed to exist by `_board_for_edit`
    moments ago, but a concurrent `delete_board` could remove the row between
    that check and this lock. `session.refresh` then raises
    `ObjectDeletedError`, which this turns into the same 404 every other
    lookup in this module gives for a board that is not there, rather than an
    uncaught 500.
    """
    try:
        session.refresh(board, with_for_update=True)
    except ObjectDeletedError as exc:
        raise HTTPException(status_code=404, detail="board not found") from exc


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
        undeliverable = owner_undeliverable_map(session, [b.profile_id for b in boards])
        out = [_to_out(b, counts.get(b.id, 0), undeliverable[b.profile_id]) for b in boards]
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
        undeliverable = owner_undeliverable_map(session, [board.profile_id])[board.profile_id]
        out = _to_out(board, 0, undeliverable)
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
        undeliverable = owner_undeliverable_map(session, [board.profile_id])[board.profile_id]
        out = _to_out(board, count, undeliverable)
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

    CONCURRENCY (fix round 1 / IMPORTANT 1). This route takes the SAME
    `SELECT ... FOR UPDATE` row lock `approve_items` takes, and takes it
    BEFORE `approved_count` is read -- not only before the eventual `UPDATE`.
    Locking only at commit time (what a bare `session.commit()` gives for
    free from Postgres's own write-write blocking) is not enough: this
    route's capacity guard is a decision made from a READ, and a concurrent
    `approve_items` that lands its own approvals between this read and this
    route's `UPDATE` is invisible to a guard that already finished reading.
    Taking the lock first forces this route to read the count AFTER any
    approval that got the lock first has committed, so the guard always sees
    the true, current count. See `approve_items`'s own docstring for the
    approve-vs-approve half of this same mechanism.
    """
    sf = request.app.state.session_factory
    with sf() as session:
        board = _board_for_edit(session, board_id=board_id, user=user)
        _lock_board_row(session, board)
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
        undeliverable = owner_undeliverable_map(session, [board.profile_id])[board.profile_id]
        out = _to_out(board, approved_count, undeliverable)
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


# --- Task 6: approve, un-approve, reorder, publish --------------------------
#
# Permissions still come from the ONE place: every route below starts with
# `_board_for_edit`, same as Task 5. Publish additionally narrows to admin
# AFTER that call -- never instead of it -- the same shape delete_board (above)
# uses for its own creator-or-admin narrowing, so a non-admin's request
# against a board they cannot even READ still 404s/403s on the SAME rule
# before the admin-only refusal is ever reached.


class QueueCallOut(BaseModel):
    call_id: int
    title: str
    url: str
    deadline: datetime | None
    source_name: str | None
    score: int
    already_approved: bool


class BoardQueueOut(BaseModel):
    items: list[QueueCallOut]
    # NOT a pagination total -- board_queue's own contract (Task 4): already-
    # approved candidates are counted OUT of this, so it answers "how many
    # still need a decision", not "how many rows exist".
    waiting_count: int


class BoardItemOut(BaseModel):
    call_id: int
    title: str
    url: str
    deadline: datetime | None
    source_name: str | None
    position: int
    approved_by: int
    approved_at: datetime


class BoardItemsOut(BaseModel):
    items: list[BoardItemOut]


def _item_out(call: Call, item: BoardItem) -> BoardItemOut:
    return BoardItemOut(
        call_id=call.id,
        title=call.title,
        url=call.url,
        deadline=call.deadline,
        source_name=(call.source.name if call.source else None),
        position=item.position,
        approved_by=item.approved_by,
        approved_at=item.approved_at,
    )


def _list_items(session, board_id: int) -> list[BoardItemOut]:
    rows = session.execute(
        select(Call, BoardItem)
        .join(BoardItem, BoardItem.call_id == Call.id)
        .where(BoardItem.board_id == board_id)
        .order_by(BoardItem.position.asc())
    ).all()
    return [_item_out(call, item) for call, item in rows]


@router.get("/boards/{board_id}/queue", response_model=BoardQueueOut)
def get_board_queue(
    request: Request,
    board_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),  # noqa: B008
):
    """The admin approval checklist -- any profile editor (§4.3), not
    admin-only. A thin serialization over `board_queue` (boards/queue.py,
    Task 4), which is where D2's "the profile OWNER's hides, not the
    caller's" decision actually lives -- this route does not re-derive it."""
    sf = request.app.state.session_factory
    with sf() as session:
        board = _board_for_edit(session, board_id=board_id, user=user)
        rows, waiting = board_queue(session, board=board, limit=limit, offset=offset)
        items = [
            QueueCallOut(
                call_id=row.call.id,
                title=row.call.title,
                url=row.call.url,
                deadline=row.call.deadline,
                source_name=(row.call.source.name if row.call.source else None),
                score=row.score,
                already_approved=row.already_approved,
            )
            for row in rows
        ]
    return BoardQueueOut(items=items, waiting_count=waiting)


@router.get("/boards/{board_id}/items", response_model=BoardItemsOut)
def get_board_items(
    request: Request,
    board_id: int,
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Approved items, in `position` order. Applies NO decision filtering --
    D3: an owner's later hide of an already-approved call must not make it
    vanish here; un-approving (DELETE below) is the only way a card comes
    down once it is up."""
    sf = request.app.state.session_factory
    with sf() as session:
        _board_for_edit(session, board_id=board_id, user=user)
        items = _list_items(session, board_id)
    return BoardItemsOut(items=items)


class ApproveIn(BaseModel):
    """Bulk approve: EITHER an explicit checklist selection OR a score floor,
    never both and never neither -- §4.3's two bulk affordances ("a checklist"
    and "approve everything at or above N") are different requests, not two
    optional filters on the same one."""

    call_ids: list[int] | None = None
    min_score: int | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _exactly_one(self) -> ApproveIn:
        if (self.call_ids is None) == (self.min_score is None):
            raise ValueError("send exactly one of call_ids or min_score")
        return self


def _candidates_at_or_above(
    session, board: Board, min_score: int, *, page_size: int = 200
) -> list[int]:
    """Every WAITING call_id in `board`'s queue scoring >= min_score.

    Paged through `board_queue` rather than one big `limit=` (which would
    silently truncate the very thing §6 forbids truncating) -- but not
    forced to walk the whole queue either: rows come back
    `ORDER BY aggregate_score DESC`, so the scan can stop at the first row
    below the floor.
    """
    call_ids: list[int] = []
    offset = 0
    while True:
        rows, _waiting = board_queue(session, board=board, limit=page_size, offset=offset)
        if not rows:
            return call_ids
        for row in rows:
            if row.score < min_score:
                return call_ids
            if not row.already_approved:
                call_ids.append(row.call.id)
        if len(rows) < page_size:
            return call_ids
        offset += page_size


@router.post("/boards/{board_id}/items", response_model=BoardItemsOut)
def approve_items(
    request: Request,
    board_id: int,
    body: ApproveIn,
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Bulk approve -- §4.3's checklist and "approve everything at or above N"
    in one route; a per-call POST costs a minute per call and an abandoned
    gate is worse than no gate.

    CAPACITY. Enforced here too, not only on the settings PUT's flip guard
    (Task 5): the cap is the STORED `items_per_page * max_pages` (D7), and a
    bulk request that would exceed it approves NONE. A partial approve is
    exactly the silent truncation spec §6 forbids, just performed by the
    server instead of the display.

    CANDIDATE-SET BYPASS (deliberate -- MINOR 5, not changed here).
    `call_ids` is validated only against `Call` existing at all (below) --
    NEVER against this board's queue candidate set. A call with no
    `CallRanking` row for this profile, one scoring below `board.min_score`,
    or one already past its deadline can all still be approved directly by
    id: none of `board_queue`'s filters (boards/queue.py) run on this path.
    D3 treats approval and the queue as deliberately different states, so
    this is a product decision, not a bug the plan asked to close. The
    consequence is real, though: an expired call approved this way consumes
    a cap slot indefinitely -- `BoardItem`'s own docstring notes "the only
    automatic removal is the deadline filter" (db/models/boards.py), which is
    the QUEUE's filter, not this route's -- yet it never renders, because the
    public board only shows non-expired items. See
    `test_approving_an_id_outside_the_queues_candidate_set_is_allowed`, which
    pins this behaviour so a later change here is deliberate.

    CONCURRENCY (Task 5 review note carried forward; corrected in fix round
    1 -- see IMPORTANT 1). This route re-reads `board` under
    `SELECT ... FOR UPDATE` (`_lock_board_row`) BEFORE counting anything the
    capacity decision depends on, so a second concurrent approve on the SAME
    board blocks here until the first commits, then counts what the first
    actually landed -- closing approve-vs-approve.

    `update_board`'s settings PUT now takes the SAME lock, in the SAME
    position (before its own `_approved_counts` read, not only before its
    `UPDATE`), which is what actually closes the approve-vs-settings-PUT
    race too. Plain write-write blocking on the two routes' eventual
    `UPDATE`s -- the mechanism this docstring used to (wrongly) credit -- does
    NOT close that race: `update_board`'s guard is a decision made from a
    READ that happens well before its `UPDATE`, so a concurrent approve that
    commits in that window is invisible to a guard that already finished
    reading, no matter what the later `UPDATE`s contend on. Locking the row
    up front, on BOTH routes, is what forces the second locker's read to
    happen after the first's commit. Different boards do not contend -- the
    lock is per-row.
    """
    sf = request.app.state.session_factory
    with sf() as session:
        board = _board_for_edit(session, board_id=board_id, user=user)
        _lock_board_row(session, board)

        if body.call_ids is not None:
            requested = list(dict.fromkeys(body.call_ids))  # de-dup, keep order
            if requested:
                found_ids = set(
                    session.execute(select(Call.id).where(Call.id.in_(requested))).scalars()
                )
                unknown = [cid for cid in requested if cid not in found_ids]
                if unknown:
                    raise HTTPException(status_code=422, detail=f"unknown call_id(s): {unknown}")
        else:
            requested = _candidates_at_or_above(session, board, body.min_score)

        already_approved_ids = set(
            session.execute(
                select(BoardItem.call_id).where(BoardItem.board_id == board.id)
            ).scalars()
        )
        new_ids = [cid for cid in requested if cid not in already_approved_ids]

        approved_count = len(already_approved_ids)
        cap = board.items_per_page * board.max_pages
        # MINOR 6: an empty selection changes nothing, so it must succeed
        # even on a board already at (or past) capacity -- `if new_ids` skips
        # a refusal that would otherwise fire on `approved_count + 0 > cap`.
        if new_ids and approved_count + len(new_ids) > cap:
            overflow = approved_count + len(new_ids) - cap
            raise HTTPException(
                status_code=409,
                detail=(
                    f"approving {len(new_ids)} more would put "
                    f"{approved_count + len(new_ids)} on a board that holds {cap} — "
                    f"drop {overflow} from this selection, or remove {overflow} "
                    "already-approved first"
                ),
            )

        if new_ids:
            existing_max = session.scalar(
                select(func.max(BoardItem.position)).where(BoardItem.board_id == board.id)
            )
            next_position = 0 if existing_max is None else existing_max + 1
            urls = dict(
                session.execute(
                    select(Call.id, Call.url).where(Call.id.in_(new_ids))
                ).all()
            )
            # THE ENCODE NOW RUNS INSIDE THIS SESSION AND UNDER THE ROW LOCK,
            # and that reverses -- for this route only -- the rule
            # public_boards.py:105-115 states as CRITICAL for the ANONYMOUS
            # route. Named here rather than discovered later.
            #
            # What it costs, from qr.py's own measured numbers: the batch
            # ceiling is `cap` above, items_per_page * max_pages. items_per_page
            # is bounded 1..64 (ck_boards_items_per_page) and max_pages is not
            # exposed on BoardSettingsIn at all, so the API-reachable worst case
            # is 192 items x ~45.5 ms = ~8.7 s holding both a pooled connection
            # and this board's `SELECT ... FOR UPDATE` -- the lock this route's
            # docstring makes the linchpin of approve-vs-approve and
            # approve-vs-settings-PUT.
            #
            # Why that is the accepted trade and not an oversight: this is an
            # authenticated, rate-limited admin action on ONE board, bounded and
            # rare, and it is what buys the anonymous path -- unauthenticated,
            # unbounded in callers -- its zero-encode guarantee. Moving the
            # encode outside the transaction (read urls, commit, encode, store
            # in a second transaction) would remove the lock hold at the cost of
            # a window in which an approved board serves no QR at all.
            # That restructuring is deliberately NOT done here; it is recorded
            # for review.
            for offset, cid in enumerate(new_ids):
                url = urls.get(cid) or ""
                # Skip the encoder for an empty url. Call.url is Text NOT NULL
                # but permits "", and qr_for("") returns a real 21-module QR of
                # the empty string -- ~1.5 KB stored per row for a code that can
                # never be served, since qr_source_url is then NULL and the
                # public route's `qr_source_url == call.url` comparison is
                # permanently False. The outcome was already correct; this makes
                # it deliberate and skips the wasted work.
                qr = qr_for(url) if url else None
                session.add(
                    BoardItem(
                        board_id=board.id,
                        call_id=cid,
                        position=next_position + offset,
                        approved_by=user.id,
                        qr_path=(qr.path or None) if qr else None,
                        qr_modules=qr.modules if qr else 0,
                        qr_source_url=url or None,
                    )
                )
            session.commit()

        items = _list_items(session, board.id)
    return BoardItemsOut(items=items)


class ReorderIn(BaseModel):
    call_ids: list[int]


@router.put("/boards/{board_id}/items", response_model=BoardItemsOut)
def reorder_items(
    request: Request,
    board_id: int,
    body: ReorderIn,
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Rewrite every approved item's `position` from `call_ids`' order.
    All-or-nothing: an unknown call_id, a missing one, or a duplicate refuses
    the WHOLE request (validated before any UPDATE is issued) rather than
    applying a partial reorder."""
    sf = request.app.state.session_factory
    with sf() as session:
        board = _board_for_edit(session, board_id=board_id, user=user)

        if len(set(body.call_ids)) != len(body.call_ids):
            raise HTTPException(status_code=422, detail="duplicate call_id in reorder")

        existing_ids = set(
            session.execute(
                select(BoardItem.call_id).where(BoardItem.board_id == board.id)
            ).scalars()
        )
        provided_ids = set(body.call_ids)
        unknown = sorted(provided_ids - existing_ids)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"unknown call_id(s), not approved on this board: {unknown}",
            )
        missing = sorted(existing_ids - provided_ids)
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"reorder must include every approved call_id; missing: {missing}",
            )

        for position, call_id in enumerate(body.call_ids):
            session.execute(
                update(BoardItem)
                .where(BoardItem.board_id == board.id, BoardItem.call_id == call_id)
                .values(position=position)
            )
        session.commit()

        items = _list_items(session, board.id)
    return BoardItemsOut(items=items)


@router.delete("/boards/{board_id}/items/{call_id}", status_code=204)
def unapprove_item(
    request: Request,
    board_id: int,
    call_id: int,
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Un-approve: any profile editor, NOT admin-only (§4.3). D3: the owner
    can take a card down deliberately, on the board -- independent of the
    per-user CallDecision hide/save state, which this never touches and which
    never reaches here."""
    sf = request.app.state.session_factory
    with sf() as session:
        board = _board_for_edit(session, board_id=board_id, user=user)
        item = session.get(BoardItem, (board.id, call_id))
        if item is None:
            raise HTTPException(status_code=404, detail="item not found on this board")
        session.delete(item)
        session.commit()
    return Response(status_code=204)


class PublishIn(BaseModel):
    is_published: bool


@router.put("/boards/{board_id}/publish", response_model=BoardOut)
def publish_board(
    request: Request,
    board_id: int,
    body: PublishIn,
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Admin-only, deliberately its OWN route rather than a field on
    BoardSettingsIn -- §4.3: "I picked these calls" and "I put them on a
    public wall" are different claims, and a field on the editor-accessible
    settings route would make the second reachable by whoever can make the
    first.

    The admin check sits AFTER `_board_for_edit`, never instead of it -- the
    same shape delete_board uses for its own (looser) creator-or-admin
    narrowing -- so a non-admin's request against a board they cannot even
    edit still gets THAT refusal, on the one permission rule this module has,
    before the admin-only refusal is reached.

    Un-publishing is admin-only too (§4.3: "(and un-publish)") and
    deliberately does NOT clear `published_by`/`published_at`: they record
    who last made the board public and when, a question that survives the
    board coming back down.
    """
    sf = request.app.state.session_factory
    with sf() as session:
        board = _board_for_edit(session, board_id=board_id, user=user)
        if not user.is_admin:
            raise HTTPException(
                status_code=403,
                detail="only an admin may publish or unpublish a board",
            )
        board.is_published = body.is_published
        if body.is_published:
            board.published_by = user.id
            board.published_at = datetime.now(UTC)
        approved_count = _approved_counts(session, [board.id]).get(board.id, 0)
        session.commit()
        session.refresh(board)
        undeliverable = owner_undeliverable_map(session, [board.profile_id])[board.profile_id]
        out = _to_out(board, approved_count, undeliverable)
    return out
