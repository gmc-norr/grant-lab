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
    ok, _ = verify_password(h, "correct horse battery")
    assert ok is True
    ok, _ = verify_password(h, "wrong")
    assert ok is False


def test_validate_rejects_short():
    with pytest.raises(WeakPassword):
        validate_password("a" * (MIN_PASSWORD_LENGTH - 1))
    validate_password("a" * MIN_PASSWORD_LENGTH)  # no raise


def test_hash_rejects_short():
    with pytest.raises(WeakPassword):
        hash_password("short")


def test_verify_none_hash_is_false_but_runs():
    ok, _ = verify_password(None, "anything")
    assert ok is False
    ok, _ = verify_password(None, "x" * 16)
    assert ok is False


def test_verify_tolerates_malformed_hash():
    ok, _ = verify_password("not-a-real-argon2-hash", "whatever")
    assert ok is False
