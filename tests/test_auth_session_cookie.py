import time

import pytest

from call_monitor.auth.session_cookie import (
    InvalidSession,
    SessionSigner,
)


def test_roundtrip():
    signer = SessionSigner(secret="x" * 32, max_age_seconds=3600)
    cookie = signer.make(user_id=42)
    assert signer.verify(cookie) == 42


def test_tampered_cookie_rejected():
    signer = SessionSigner(secret="x" * 32, max_age_seconds=3600)
    cookie = signer.make(user_id=42)
    tampered = cookie[:-1] + ("A" if cookie[-1] != "A" else "B")
    with pytest.raises(InvalidSession):
        signer.verify(tampered)


def test_different_secret_rejects():
    signer1 = SessionSigner(secret="x" * 32, max_age_seconds=3600)
    cookie = signer1.make(user_id=42)
    signer2 = SessionSigner(secret="y" * 32, max_age_seconds=3600)
    with pytest.raises(InvalidSession):
        signer2.verify(cookie)


def test_expired_cookie_rejected():
    signer = SessionSigner(secret="x" * 32, max_age_seconds=0)
    cookie = signer.make(user_id=42)
    # max_age=0 means instantly expired when verified
    time.sleep(1)
    with pytest.raises(InvalidSession):
        signer.verify(cookie)
