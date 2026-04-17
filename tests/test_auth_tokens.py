from datetime import UTC, datetime

import pytest

from call_monitor.auth.tokens import (
    InvalidToken,
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
    """Replay (already-consumed token) raises InvalidToken — indistinguishable
    from unknown/expired to the caller (no oracle leak)."""
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()
        consume_token(s, raw)
        s.commit()

        with pytest.raises(InvalidToken):
            consume_token(s, raw)


def test_consume_token_rejects_expired(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        raw = issue_token(s, user_id=u.id, ttl_minutes=-1)  # already expired
        s.commit()
        with pytest.raises(InvalidToken):
            consume_token(s, raw)


def test_consume_token_rejects_unknown(pg_session_factory):
    with pg_session_factory() as s, pytest.raises(InvalidToken):
        consume_token(s, "does-not-exist-at-all-12345678")


def test_consume_token_emits_exactly_one_sql_statement_on_failure(pg_session_factory):
    """Regression guard: consume_token must NOT classify failure reasons
    by running a second SELECT. That would leak a timing oracle
    distinguishing 'token exists (expired/consumed)' from 'token unknown'.
    """
    from sqlalchemy import event

    with pg_session_factory() as s:
        statements: list[str] = []
        engine = s.get_bind()

        @event.listens_for(engine, "before_cursor_execute")
        def _capture(conn, cursor, statement, parameters, context, executemany):
            # Filter to just the token-table statements this function emits.
            if "magic_link_tokens" in statement.lower():
                statements.append(statement)

        try:
            with pytest.raises(InvalidToken):
                consume_token(s, "does-not-exist-at-all-12345678")
        finally:
            event.remove(engine, "before_cursor_execute", _capture)

    # Exactly one UPDATE — no follow-up SELECT.
    assert len(statements) == 1, f"expected 1 stmt, got {len(statements)}: {statements}"
    assert statements[0].strip().lower().startswith("update")
