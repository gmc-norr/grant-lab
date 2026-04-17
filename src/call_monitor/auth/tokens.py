from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from call_monitor.db.models import MagicLinkToken


class InvalidToken(Exception):
    """Raised by consume_token for any reason the token can't be redeemed.

    One exception class, not three, because distinguishing
    unknown / expired / already-consumed at the API surface creates a timing
    oracle the /login/verify route shouldn't expose. All three paths are
    collapsed to the same 400 by the route handler, and that's by design.
    """


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_token(session: Session, *, user_id: int, ttl_minutes: int = 15) -> str:
    """Issue a magic-link token. Caller is responsible for committing.

    The raw token is returned once; only its sha256 hash is persisted.
    """
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

    Single SQL statement so there's no classification oracle: unknown,
    expired, and already-consumed all surface as InvalidToken.

    Caller must commit the session for the consumption to persist;
    if the caller's transaction rolls back, the token remains redeemable.
    """
    token_hash = hash_token(raw)
    now = datetime.now(UTC)

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
    if row is None:
        raise InvalidToken()
    return int(row[0])
