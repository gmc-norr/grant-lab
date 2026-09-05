import time

import pytest

from grant_lab.auth.session_cookie import (
    InvalidSession,
    SessionSigner,
)


def test_roundtrip():
    signer = SessionSigner(secret="x" * 32, max_age_seconds=3600)
    cookie = signer.make(user_id=42)
    data = signer.verify(cookie)
    assert data.user_id == 42
    assert data.session_epoch == 0


def test_roundtrip_with_epoch():
    signer = SessionSigner(secret="x" * 32, max_age_seconds=3600)
    cookie = signer.make(user_id=42, session_epoch=7)
    data = signer.verify(cookie)
    assert data.user_id == 42
    assert data.session_epoch == 7


def test_tampered_cookie_rejected():
    signer = SessionSigner(secret="x" * 32, max_age_seconds=3600)
    cookie = signer.make(user_id=42)
    # Flip the FIRST char (in the payload segment), not the last byte of
    # the signature: base64url's trailing character holds only 4 effective
    # bits and the 2 padding bits can decode to the same byte, leaving the
    # signature valid against an unchanged payload (~1-in-32 flake rate).
    tampered = ("A" if cookie[0] != "A" else "B") + cookie[1:]
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
