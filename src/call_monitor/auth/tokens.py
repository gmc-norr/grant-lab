from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from call_monitor.db.models import MagicLinkToken


class TokenNotFound(Exception):
    pass


class TokenExpired(Exception):
    pass


class TokenAlreadyConsumed(Exception):
    pass


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_token(session: Session, *, user_id: int, ttl_minutes: int = 15) -> str:
    raw = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    row = MagicLinkToken(
        user_id=user_id,
        token_hash=hash_token(raw),
        created_at=now,
        expires_at=now + timedelta(minutes=ttl_minutes),
    )
    session.add(row)
    return raw


def consume_token(session: Session, raw: str) -> int:
    """Atomically mark the token consumed and return the owning user_id.

    Raises TokenNotFound / TokenExpired / TokenAlreadyConsumed.
    """
    token_hash = hash_token(raw)
    now = datetime.now(UTC)

    # Atomic single-use: UPDATE ... WHERE consumed_at IS NULL AND expires_at > now()
    result = session.execute(
        text(
            """
            UPDATE magic_link_tokens
               SET consumed_at = :now
             WHERE token_hash = :token_hash
               AND consumed_at IS NULL
               AND expires_at > :now
         RETURNING user_id
            """
        ),
        {"now": now, "token_hash": token_hash},
    )
    row = result.first()
    if row is not None:
        return int(row[0])

    # Figure out *why* it failed so the caller can report accurately.
    existing = session.execute(
        text(
            "SELECT consumed_at, expires_at FROM magic_link_tokens WHERE token_hash = :h"
        ),
        {"h": token_hash},
    ).first()
    if existing is None:
        raise TokenNotFound()
    consumed_at, expires_at = existing
    if consumed_at is not None:
        raise TokenAlreadyConsumed()
    if expires_at <= now:
        raise TokenExpired()
    # Should be unreachable.
    raise TokenNotFound()
