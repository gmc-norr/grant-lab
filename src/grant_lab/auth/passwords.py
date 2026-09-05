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


def verify_password(password_hash: str | None, password: str) -> tuple[bool, bool]:
    """Verify `password` against `password_hash`.

    Returns ``(ok, needs_rehash)`` where:
    - ``ok`` is True only when the password matches a real (non-None) hash.
    - ``needs_rehash`` is True when the hash parameters are outdated and the
      caller should persist a freshly hashed credential (only possible on a
      successful verify, so it is always False when ok is False).

    When `password_hash` is None, verify runs against a dummy hash for timing
    parity (so an unknown-email path costs the same as a known-email path) and
    always returns ``(False, False)``.
    """
    target = password_hash if password_hash is not None else _DUMMY_HASH
    try:
        _ph.verify(target, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, False
    if password_hash is None:
        return False, False
    return True, _ph.check_needs_rehash(password_hash)
