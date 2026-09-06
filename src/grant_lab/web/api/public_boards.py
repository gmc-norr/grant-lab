"""The one unauthenticated route: the corridor board's public JSON.

MATCH-edition module. Nothing here may import from grant_lab.proposal_assist --
tests/architecture/test_match_edition_process_cut.py boots a match process in a
fresh interpreter and asserts CONSTRUCTING the app (create_app()) imports zero
proposal_assist modules. This router is mounted in `_ALWAYS`, so it is eagerly
imported by the web/api package `__init__` in every process.

DIFFERENT MODULE FROM `web/api/boards.py` ON PURPOSE. That module's routes are
all keyed on `board_id` and gated by `_board_for_edit`
(tests/architecture/test_boards_permission_single_source.py pins its exact
route set); this one is keyed on `slug` and carries no auth dependency at
all, so it deliberately sits outside that gate -- see the gate's own
docstring. Putting it in a separate module makes "this is the public surface"
visible in a directory listing, not just in a route path.

THE PAYLOAD IS A CLOSED LIST (spec §6: "exactly the card fields and nothing
else"). Deliberately absent: score, LLM rationale, criterion chips,
`quoted_evidence`, `applicant_notes`, any profile identity beyond the board's
own title, `call_id`, `profile_id` -- nothing that lets a scraper join back to
anything else in this app.

UNPUBLISHED -> 404, INDISTINGUISHABLE FROM NONEXISTENT (§6). Task 5/6's
`BoardOut.slug` is already visible to every org reader via `GET /api/boards`
for any profile they can read, so an unpublished board's slug is not a
secret today -- this route is the only thing standing between that and a
public page, and it must give the exact same body for "not published" and
"no such slug", from the exact same `raise`, or it becomes a slug oracle.

RATE LIMITED: 60 requests / 15 minutes / IP, via the same `SlidingWindowLimiter`
`app.state.email_limiter` and `app.state.ip_limiter` use (main.py). Sixty is
20x headroom over one legitimate screen's expected request rate (a 15-minute
window over a ~5-minute client-side cache is ~3 requests) while still bounding
a scraper that has no session to rate-limit any other way.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from grant_lab.boards.public import public_board_by_slug, public_board_items
from grant_lab.web.client_ip import client_ip
from grant_lab.web.filters import deadline_local_date

router = APIRouter(tags=["public-boards"])

# The ONE 404 message for both "unpublished" and "nonexistent slug" -- a
# second, differently-worded 404 would let a client tell the two apart and
# turn this route into a slug oracle over BoardOut.slug's existing exposure.
_NOT_FOUND = "board not found"


class PublicBoardItemOut(BaseModel):
    title: str
    source_name: str
    # MINOR 5: typed Optional to match the shared `deadline_local_date()`
    # helper's own signature (`web/filters.py:37`, also `str | None` because
    # it also backs `CallOut`, where a deadline CAN be unset) -- not because
    # this field can actually be null here. `public.py`'s
    # `Call.deadline.is_not(None)` filter means every row this endpoint
    # emits already has a deadline, so this branch never fires in practice.
    deadline_local_date: str | None
    budget_min: int | None
    budget_max: int | None
    currency: str | None
    url: str
    # A bare `#` comment here is invisible to `openapi-fetch`'s generated
    # types (fix round 3): `frontend/src/api/schema.gen.ts` is built from
    # the OpenAPI schema, not from this source file, so a React caller
    # typing `item.qr_modules` saw a bare `number` with no hint that 0 is a
    # sentinel. `Field(description=...)` propagates into the schema (and
    # from there into the generated type's doc comment) where a `#` comment
    # cannot.
    qr_modules: int = Field(
        description=(
            "Matrix width in modules, EXCLUDING the quiet zone. 0 means NO QR "
            "was produced -- the url exceeded a measured byte-cost bound (see "
            "`qr_for`'s `_MAX_URL_BYTES`), or in principle segno's own overflow "
            "ceiling -- and the caller should render no `<svg>` for this card. "
            "Unambiguous: the smallest real QR is 21 modules."
        )
    )
    qr_path: str


class PublicBoardOut(BaseModel):
    title: str
    generated_at: datetime
    orientation: Literal["landscape", "portrait"]
    items_per_page: int
    max_pages: int
    page_seconds: int
    items: list[PublicBoardItemOut]


@router.get("/public/boards/{slug}", response_model=PublicBoardOut)
def get_public_board(request: Request, slug: str):
    if not request.app.state.board_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests")
    sf = request.app.state.session_factory
    # CRITICAL (fix round 2): the DB session closes here, BEFORE any QR would
    # be encoded. Since migration 0077 this route encodes NOTHING at all -- it
    # reads the QR stored at approval (boards.py's approve_items) -- so the rule
    # below is now about what must never come BACK, not about live behaviour
    # here. It is also no longer global: approve_items deliberately encodes
    # inside its session and under its row lock, which is sound there for
    # reasons named at that call site and unsound here, because this route is
    # anonymous and unbounded in callers.
    #
    # `rows` is already a `list[PublicBoardRow]` of plain scalars
    # (boards/public.py), not ORM objects, and `board_fields` below copies
    # out the handful of ORM attributes this route still needs -- so nothing
    # past this block touches the session. Round 1 bounded `qr_for`'s
    # exception; the encode loop itself used to run INSIDE `with sf() as
    # session:`, holding one of the pool's 30 connections
    # (`pool_size=10, max_overflow=20`, `db/session.py`) for the whole
    # (synchronous, CPU-bound) loop -- shared with every authenticated
    # route. Encoding needs no connection, so it happens after the `with`
    # exits instead.
    with sf() as session:
        board = public_board_by_slug(session, slug=slug)
        if board is None or not board.is_published:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        rows = public_board_items(session, board_id=board.id)
        board_fields = {
            "title": board.title,
            "orientation": board.orientation,
            "items_per_page": board.items_per_page,
            "max_pages": board.max_pages,
            "page_seconds": board.page_seconds,
        }

    items = []
    for row in rows:
        items.append(
            PublicBoardItemOut(
                title=row.title,
                source_name=row.source_name,
                deadline_local_date=deadline_local_date(row.deadline),
                budget_min=row.budget_min,
                budget_max=row.budget_max,
                currency=row.currency,
                url=row.url,
                qr_modules=row.qr_modules,
                qr_path=row.qr_path,
            )
        )
    return PublicBoardOut(
        generated_at=datetime.now(UTC),
        items=items,
        **board_fields,
    )
