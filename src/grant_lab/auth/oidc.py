# src/grant_lab/auth/oidc.py
from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from joserfc.jwt import JWTClaimsRegistry

_MAX_CLAIM_LEN = 4096  # reject absurdly long string claims


class IdTokenInvalid(Exception):
    """The id_token failed signature or claim validation."""


class OIDCError(Exception):
    """Discovery / token-endpoint / network failure."""


def generate_token(n: int = 32) -> str:
    return secrets.token_urlsafe(n)


def generate_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class OIDCClient:
    """Minimal OIDC relying-party client. Discovery-driven, RS256-only."""

    def __init__(
        self,
        *,
        discovery_url: str,
        client_id: str,
        client_secret: str,
        http_client: httpx.Client | None = None,
    ):
        self._discovery_url = discovery_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http_client or httpx.Client(timeout=10.0)
        self._metadata: dict | None = None
        self._jwks: KeySet | None = None

    def _meta(self) -> dict:
        if self._metadata is None:
            try:
                r = self._http.get(self._discovery_url)
                r.raise_for_status()
                self._metadata = r.json()
            except (httpx.HTTPError, ValueError) as e:
                raise OIDCError(f"discovery failed: {e}") from e
        return self._metadata

    def _jwk_set(self, *, force: bool = False):
        if self._jwks is None or force:
            try:
                r = self._http.get(self._meta()["jwks_uri"])
                r.raise_for_status()
                self._jwks = KeySet.import_key_set(r.json())
            except (httpx.HTTPError, JoseError, ValueError, KeyError) as e:
                raise OIDCError(f"jwks fetch failed: {e}") from e
        return self._jwks

    def build_authorization_url(self, *, redirect_uri, state, nonce, code_challenge) -> str:
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return self._meta()["authorization_endpoint"] + "?" + urlencode(params)

    def exchange_code(self, *, code, code_verifier, redirect_uri) -> dict:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code_verifier": code_verifier,
        }
        try:
            r = self._http.post(self._meta()["token_endpoint"], data=data)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise OIDCError(f"token exchange failed: {e}") from e

    def _decode(self, id_token: str):
        # Decode with the cached JWKS; on an unknown signing key (rotation),
        # refetch the JWKS ONCE and retry.
        try:
            return jwt.decode(id_token, self._jwk_set(), algorithms=["RS256"])
        except (JoseError, ValueError, KeyError):
            return jwt.decode(id_token, self._jwk_set(force=True), algorithms=["RS256"])

    def validate_id_token(self, id_token: str, *, expected_nonce: str) -> dict:
        issuer = self._meta()["issuer"]
        try:
            claims = self._decode(id_token).claims
            # leeway tolerates modest server/Google clock skew so a token with
            # iat a few seconds in the future (or exp just past) isn't spuriously
            # rejected; without it, skew causes intermittent
            # sign-in failures.
            JWTClaimsRegistry(
                leeway=60,
                iss={"essential": True, "value": issuer},
                exp={"essential": True},
                iat={"essential": True},
                sub={"essential": True},
                email={"essential": True},
                aud={"essential": True},
            ).validate(claims)
        except (JoseError, ValueError, KeyError) as e:
            raise IdTokenInvalid(f"decode/validate failed: {e}") from e

        aud = claims.get("aud")
        aud_ok = aud == self._client_id or (isinstance(aud, list) and self._client_id in aud)
        if not aud_ok:
            raise IdTokenInvalid("aud mismatch")
        if (isinstance(aud, list) or "azp" in claims) and claims.get("azp") != self._client_id:
            raise IdTokenInvalid("azp mismatch")
        if claims.get("nonce") != expected_nonce:
            raise IdTokenInvalid("nonce mismatch")
        if claims.get("email_verified") is not True:
            raise IdTokenInvalid("email not verified")

        # Type/length sanity on the strings we persist.
        for field in ("sub", "email", "iss"):
            v = claims.get(field)
            if not isinstance(v, str) or not (0 < len(v) <= _MAX_CLAIM_LEN):
                raise IdTokenInvalid(f"claim {field} has invalid type/length")
        name = claims.get("name")
        if name is not None and (not isinstance(name, str) or len(name) > _MAX_CLAIM_LEN):
            raise IdTokenInvalid("claim name has invalid type/length")
        return dict(claims)
