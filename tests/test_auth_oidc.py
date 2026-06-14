# tests/test_auth_oidc.py
import time

import httpx
import pytest
from authlib.jose import JsonWebKey, jwt

from call_manager.auth.oidc import (
    IdTokenInvalid,
    OIDCClient,
    generate_pkce,
    generate_token,
)

ISS = "https://accounts.google.com"
AUD = "test-client-id"
DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"
_KEY = JsonWebKey.generate_key("RSA", 2048, is_private=True)


def _meta():
    return {
        "issuer": ISS,
        "authorization_endpoint": f"{ISS}/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
    }


def _id_token(claims: dict, key=_KEY) -> str:
    return jwt.encode({"alg": "RS256", "kid": key.kid}, claims, key).decode("ascii")


def _make_client(token_response=None, keys=None, alg="RS256"):
    jwks = {"keys": [(keys or _KEY).as_dict(is_private=False)]}

    def handler(request):
        url = str(request.url)
        if url == DISCOVERY:
            return httpx.Response(200, json=_meta())
        if url == _meta()["jwks_uri"]:
            return httpx.Response(200, json=jwks)
        if url == _meta()["token_endpoint"]:
            return httpx.Response(200, json=token_response or {})
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return OIDCClient(discovery_url=DISCOVERY, client_id=AUD, client_secret="s", http_client=http)


def _claims(**over):
    now = int(time.time())
    base = {
        "iss": ISS, "aud": AUD, "sub": "1234567890", "email": "p@gmail.com",
        "email_verified": True, "name": "P", "nonce": "the-nonce",
        "iat": now, "exp": now + 600,
    }
    base.update(over)
    return base


def test_pkce_and_token():
    v, c = generate_pkce()
    assert c != v and len(v) >= 43
    assert generate_token() != generate_token()


def test_authorization_url():
    url = _make_client().build_authorization_url(
        redirect_uri="https://app/cb", state="st", nonce="no", code_challenge="cc")
    assert "code_challenge=cc" in url and "code_challenge_method=S256" in url
    assert "state=st" in url and "nonce=no" in url and "scope=openid" in url


def test_happy_path():
    client = _make_client({"id_token": _id_token(_claims())})
    tok = client.exchange_code(code="abc", code_verifier="v", redirect_uri="https://app/cb")
    claims = client.validate_id_token(tok["id_token"], expected_nonce="the-nonce")
    assert claims["sub"] == "1234567890" and claims["email_verified"] is True


@pytest.mark.parametrize("over", [
    {"iss": "https://evil"}, {"aud": "other"}, {"exp": int(time.time()) - 10},
    {"nonce": "wrong"}, {"email_verified": False},
])
def test_rejects_bad_claims(over):
    client = _make_client()
    with pytest.raises(IdTokenInvalid):
        client.validate_id_token(_id_token(_claims(**over)), expected_nonce="the-nonce")


@pytest.mark.parametrize("missing", ["sub", "email", "exp", "iat"])
def test_rejects_missing_essential_claim(missing):
    client = _make_client()
    c = _claims()
    del c[missing]
    with pytest.raises(IdTokenInvalid):
        client.validate_id_token(_id_token(c), expected_nonce="the-nonce")


def test_array_aud_with_matching_azp_ok():
    client = _make_client()
    claims = client.validate_id_token(
        _id_token(_claims(aud=[AUD, "x"], azp=AUD)), expected_nonce="the-nonce")
    assert claims["sub"] == "1234567890"


def test_array_aud_without_our_id_rejected():
    client = _make_client()
    with pytest.raises(IdTokenInvalid):
        client.validate_id_token(
            _id_token(_claims(aud=["a", "b"], azp="a")), expected_nonce="the-nonce")


def test_unknown_kid_triggers_jwks_refetch():
    # First the client caches a JWKS with KEY1; then Google "rotates" to KEY2.
    key1 = JsonWebKey.generate_key("RSA", 2048, is_private=True)
    key2 = JsonWebKey.generate_key("RSA", 2048, is_private=True)
    state = {"key": key1}

    def handler(request):
        url = str(request.url)
        if url == DISCOVERY:
            return httpx.Response(200, json=_meta())
        if url == _meta()["jwks_uri"]:
            return httpx.Response(200, json={"keys": [state["key"].as_dict(is_private=False)]})
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = OIDCClient(discovery_url=DISCOVERY, client_id=AUD, client_secret="s", http_client=http)
    # prime cache with key1
    client.validate_id_token(_id_token(_claims(), key=key1), expected_nonce="the-nonce")
    # rotate: token signed by key2, server now serves key2
    state["key"] = key2
    claims = client.validate_id_token(_id_token(_claims(), key=key2), expected_nonce="the-nonce")
    assert claims["sub"] == "1234567890"
