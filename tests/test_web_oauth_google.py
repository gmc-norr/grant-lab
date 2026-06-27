# tests/test_web_oauth_google.py
import time
from urllib.parse import parse_qs, urlparse

from authlib.jose import JsonWebKey, jwt
from fastapi.testclient import TestClient

from call_manager.db.models import User
from call_manager.main import create_app

ISS = "https://accounts.google.com"
AUD = "test-client-id"
_KEY = JsonWebKey.generate_key("RSA", 2048, is_private=True)


class FakeOIDC:
    def __init__(self):
        self._codes = {}
        self.fail_exchange = False

    def set_code(self, code, claims):
        self._codes[code] = claims

    def build_authorization_url(self, *, redirect_uri, state, nonce, code_challenge):
        return f"{ISS}/auth?state={state}&nonce={nonce}&code_challenge={code_challenge}"

    def exchange_code(self, *, code, code_verifier, redirect_uri):
        from call_manager.auth.oidc import OIDCError

        if self.fail_exchange or code not in self._codes:
            raise OIDCError("bad code")
        return {
            "id_token": jwt.encode(
                {"alg": "RS256", "kid": _KEY.kid}, self._codes[code], _KEY
            ).decode()
        }

    def validate_id_token(self, id_token, *, expected_nonce):
        from call_manager.auth.oidc import IdTokenInvalid

        claims = jwt.decode(id_token, {"keys": [_KEY.as_dict(is_private=False)]})
        claims.validate()
        if claims.get("nonce") != expected_nonce:
            raise IdTokenInvalid("nonce")
        if claims.get("email_verified") is not True:
            raise IdTokenInvalid("email not verified")
        return dict(claims)


