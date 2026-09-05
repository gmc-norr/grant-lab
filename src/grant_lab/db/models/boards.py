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
        CheckConstraint(
            "orientation IN ('landscape','portrait')", name="ck_boards_orientation"
        ),
        # >= 16 chars, per §4.3. Enforced in the DB so a hand-written INSERT
        # cannot create a guessable public URL.
        CheckConstraint("char_length(slug) >= 16", name="ck_boards_slug_length"),
        CheckConstraint("items_per_page > 0", name="ck_boards_items_per_page"),
        CheckConstraint("max_pages > 0", name="ck_boards_max_pages"),
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
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    published_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BoardItem(Base):
    """One approved call on one board.

    Stores a `call_id` and nothing about the call. D8: the board shows LIVE call
    data, so the public query joins `calls` and a funder's correction reaches the
    wall with no renewed approval. A snapshot would keep showing a deadline the
    funder has since fixed, and on a hospital wall a wrong date is worse than an
    unreviewed title.

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
    approved_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
