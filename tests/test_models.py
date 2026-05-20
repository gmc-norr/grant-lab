from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import MagicLinkToken, Profile, ProfileKind, User


def test_can_insert_and_read_user(pg_session_factory):
    SessionLocal = pg_session_factory
    with SessionLocal() as s:
        u = User(email="alice@ki.se", name="Alice", is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        assert u.id is not None
        assert u.created_at is not None


def test_user_email_is_unique(pg_session_factory):
    SessionLocal = pg_session_factory
    with SessionLocal() as s:
        s.add(User(email="bob@ki.se"))
        s.commit()
    with SessionLocal() as s, pytest.raises(IntegrityError):
        s.add(User(email="bob@ki.se"))
        s.commit()


def test_user_email_check_rejects_mixed_case(pg_session_factory):
    """The CHECK constraint forces lowercase storage so UNIQUE(email) is
    case-insensitive in practice. Inserting a non-lowercased email must fail."""
    SessionLocal = pg_session_factory
    with SessionLocal() as s, pytest.raises(IntegrityError):
        s.add(User(email="Alice@ki.se"))
        s.commit()


def test_active_profile_name_is_unique_at_db_level(pg_session_factory):
    """Partial unique index on (name) WHERE is_active closes the TOCTOU
    window between the handler's pre-check and the INSERT. Two active
    profiles with the same name must be rejected at the DB level."""
    SessionLocal = pg_session_factory
    from sqlalchemy import select

    with SessionLocal() as s:
        u = User(email="owner@ki.se")
        s.add(u)
        s.flush()
        s.add(
            Profile(
                name="dup", kind=ProfileKind.GROUP, created_by=u.id, description="", structured={}
            )
        )
        s.commit()

    with SessionLocal() as s, pytest.raises(IntegrityError):
        u = s.execute(select(User).where(User.email == "owner@ki.se")).scalar_one()
        s.add(
            Profile(
                name="dup", kind=ProfileKind.GROUP, created_by=u.id, description="", structured={}
            )
        )
        s.commit()


def test_inactive_profile_reuses_name(pg_session_factory):
    """The partial index only covers `is_active = true`, so an archived
    profile (is_active=false) doesn't block reuse of the name."""
    from sqlalchemy import select

    SessionLocal = pg_session_factory
    with SessionLocal() as s:
        u = User(email="owner2@ki.se")
        s.add(u)
        s.flush()
        old = Profile(
            name="shared",
            kind=ProfileKind.GROUP,
            created_by=u.id,
            description="",
            structured={},
            is_active=False,
        )
        s.add(old)
        s.commit()

    with SessionLocal() as s:
        u = s.execute(select(User).where(User.email == "owner2@ki.se")).scalar_one()
        s.add(
            Profile(
                name="shared",
                kind=ProfileKind.GROUP,
                created_by=u.id,
                description="",
                structured={},
                is_active=True,
            )
        )
        s.commit()  # must not raise


def test_magic_link_token_row(pg_session_factory):
    SessionLocal = pg_session_factory
    with SessionLocal() as s:
        u = User(email="carol@ki.se")
        s.add(u)
        s.flush()
        now = datetime.now(UTC)
        t = MagicLinkToken(
            user_id=u.id,
            token_hash="a" * 64,
            created_at=now,
            expires_at=now + timedelta(minutes=15),
        )
        s.add(t)
        s.commit()
        s.refresh(t)
        assert t.id is not None
        assert t.consumed_at is None
