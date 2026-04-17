from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


class InvalidSession(Exception):
    pass


class SessionSigner:
    def __init__(self, *, secret: str, max_age_seconds: int = 30 * 24 * 3600):
        self._serializer = URLSafeTimedSerializer(secret, salt="call-monitor-session")
        self._max_age = max_age_seconds

    def make(self, *, user_id: int) -> str:
        return self._serializer.dumps({"uid": user_id})

    def verify(self, cookie: str) -> int:
        try:
            payload = self._serializer.loads(cookie, max_age=self._max_age)
        except (BadSignature, SignatureExpired) as e:
            raise InvalidSession(str(e)) from e
        if not isinstance(payload, dict) or "uid" not in payload:
            raise InvalidSession("malformed payload")
        return int(payload["uid"])
