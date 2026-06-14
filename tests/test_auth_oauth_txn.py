# tests/test_auth_oauth_txn.py
import pytest

from call_manager.auth.oauth_txn import InvalidTransaction, TransactionCipher


def test_round_trip():
    c = TransactionCipher(secret="x" * 32)
    token = c.make({"state": "s", "nonce": "n", "code_verifier": "v", "next": "/x"})
    assert c.read(token, max_age_seconds=600) == {
        "state": "s", "nonce": "n", "code_verifier": "v", "next": "/x",
    }


def test_tamper_rejected():
    c = TransactionCipher(secret="x" * 32)
    token = c.make({"state": "s"})
    bad = token[:-2] + ("AA" if token[-2:] != "AA" else "BB")
    with pytest.raises(InvalidTransaction):
        c.read(bad, max_age_seconds=600)


def test_expiry_rejected():
    c = TransactionCipher(secret="x" * 32)
    token = c.make({"state": "s"})
    with pytest.raises(InvalidTransaction):
        c.read(token, max_age_seconds=0)


def test_distinct_secret_cannot_read():
    a = TransactionCipher(secret="a" * 32)
    b = TransactionCipher(secret="b" * 32)
    with pytest.raises(InvalidTransaction):
        b.read(a.make({"state": "s"}), max_age_seconds=600)