def _setup(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
        "GOOGLE_OIDC_ENABLED": "true",
        "GOOGLE_CLIENT_ID": AUD,
        "GOOGLE_CLIENT_SECRET": "secret",
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    fake = FakeOIDC()
    app.state.google_oidc_client = fake
    return app, fake


def _claims(**over):
    now = int(time.time())
    base = {
        "iss": ISS,
        "aud": AUD,
        "sub": "g-1",
        "email": "new@gmail.com",
        "email_verified": True,
        "name": "New",
        "iat": now,
        "exp": now + 600,
    }
    base.update(over)
    return base


def _start(c):
    r = c.post("/auth/google/start", data={"next": "/"}, follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    return q["state"][0], q["nonce"][0]


def test_start_sets_cookie_and_redirects(monkeypatch, pg_url, pg_session_factory):
    app, _ = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.post("/auth/google/start", data={"next": "/"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith(ISS)
        assert "oauth_txn" in "".join(r.headers.get_list("set-cookie"))


def test_callback_new_user_pending(monkeypatch, pg_url, pg_session_factory):
    app, fake = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        state, nonce = _start(c)
        # Allowed-domain (ki.se) email so the sign-in is admitted and creates a
        # pending account (the disallowed-domain path is covered separately).
        fake.set_code("C", _claims(email="new@ki.se", nonce=nonce))
        r = c.get(
            f"/auth/google/callback?code=C&state={state}",
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and r.headers["location"] == "/auth/pending"
        assert r.headers.get("referrer-policy") == "no-referrer"
        assert "oauth_txn=" in "".join(r.headers.get_list("set-cookie"))  # cleared
    with pg_session_factory() as s:
        assert s.query(User).filter_by(email="new@ki.se").one().status == "pending"


def test_callback_disallowed_domain_rejected(monkeypatch, pg_url, pg_session_factory):
    """A Google sign-in from a domain outside ALLOWED_EMAIL_DOMAINS must NOT
    create a pending account — it lands on /auth/error, parity with password
    registration + admin bootstrap. Regression for the OIDC allow-list gap."""
    app, fake = _setup(monkeypatch, pg_url)  # ALLOWED_EMAIL_DOMAINS=ki.se
    with TestClient(app) as c:
        state, nonce = _start(c)
        fake.set_code("C", _claims(email="outsider@gmail.com", nonce=nonce))
        r = c.get(
            f"/auth/google/callback?code=C&state={state}",
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
        assert r.headers["location"] == "/auth/error"
    with pg_session_factory() as s:
        assert s.query(User).filter_by(email="outsider@gmail.com").one_or_none() is None


def test_callback_active_user_gets_session(monkeypatch, pg_url, pg_session_factory):
    app, fake = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        s.add(User(email="ok@ki.se", status="active", oidc_iss=ISS, oidc_sub="g-2"))
        s.commit()
    with TestClient(app) as c:
        state, nonce = _start(c)
        fake.set_code("C2", _claims(sub="g-2", email="ok@ki.se", nonce=nonce))
        r = c.get(
            f"/auth/google/callback?code=C2&state={state}",
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert "session=" in "".join(r.headers.get_list("set-cookie"))


def test_callback_disabled_user_no_access(monkeypatch, pg_url, pg_session_factory):
    app, fake = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        s.add(User(email="no@ki.se", status="disabled", oidc_iss=ISS, oidc_sub="g-9"))
        s.commit()
    with TestClient(app) as c:
        state, nonce = _start(c)
        fake.set_code("C9", _claims(sub="g-9", email="no@ki.se", nonce=nonce))
        r = c.get(
            f"/auth/google/callback?code=C9&state={state}",
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
        assert r.headers["location"] == "/auth/no-access"


def test_callback_email_collision_error(monkeypatch, pg_url, pg_session_factory):
    app, fake = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        s.add(User(email="dupe@ki.se", status="active"))
        s.commit()
    with TestClient(app) as c:
        state, nonce = _start(c)
        fake.set_code("C3", _claims(sub="g-3", email="dupe@ki.se", nonce=nonce))
        r = c.get(
            f"/auth/google/callback?code=C3&state={state}",
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
        assert r.headers["location"] == "/auth/error"


def test_callback_state_mismatch_error(monkeypatch, pg_url, pg_session_factory):
    app, _ = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        _start(c)
        r = c.get(
            "/auth/google/callback?code=X&state=WRONG",
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
        assert r.headers["location"] == "/auth/error"
        assert r.headers.get("referrer-policy") == "no-referrer"


def test_callback_provider_error_param(monkeypatch, pg_url, pg_session_factory):
    app, _ = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        state, _ = _start(c)
        r = c.get(
            f"/auth/google/callback?error=access_denied&state={state}", follow_redirects=False
        )
        assert r.headers["location"] == "/auth/error"


def test_callback_missing_cookie_error(monkeypatch, pg_url, pg_session_factory):
    app, _ = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.get("/auth/google/callback?code=C&state=S", follow_redirects=False)
        assert r.headers["location"] == "/auth/error"


def test_callback_token_exchange_failure_error(monkeypatch, pg_url, pg_session_factory):
    app, fake = _setup(monkeypatch, pg_url)
    fake.fail_exchange = True
    with TestClient(app) as c:
        state, _ = _start(c)
        r = c.get(f"/auth/google/callback?code=C&state={state}", follow_redirects=False)
        assert r.headers["location"] == "/auth/error"


def test_callback_email_unverified_error(monkeypatch, pg_url, pg_session_factory):
    app, fake = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        state, nonce = _start(c)
        fake.set_code("CU", _claims(email_verified=False, nonce=nonce))
        r = c.get(f"/auth/google/callback?code=CU&state={state}", follow_redirects=False)
        assert r.headers["location"] == "/auth/error"


def test_routes_404_when_disabled(monkeypatch, pg_url, pg_session_factory):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    with TestClient(app) as c:
        assert c.post("/auth/google/start", data={}).status_code == 404


def test_connect_google_links_active_user(monkeypatch, pg_url, pg_session_factory):
    app, fake = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="link@ki.se", status="active")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    with TestClient(app) as c:
        c.cookies.set("session", app.state.session_signer.make(user_id=uid))
        start = c.post("/auth/google/start", data={"link": "1"}, follow_redirects=False)
        q = parse_qs(urlparse(start.headers["location"]).query)
        # Google identity must also be within ALLOWED_EMAIL_DOMAINS (ki.se) after
        # Fix 3: link-mode now enforces the domain allowlist so a user can't bypass
        # the domain gate by linking a personal Gmail to their institutional account.
        fake.set_code("LC", _claims(sub="g-link", email="link-google@ki.se", nonce=q["nonce"][0]))
        r = c.get(
            f"/auth/google/callback?code=LC&state={q['state'][0]}",
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
        assert r.headers["location"] == "/me"
    with pg_session_factory() as s:
        assert s.get(User, uid).oidc_sub == "g-link"


def test_connect_google_unauthenticated_redirects_to_login(monkeypatch, pg_url, pg_session_factory):
    app, _ = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.post("/auth/google/start", data={"link": "1"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
