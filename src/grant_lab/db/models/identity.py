"""The person. Every other owner column points here."""

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


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Defense in depth: the application always lowercases before insert,
        # but Postgres `UNIQUE(email)` is case-sensitive, so a rogue SQL
        # admin task could create both `Alice@umu.se` and `alice@umu.se` and
        # split a single person across two accounts. The CHECK forces
        # lowercase at the DB level — combined with UNIQUE(email), this
        # guarantees one row per address regardless of case.
        CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),
        CheckConstraint(
            "status IN ('pending', 'active', 'disabled')",
            name="ck_users_status_valid",
        ),
        CheckConstraint(
            "(oidc_iss IS NULL) = (oidc_sub IS NULL)",
            name="ck_users_oidc_paired_null",
        ),
        Index(
            "uq_users_oidc_identity",
            "oidc_iss",
            "oidc_sub",
            unique=True,
            postgresql_where=text("oidc_sub IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Fails CLOSED: a User created without an explicit status is `pending`
    # (no access). Only the migration backfill and admin bootstrap make a
    # user `active`.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )
    oidc_iss: Mapped[str | None] = mapped_column(Text, nullable=True)
    oidc_sub: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    digest_bounce_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    digest_disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Monotonic counter embedded in the signed session cookie. Bumping it
    # (on logout or admin revocation) invalidates every outstanding cookie
    # for this user, since sessions are otherwise stateless signed tokens.
    session_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # Set during first-run value loop: the profile the user most recently
    # bootstrapped or selected as their default. SET NULL on profile delete.
    default_profile_id: Mapped[int | None] = mapped_column(
        BigInteger,
        # use_alter: `users` and `profiles` reference each other (this column,
        # and profiles.created_by -> users.id), so SQLAlchemy cannot order the
        # two for whole-metadata DDL. Deferring THIS side to a post-create
        # ALTER breaks the cycle; it has to be this side because it is the
        # nullable one — profiles.created_by is NOT NULL and cannot wait.
        # The name matches alembic 0046's create_foreign_key, so the model and
        # the database agree and no migration is needed.
        ForeignKey(
            "profiles.id",
            ondelete="SET NULL",
            name="fk_users_default_profile_id",
            use_alter=True,
        ),
        nullable=True,
    )
