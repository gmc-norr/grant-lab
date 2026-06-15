from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 12

_ph = PasswordHasher()
# Precomputed dummy hash: verifying against it on the no-credential path costs
# the same as a real verify, so timing doesn't reveal whether a credential exists.
_DUMMY_HASH = _ph.hash("x" * 16)


class WeakPassword(ValueError):
    """Password does not meet the minimum policy."""


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(f"password must be at least {MIN_PASSWORD_LENGTH} characters")


def hash_password(password: str) -> str:
    validate_password(password)
    return _ph.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Verify `password` against `password_hash`. When `password_hash` is None,
    verify against a dummy hash (timing parity) and always return False."""
    target = password_hash if password_hash is not None else _DUMMY_HASH
    try:
        _ph.verify(target, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return password_hash is not None
