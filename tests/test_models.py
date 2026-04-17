from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from call_monitor.db.models import MagicLinkToken, User


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
