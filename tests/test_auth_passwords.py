import pytest

from call_manager.auth.passwords import (
    MIN_PASSWORD_LENGTH,
    WeakPassword,
    hash_password,
    validate_password,
    verify_password,
)


def test_hash_and_verify_roundtrip():
    h = hash_password("correct horse battery")
    assert h != "correct horse battery"
    assert verify_password(h, "correct horse battery") is True
    assert verify_password(h, "wrong") is False


def test_validate_rejects_short():
    with pytest.raises(WeakPassword):
        validate_password("a" * (MIN_PASSWORD_LENGTH - 1))
    validate_password("a" * MIN_PASSWORD_LENGTH)  # no raise


def test_hash_rejects_short():
    with pytest.raises(WeakPassword):
        hash_password("short")


def test_verify_none_hash_is_false_but_runs():
    assert verify_password(None, "anything") is False
    assert verify_password(None, "x" * 16) is False


def test_verify_tolerates_malformed_hash():
    assert verify_password("not-a-real-argon2-hash", "whatever") is False
