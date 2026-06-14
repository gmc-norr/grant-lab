# src/call_manager/auth/oauth_txn.py
from __future__ import annotations

import base64
import json

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class InvalidTransaction(Exception):
    """The OAuth transaction cookie is missing, tampered, or expired."""


def _fernet_key_from_secret(secret: str) -> bytes:
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"call-manager-oauth-txn",  # distinct from the session signer's key
    ).derive(secret.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


class TransactionCipher:
    """Encrypt (not just sign) the short-lived OAuth transaction payload.

    Holds the PKCE code_verifier, state, nonce and post-login next. The
    project's SessionSigner forbids stashing sensitive state in a signed-only
    cookie; Fernet gives authenticated encryption + a built-in TTL.
    """

    def __init__(self, *, secret: str):
        self._fernet = Fernet(_fernet_key_from_secret(secret))

    def make(self, payload: dict) -> str:
        return self._fernet.encrypt(json.dumps(payload).encode("utf-8")).decode("ascii")

    def read(self, token: str, *, max_age_seconds: int) -> dict:
        # A non-positive validity window means the token is already expired.
        # Fernet's TTL check is second-resolution (current > ts + ttl), so a
        # freshly-minted token with ttl=0 would otherwise still verify within
        # the same second; reject it explicitly.
        if max_age_seconds <= 0:
            raise InvalidTransaction("transaction expired")
        try:
            raw = self._fernet.decrypt(token.encode("ascii"), ttl=max_age_seconds)
        except (InvalidToken, ValueError, TypeError) as e:
            raise InvalidTransaction(str(e)) from e
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise InvalidTransaction("payload is not an object")
        return data
