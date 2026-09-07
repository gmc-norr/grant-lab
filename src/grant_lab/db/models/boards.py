"""The corridor board and its approved queue.

MATCH-edition tables. Nothing here may import from grant_lab.proposal_assist --
tests/architecture/test_match_edition_process_cut.py boots a match process in a
fresh interpreter and asserts that CONSTRUCTING the app (create_app()) imports
zero proposal_assist modules. It measures construction, not boot: the lifespan
that runs afterwards imports seven of them unguarded (main.py:212, 229-233, 251).
Everything in this plan is reachable from create_app(), so the gate does bite
here -- but do not restate it as "a match boot imports none".

`orientation` is STORED rather than inferred from the viewport. Spec §6 said the
renderer picks it from the aspect ratio; the 2026-09-05 decisions note (D7)
overrides that, because §6 also caps approval at `items_per_page * max_pages`
and forbids "the display silently truncating a list an admin approved". If the
renderer decides orientation, the cap is unknowable at approval time. See the
plan's "Deviation 2".
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from grant_lab.db.base import Base

# Landscape 1920x1080 is a 2x4 grid of 8; portrait 1080x1920 is 1x4 of 4 (spec
# §6). `items_per_page` remains an override for a screen that measures
# differently, so this is a DEFAULT, not the value.
ORIENTATION_ITEMS_PER_PAGE = {"landscape": 8, "portrait": 4}


class Board(Base):
    __tablename__ = "boards"
    __table_args__ = (
        CheckConstraint("orientation IN ('landscape','portrait')", name="ck_boards_orientation"),
        # >= 16 chars, per §4.3. Enforced in the DB so a hand-written INSERT
        # cannot create a guessable public URL.
        CheckConstraint("char_length(slug) >= 16", name="ck_boards_slug_length"),
        # 1-64: originally "items_per_page > 0" (migration 0072); migration
        # 0074 dropped and recreated it as a range in the same commit that
        # added the ceiling on BoardSettingsIn.items_per_page (web/api/
        # boards.py) -- 64 is roughly eight times spec §6's largest real grid
        # (landscape, 8), a guard-rail against nonsense rather than a product
        # limit, and far below what int4 can hold.
        CheckConstraint("items_per_page BETWEEN 1 AND 64", name="ck_boards_items_per_page"),
        CheckConstraint("max_pages > 0", name="ck_boards_max_pages"),
        # Matches CallRanking.aggregate_score's own 0-100 scale
        # (ranking/aggregate.py: "gates + LLM rubric -> 0-100 score") --
        # `min_score` is compared directly against that column in
        # boards/queue.py. Migration 0074; the API's own bound is
        # BoardSettingsIn.min_score (web/api/boards.py) -- this is the floor,
        # not a restatement of it.
        CheckConstraint("min_score BETWEEN 0 AND 100", name="ck_boards_min_score_range"),
        # 1-3600 (one hour). Migration 0074, alongside ck_boards_min_score_
        # range. Task 10's rotation timer divides by this; 0 or negative had
        # no DB backstop before this constraint and returned 200 from PUT. The
        # ceiling is a guard-rail, not a product limit: an hour per corridor
        # page is already absurd.
        CheckConstraint("page_seconds BETWEEN 1 AND 3600", name="ck_boards_page_seconds"),
        Index("ix_boards_profile_id", "profile_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # RESTRICT, per §4.3: deleting a profile must not silently blank a wall.
    profile_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False
    )
    is_published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    min_score: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default=text("60")
    )
    orientation: Mapped[str] = mapped_column(
        String(16), nullable=False, default="landscape", server_default=text("'landscape'")
    )
    items_per_page: Mapped[int] = mapped_column(
        Integer, nullable=False, default=8, server_default=text("8")
    )
    max_pages: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default=text("3")
    )
    page_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default=text("30")
    )
    # Off by default: every board that exists when 0078 lands keeps the
    # approval gate. Turning this on replaces the board's hand-picked items on
    # the next 05:00 run -- see the spec's "Flipping the switch".
    auto_publish: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    published_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # onupdate: SQLAlchemy applies this client-side in the UPDATE statement
    # itself (no DDL, no trigger) -- matches the other six updated_at columns
    # in this repo (profiles.py, workspaces.py, ranking.py x2, jobs.py x2).
    # Without it, update_board (web/api/boards.py) never advanced this column
    # and BoardOut.updated_at stayed permanently equal to created_at even
    # after a settings change -- Task 5 is this column's only writer and only
    # reader, so it is Task 5's to fix.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class BoardItem(Base):
    """One approved call on one board.

    Everything DISPLAYED is joined live from `calls` -- D8: the public query
    joins `calls` and a funder's correction reaches the wall with no renewed
    approval. A snapshot would keep showing a deadline the funder has since
    fixed, and on a hospital wall a wrong date is worse than an unreviewed
    title. D8 holds for all of it, still.

    The one stored copy is `qr_source_url`, a snapshot of `Call.url` as it was
    at approval time. It is never displayed itself -- it exists only so a
    stale QR can be WITHDRAWN: the public route compares it against the
    call's current url and serves `qr_modules=0` on a mismatch, never a QR
    pointing somewhere the call no longer lives. See `qr_path`/`qr_modules`
    below for why the QR itself is rendered once, at approval, rather than on
    the public path.

    Withdrawal is ONE-WAY, and that is the cost of rendering at approval: there
    is no re-render path. If a funder corrects a url -- or corrects it BACK --
    the card stays QR-less until an admin removes the item and re-approves it
    (DELETE /boards/{id}/items/{call_id} then POST /boards/{id}/items). Worth
    knowing before treating a blank code as a rendering bug.

    Freshness here is URL-only. `qr_for`'s output also depends on its error
    level and `_MAX_URL_BYTES` (qr.py), neither of which is recorded on the row,
    so re-parameterising the encoder does NOT withdraw QRs rendered under the
    old parameters. The claim above is about the CALL moving, not the ENCODER
    changing.

    `Call` has NO status or withdrawn column (measured), so "the board drops
    withdrawn calls" is not implementable in release 1 and must not be claimed.
    The only automatic removal is the deadline filter.
    """

    __tablename__ = "board_items"
    __table_args__ = (Index("ix_board_items_board_position", "board_id", "position"),)

    board_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("boards.id", ondelete="CASCADE"), primary_key=True
    )
    call_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("calls.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    # NULL means published automatically by boards/auto_publish.py. Inventing a
    # system user to keep this NOT NULL would make the audit trail lie about
    # who decided. The value survives a board returning to manual mode.
    approved_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # The QR, rendered ONCE at approval by an admin in an authenticated,
    # rate-limited request -- not on the anonymous /b/:slug path, where
    # qr.py's per-URL global _encode_lock made two alternating max-shape
    # boards a cache-thrash amplifier.
    qr_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 0 means "no QR" -- the signal the frontend already honours
    # (qr_modules === 0 => no <svg>).
    # `default=0` as well as `server_default`, matching max_pages at :96-97:
    # without it a BoardItem(...) built without the kwarg reads back None until
    # flush, and five test files build one directly.
    qr_modules: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # The Call.url this QR was rendered from. NOT bookkeeping: a stored QR is
    # derived data with the same exists-versus-current problem the freshness
    # release fixed for embeddings, and a QR on a corridor wall pointing where
    # the call no longer lives is worse than no QR. The public route compares
    # this and serves qr_modules=0 on a mismatch.
    qr_source_url: Mapped[str | None] = mapped_column(Text, nullable=True)


class BoardSuppression(Base):
    """One call an operator took off ONE board.

    This is an EXCEPTION list, not a second approval queue. It exists because
    an auto board rebuilds itself at 05:00, so deleting a row is not a
    takedown on its own -- the next run would put it back. It is written by
    `unapprove_item` and cleared by `approve_items`, which is a newer and
    stronger signal from the same human.

    Scoped to one board on purpose. Archiving a call reaches every board on
    that profile AND the owner's dashboard, and takes effect only at the next
    run; both are wrong for "this one card should not be on this one wall".
    """

    __tablename__ = "board_suppressions"

    board_id: Mapped[int] = mapped_column(
        ForeignKey("boards.id", ondelete="CASCADE"), primary_key=True
    )
    call_id: Mapped[int] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), primary_key=True
    )
    suppressed_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    suppressed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
