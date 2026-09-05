from __future__ import annotations

from dataclasses import dataclass

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


class InvalidSession(Exception):
    pass


@dataclass(frozen=True)
class SessionData:
    user_id: int
    # Cookie's view of the user's session epoch. get_current_user rejects the
    # cookie when this no longer matches users.session_epoch (logout / admin
    # revocation bumps the column, invalidating every outstanding cookie).
    session_epoch: int


class SessionSigner:
    """Sign (not encrypt) the user id into the session cookie.

    URLSafeTimedSerializer emits `<base64-json>.<timestamp>.<hmac>`. The
    payload segment is plaintext to anyone who holds the cookie — which
    is fine for a bare integer user id, but means the cookie is NOT a
    place to stash anything sensitive (email, role flags, internal
    identifiers). Add sensitive state to server-side session storage
    instead. Signing still prevents tampering (the HMAC gate remains).
    """

    def __init__(self, *, secret: str, max_age_seconds: int = 30 * 24 * 3600):
        self._serializer = URLSafeTimedSerializer(secret, salt="call-manager-session")
        self._max_age = max_age_seconds

    def make(self, *, user_id: int, session_epoch: int = 0) -> str:
        return self._serializer.dumps({"uid": user_id, "ep": session_epoch})

    def verify(self, cookie: str) -> SessionData:
        try:
            payload = self._serializer.loads(cookie, max_age=self._max_age)
        except (BadSignature, SignatureExpired) as e:
            raise InvalidSession(str(e)) from e
        if not isinstance(payload, dict) or "uid" not in payload:
            raise InvalidSession("malformed payload")
        # `ep` is absent on cookies minted before session_epoch existed; treat
        # those as epoch 0 (the server-side default) so they remain valid.
        return SessionData(user_id=int(payload["uid"]), session_epoch=int(payload.get("ep", 0)))
