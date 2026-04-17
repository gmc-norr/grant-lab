from datetime import UTC, datetime

import pytest

from call_monitor.auth.tokens import (
    TokenAlreadyConsumed,
    TokenExpired,
    TokenNotFound,
    consume_token,
    hash_token,
    issue_token,
)
from call_monitor.db.models import MagicLinkToken, User


def test_hash_token_is_sha256_hex(tmp_path):
    h = hash_token("hello")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)
    assert h == hash_token("hello")
    assert h != hash_token("world")


def test_issue_token_returns_raw_and_stores_hash(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)

        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()
        assert len(raw) >= 32

        row = s.query(MagicLinkToken).filter_by(user_id=u.id).one()
        assert row.token_hash == hash_token(raw)
        assert row.consumed_at is None
        assert row.expires_at > datetime.now(UTC)


def test_consume_token_marks_consumed_and_returns_user_id(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()

        user_id = consume_token(s, raw)
        s.commit()
        assert user_id == u.id

        row = s.query(MagicLinkToken).filter_by(user_id=u.id).one()
        assert row.consumed_at is not None


def test_consume_token_rejects_replay(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()
        consume_token(s, raw)
        s.commit()

        with pytest.raises(TokenAlreadyConsumed):
            consume_token(s, raw)


def test_consume_token_rejects_expired(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        raw = issue_token(s, user_id=u.id, ttl_minutes=-1)  # already expired
        s.commit()
        with pytest.raises(TokenExpired):
            consume_token(s, raw)


def test_consume_token_rejects_unknown(pg_session_factory):
    with pg_session_factory() as s, pytest.raises(TokenNotFound):
        consume_token(s, "does-not-exist-at-all-12345678")
