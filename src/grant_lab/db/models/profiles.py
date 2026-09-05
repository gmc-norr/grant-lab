"""A research profile, who may read it, and who is subscribed to it."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from grant_lab.db.base import Base


class ProfileKind(enum.StrEnum):
    ORGANIZATIONAL_UNIT = "organizational_unit"
    INDIVIDUAL = "individual"


class ProfileLevel(enum.StrEnum):
    LAB = "lab"
    GROUP = "group"
    DEPARTMENT = "department"
    CLINIC = "clinic"
    INSTITUTION = "institution"
    OTHER = "other"


class Profile(Base):
    __tablename__ = "profiles"
    __table_args__ = (
        # Partial UNIQUE on active names. Closes the race in the handler
        # that pre-checks, releases the session, then inserts. Archived
        # profiles (is_active=false) can reuse the name — that's why it's
        # partial. See alembic/versions/0009_*.
        Index(
            "uq_profiles_active_name",
            "name",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
        # Mirror migration 0007's embedding constraints/indexes in the ORM so
        # the model stays the source of truth (autogenerate parity).
        CheckConstraint(
            "(embedding IS NULL) = (embedding_model IS NULL)",
            name="ck_profiles_embedding_model_paired",
        ),
        Index(
            "ix_profiles_embedding_null",
            "id",
            postgresql_where=text("embedding IS NULL"),
        ),
        CheckConstraint(
            "kind IN ('organizational_unit','individual')",
            name="ck_profiles_kind",
        ),
        CheckConstraint(
            "level IS NULL OR level IN ('lab','group','department','clinic','institution','other')",
            name="ck_profiles_level",
        ),
        CheckConstraint(
            "visibility IN ('private','org')",
            name="ck_profiles_visibility",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_profile_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    structured: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    # ISO 4217. NULL = unknown — v2's budget gate treats NULL as no penalty.
    budget_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(1024),
        nullable=True,
    )
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 'org'  -- every active user may read it. The DEFAULT, so migration 0070 is
    #           a no-op for existing rows and nobody's Monday changes.
    # 'private' -- owner, admins and explicit profile_viewers rows only.
    # String + CHECK rather than a native enum: matches ck_profiles_kind two
    # constraints up and ck_ranking_remote_attempts_outcome, and avoids ALTER
    # TYPE if a third value ever appears.
    visibility: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=text("'org'")
    )


class ProfileViewer(Base):
    """A user the profile's owner has explicitly let read it.

    Deliberately identical in shape to workspace_viewers (migration 0015): same
    composite PK, same CASCADEs, same added_at. The read predicate in
    web/profile_access.py copies workspaces.py's for the same reason -- spec 4.3
    requires that no second permission model appears, and boards are built on
    the profile rule.

    Absence of a row is not "no access": visibility='org' and is_admin both grant
    reading without one, and the OWNER never has a row. See readable_profile_ids().
    """

    __tablename__ = "profile_viewers"

    profile_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("profiles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True
    )
    digest_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProfileBootstrapJob(Base):
    __tablename__ = "profile_bootstrap_jobs"
    __table_args__ = (
        UniqueConstraint("profile_id", name="uq_profile_bootstrap_jobs_profile"),
        CheckConstraint(
            "status IN ('queued','running','done','failed')",
            name="ck_profile_bootstrap_jobs_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # CLEARED on terminal completion
    seed: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    pubmed_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    publications_fetched: Mapped[int | None] = mapped_column(Integer, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
