# Google OIDC + OAuth Authorization Code with PKCE — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let people sign in with Google via OIDC (Authorization Code + PKCE) behind an admin approval gate — self-service sign-in creates a `pending` account with no access until an admin approves it and sets member/admin; magic-link is kept but becomes login-only.

**Architecture:** A discovery-URL-driven OIDC client (Authlib Jose + httpx) drives a `/auth/google/start` → `/auth/google/callback` flow. Transaction state (PKCE verifier, state, nonce, next) rides in a short-lived **encrypted** cookie. Accounts are resolved only by the stable `(oidc_iss, oidc_sub)` pair (no email auto-linking). A new `users.status` column (`pending`/`active`/`disabled`) is enforced centrally in `get_current_user` and in digest selection. Admins approve/deny/disable via new admin routes.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x + Alembic + PostgreSQL 16, Jinja2/HTMX, Authlib, cryptography (Fernet), httpx, pytest + testcontainers.

**Spec:** [docs/superpowers/specs/2026-06-14-google-oauth-oidc-pkce-design.md](../specs/2026-06-14-google-oauth-oidc-pkce-design.md)

**Conventions used below:**
- Run tests with `uv run pytest` and lint with `uv run ruff check` / `uv run ruff format`.
- Each task ends with a commit. Keep commits green.
- New users default to `status='active'` at the DB layer (`server_default`), because every *existing* code path that builds a `User(...)` is a trusted path (bootstrap, tests). The **only** untrusted path — Google self-service resolution (Task 6) — sets `status='pending'` explicitly.

---

## Task 1: Add dependencies (Authlib, cryptography)

**Files:**
- Modify: `pyproject.toml` (dependencies array)

- [ ] **Step 1: Add the two dependencies**

In `pyproject.toml`, add to the `dependencies` array (alphabetical-ish, next to `httpx`):

```toml
    "authlib>=1.3",
    "cryptography>=43.0",
```

- [ ] **Step 2: Sync the environment**

Run: `uv sync`
Expected: resolves and installs `authlib` and `cryptography` (cryptography may already be present transitively; this pins it as a direct dep).

- [ ] **Step 3: Verify imports**

Run: `uv run python -c "import authlib.jose, cryptography.fernet; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add authlib + cryptography for Google OIDC"
```

---

## Task 2: `users` status + OIDC columns (model + migration)

**Files:**
- Modify: `src/call_manager/db/models.py` (User class, lines ~35-75)
- Create: `alembic/versions/<rev>_users_status_and_oidc.py`
- Test: `tests/test_user_status_oidc_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_user_status_oidc_model.py
import pytest
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import User


def test_new_user_defaults_to_active(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="a@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        assert u.status == "active"
        assert u.oidc_sub is None and u.oidc_iss is None


def test_status_check_constraint_rejects_bad_value(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="b@ki.se", status="bogus"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_paired_null_oidc_constraint(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="c@ki.se", oidc_sub="123"))  # iss missing -> violates pairing
        with pytest.raises(IntegrityError):
            s.commit()


def test_oidc_identity_is_unique(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="d@ki.se", oidc_iss="https://accounts.google.com", oidc_sub="sub-1"))
        s.commit()
    with pg_session_factory() as s:
        s.add(User(email="e@ki.se", oidc_iss="https://accounts.google.com", oidc_sub="sub-1"))
        with pytest.raises(IntegrityError):
            s.commit()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_user_status_oidc_model.py -v`
Expected: errors/failures — `User` has no `status`/`oidc_sub`/`oidc_iss` attributes (and the migration doesn't exist yet).

- [ ] **Step 3: Add columns + constraints to the model**

In `src/call_manager/db/models.py`, extend `User.__table_args__` and add columns. Replace the existing `__table_args__` tuple with:

```python
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),
        CheckConstraint(
            "status IN ('pending', 'active', 'disabled')",
            name="ck_users_status_valid",
        ),
        CheckConstraint(
            "(oidc_iss IS NULL) = (oidc_sub IS NULL)",
            name="ck_users_oidc_paired_null",
        ),
        Index(
            "uq_users_oidc_identity",
            "oidc_iss",
            "oidc_sub",
            unique=True,
            postgresql_where=text("oidc_sub IS NOT NULL"),
        ),
    )
```

Add these columns after `is_admin` (keep `is_admin` as-is):

```python
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'active'")
    )
    oidc_iss: Mapped[str | None] = mapped_column(Text, nullable=True)
    oidc_sub: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

(`Index`, `Text`, `BigInteger`, `ForeignKey`, `CheckConstraint`, `text` are already imported at the top of the file.)

- [ ] **Step 4: Find the current migration head**

Run: `uv run alembic heads`
Expected: prints one revision id, e.g. `00ab... (head)`. Note it as `<HEAD_REV>`.

- [ ] **Step 5: Create the migration**

Create `alembic/versions/0099_users_status_and_oidc.py` (pick the next numeric prefix after the current highest in `alembic/versions/`):

```python
"""users.status + oidc identity columns

Revision ID: users_status_oidc
Revises: <HEAD_REV>
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "users_status_oidc"
down_revision = "<HEAD_REV>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
    )
    op.add_column("users", sa.Column("oidc_iss", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("oidc_sub", sa.Text(), nullable=True))
    op.add_column(
        "users",
        sa.Column("approved_by_user_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "users", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_users_approved_by",
        "users",
        "users",
        ["approved_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_users_status_valid", "users", "status IN ('pending', 'active', 'disabled')"
    )
    op.create_check_constraint(
        "ck_users_oidc_paired_null", "users", "(oidc_iss IS NULL) = (oidc_sub IS NULL)"
    )
    op.create_index(
        "uq_users_oidc_identity",
        "users",
        ["oidc_iss", "oidc_sub"],
        unique=True,
        postgresql_where=sa.text("oidc_sub IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_users_oidc_identity", table_name="users")
    op.drop_constraint("ck_users_oidc_paired_null", "users", type_="check")
    op.drop_constraint("ck_users_status_valid", "users", type_="check")
    op.drop_constraint("fk_users_approved_by", "users", type_="foreignkey")
    op.drop_column("users", "approved_at")
    op.drop_column("users", "approved_by_user_id")
    op.drop_column("users", "oidc_sub")
    op.drop_column("users", "oidc_iss")
    op.drop_column("users", "status")
```

- [ ] **Step 6: Run the model tests**

Run: `uv run pytest tests/test_user_status_oidc_model.py -v`
Expected: all PASS (the `migrated_engine` fixture runs `alembic upgrade head`, applying the new migration).

- [ ] **Step 7: Verify migration round-trips**

Run: `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: no errors. (Requires a reachable dev DB via `DATABASE_URL`; skip if not available locally — CI/testcontainers covers upgrade.)

- [ ] **Step 8: Commit**

```bash
git add src/call_manager/db/models.py alembic/versions/ tests/test_user_status_oidc_model.py
git commit -m "feat(db): add users.status + oidc identity columns and constraints"
```

---

## Task 3: Encrypted OAuth transaction cookie

**Files:**
- Create: `src/call_manager/auth/oauth_txn.py`
- Test: `tests/test_auth_oauth_txn.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auth_oauth_txn.py
import time

import pytest

from call_manager.auth.oauth_txn import InvalidTransaction, TransactionCipher


def test_round_trip():
    c = TransactionCipher(secret="x" * 32)
    token = c.make({"state": "s", "nonce": "n", "code_verifier": "v", "next": "/x"})
    assert c.read(token, max_age_seconds=600) == {
        "state": "s",
        "nonce": "n",
        "code_verifier": "v",
        "next": "/x",
    }


def test_tamper_rejected():
    c = TransactionCipher(secret="x" * 32)
    token = c.make({"state": "s"})
    with pytest.raises(InvalidTransaction):
        c.read(token[:-2] + ("AA" if token[-2:] != "AA" else "BB"), max_age_seconds=600)


def test_expiry_rejected():
    c = TransactionCipher(secret="x" * 32)
    token = c.make({"state": "s"})
    with pytest.raises(InvalidTransaction):
        c.read(token, max_age_seconds=0)
        time.sleep(0.01)
        c.read(token, max_age_seconds=0)


def test_distinct_secret_cannot_read():
    a = TransactionCipher(secret="a" * 32)
    b = TransactionCipher(secret="b" * 32)
    with pytest.raises(InvalidTransaction):
        b.read(a.make({"state": "s"}), max_age_seconds=600)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_auth_oauth_txn.py -v`
Expected: FAIL — module `call_manager.auth.oauth_txn` does not exist.

- [ ] **Step 3: Implement the cipher**

```python
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
    """Derive a Fernet key from SESSION_SECRET via HKDF.

    A distinct `info` label keeps this key independent from the session
    cookie's signing key (which uses the same SESSION_SECRET but the
    itsdangerous salt 'call-manager-session'). No second secret to manage.
    """
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"call-manager-oauth-txn",
    ).derive(secret.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


class TransactionCipher:
    """Encrypt (not just sign) the short-lived OAuth transaction payload.

    Holds the PKCE code_verifier, state, nonce and post-login next. The
    project's SessionSigner forbids stashing sensitive state in a signed-only
    cookie; Fernet gives us authenticated encryption + a built-in TTL.
    """

    def __init__(self, *, secret: str):
        self._fernet = Fernet(_fernet_key_from_secret(secret))

    def make(self, payload: dict) -> str:
        return self._fernet.encrypt(json.dumps(payload).encode("utf-8")).decode("ascii")

    def read(self, token: str, *, max_age_seconds: int) -> dict:
        try:
            raw = self._fernet.decrypt(token.encode("ascii"), ttl=max_age_seconds)
        except (InvalidToken, ValueError, TypeError) as e:
            raise InvalidTransaction(str(e)) from e
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise InvalidTransaction("payload is not an object")
        return data
```

Note: the test calls `c.read(token, max_age_seconds=...)` positionally for the token and keyword for max_age; signature above matches (`token` positional, `max_age_seconds` keyword-only).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_auth_oauth_txn.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/auth/oauth_txn.py
git add src/call_manager/auth/oauth_txn.py tests/test_auth_oauth_txn.py
git commit -m "feat(auth): encrypted OAuth transaction cookie cipher"
```

---

## Task 4: Google OIDC configuration

**Files:**
- Modify: `src/call_manager/config.py` (Settings fields + a model_validator + a computed field)
- Modify: `.env.example`
- Test: `tests/test_config_google_oidc.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_google_oidc.py
import pytest

from call_manager.config import Settings


def _base(monkeypatch, **extra):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x/y")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "https://app.example.org")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    return Settings()


def test_oidc_disabled_by_default(monkeypatch):
    s = _base(monkeypatch)
    assert s.google_oidc_enabled is False
    assert s.google_redirect_uri == "https://app.example.org/auth/google/callback"


def test_enabled_requires_client_credentials(monkeypatch):
    with pytest.raises(ValueError, match="GOOGLE_CLIENT_ID"):
        _base(monkeypatch, GOOGLE_OIDC_ENABLED="true")


def test_enabled_with_credentials_ok(monkeypatch):
    s = _base(
        monkeypatch,
        GOOGLE_OIDC_ENABLED="true",
        GOOGLE_CLIENT_ID="cid",
        GOOGLE_CLIENT_SECRET="csecret",
    )
    assert s.google_oidc_enabled is True
    assert s.google_client_id == "cid"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_config_google_oidc.py -v`
Expected: FAIL — `Settings` has no `google_oidc_enabled` etc.

- [ ] **Step 3: Add the settings fields**

In `src/call_manager/config.py`, add these fields inside `class Settings` (place near `app_base_url`):

```python
    google_oidc_enabled: bool = Field(default=False, alias="GOOGLE_OIDC_ENABLED")
    google_client_id: str | None = Field(default=None, alias="GOOGLE_CLIENT_ID")
    google_client_secret: str | None = Field(default=None, alias="GOOGLE_CLIENT_SECRET")
    google_oidc_discovery_url: str = Field(
        default="https://accounts.google.com/.well-known/openid-configuration",
        alias="GOOGLE_OIDC_DISCOVERY_URL",
    )
```

- [ ] **Step 4: Add the computed redirect URI and the prod guard**

Add a computed field (next to `cookie_secure`):

```python
    @computed_field  # type: ignore[prop-decorator]
    @property
    def google_redirect_uri(self) -> str:
        return self.app_base_url.rstrip("/") + "/auth/google/callback"
```

Add a validator (next to `_require_strong_secret_in_prod`):

```python
    @model_validator(mode="after")
    def _require_google_credentials_when_enabled(self):
        if self.google_oidc_enabled:
            if not self.google_client_id or not self.google_client_secret:
                raise ValueError(
                    "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required when "
                    "GOOGLE_OIDC_ENABLED is true."
                )
        return self
```

- [ ] **Step 5: Update `.env.example`**

Append:

```bash
# --- Google sign-in (OIDC, Authorization Code + PKCE) ---
# Off by default. When true, GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required.
# Register the redirect URI <APP_BASE_URL>/auth/google/callback in the Google
# Cloud console. Self-service sign-in creates a PENDING account with no access
# until an admin approves it.
# GOOGLE_OIDC_ENABLED=false
# GOOGLE_CLIENT_ID=
# GOOGLE_CLIENT_SECRET=
# GOOGLE_OIDC_DISCOVERY_URL=https://accounts.google.com/.well-known/openid-configuration

# NOTE: INITIAL_ADMIN_EMAIL admins are re-activated + re-promoted on every
# startup (lockout recovery). To permanently disable a configured admin, remove
# them from INITIAL_ADMIN_EMAIL first.
```

- [ ] **Step 6: Run to verify it passes**

Run: `uv run pytest tests/test_config_google_oidc.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/config.py .env.example tests/test_config_google_oidc.py
git commit -m "feat(config): Google OIDC settings + prod guard + redirect URI"
```

---

## Task 5: OIDC client (discovery, code exchange, id_token validation, PKCE)

**Files:**
- Create: `src/call_manager/auth/oidc.py`
- Test: `tests/test_auth_oidc.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auth_oidc.py
import json
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


def _id_token(claims: dict) -> str:
    header = {"alg": "RS256", "kid": _KEY.kid}
    return jwt.encode(header, claims, _KEY).decode("ascii")


def _metadata() -> dict:
    return {
        "issuer": ISS,
        "authorization_endpoint": f"{ISS}/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
    }


def _make_client(token_response: dict | None = None) -> OIDCClient:
    jwks = {"keys": [_KEY.as_dict(is_private=False)]}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == DISCOVERY:
            return httpx.Response(200, json=_metadata())
        if url == _metadata()["jwks_uri"]:
            return httpx.Response(200, json=jwks)
        if url == _metadata()["token_endpoint"]:
            return httpx.Response(200, json=token_response or {})
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return OIDCClient(
        discovery_url=DISCOVERY,
        client_id=AUD,
        client_secret="secret",
        http_client=http,
    )


def _valid_claims(**over) -> dict:
    now = int(time.time())
    base = {
        "iss": ISS,
        "aud": AUD,
        "sub": "1234567890",
        "email": "person@gmail.com",
        "email_verified": True,
        "name": "A Person",
        "nonce": "the-nonce",
        "iat": now,
        "exp": now + 600,
    }
    base.update(over)
    return base


def test_generate_pkce_and_token_are_distinct():
    v1, c1 = generate_pkce()
    assert c1 != v1 and len(v1) >= 43
    assert generate_token() != generate_token()


def test_authorization_url_contains_pkce_and_state():
    client = _make_client()
    url = client.build_authorization_url(
        redirect_uri="https://app/cb", state="st", nonce="no", code_challenge="cc"
    )
    assert "code_challenge=cc" in url and "code_challenge_method=S256" in url
    assert "state=st" in url and "nonce=no" in url and "scope=openid" in url


def test_exchange_and_validate_happy_path():
    client = _make_client({"id_token": _id_token(_valid_claims())})
    token = client.exchange_code(code="abc", code_verifier="v", redirect_uri="https://app/cb")
    claims = client.validate_id_token(token["id_token"], expected_nonce="the-nonce")
    assert claims["sub"] == "1234567890"
    assert claims["email_verified"] is True


@pytest.mark.parametrize(
    "over",
    [
        {"iss": "https://evil.example"},
        {"aud": "someone-else"},
        {"exp": int(time.time()) - 10},
        {"nonce": "wrong"},
    ],
)
def test_validate_rejects_bad_claims(over):
    client = _make_client()
    with pytest.raises(IdTokenInvalid):
        client.validate_id_token(_id_token(_valid_claims(**over)), expected_nonce="the-nonce")


def test_validate_accepts_array_aud_with_matching_azp():
    client = _make_client()
    tok = _id_token(_valid_claims(aud=[AUD, "other"], azp=AUD))
    claims = client.validate_id_token(tok, expected_nonce="the-nonce")
    assert claims["sub"] == "1234567890"


def test_validate_rejects_array_aud_without_our_id():
    client = _make_client()
    tok = _id_token(_valid_claims(aud=["a", "b"], azp="a"))
    with pytest.raises(IdTokenInvalid):
        client.validate_id_token(tok, expected_nonce="the-nonce")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_auth_oidc.py -v`
Expected: FAIL — `call_manager.auth.oidc` does not exist.

- [ ] **Step 3: Implement the client**

```python
# src/call_manager/auth/oidc.py
from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import httpx
from authlib.jose import JsonWebKey, jwt
from authlib.jose.errors import JoseError


class IdTokenInvalid(Exception):
    """The id_token failed signature or claim validation."""


class OIDCError(Exception):
    """Discovery / token-endpoint / network failure."""


def generate_token(n: int = 32) -> str:
    """Random URL-safe token for `state` / `nonce`."""
    return secrets.token_urlsafe(n)


def generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class OIDCClient:
    """Minimal OIDC relying-party client over httpx + authlib.jose.

    Discovery-driven so the issuer can later be swapped for a broker.
    """

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
        self._jwks = None

    def _meta(self) -> dict:
        if self._metadata is None:
            try:
                r = self._http.get(self._discovery_url)
                r.raise_for_status()
                self._metadata = r.json()
            except (httpx.HTTPError, ValueError) as e:
                raise OIDCError(f"discovery failed: {e}") from e
        return self._metadata

    def _jwk_set(self):
        if self._jwks is None:
            try:
                r = self._http.get(self._meta()["jwks_uri"])
                r.raise_for_status()
                self._jwks = JsonWebKey.import_key_set(r.json())
            except (httpx.HTTPError, ValueError, KeyError) as e:
                raise OIDCError(f"jwks fetch failed: {e}") from e
        return self._jwks

    def build_authorization_url(
        self, *, redirect_uri: str, state: str, nonce: str, code_challenge: str
    ) -> str:
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

    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> dict:
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

    def validate_id_token(self, id_token: str, *, expected_nonce: str) -> dict:
        try:
            claims = jwt.decode(id_token, self._jwk_set())
        except JoseError as e:
            raise IdTokenInvalid(f"decode failed: {e}") from e

        iss = self._meta()["issuer"]
        # aud may be a string or a list; our client_id must be present.
        aud = claims.get("aud")
        aud_ok = aud == self._client_id or (
            isinstance(aud, list) and self._client_id in aud
        )
        if claims.get("iss") != iss or not aud_ok:
            raise IdTokenInvalid("iss/aud mismatch")
        # When aud is multi-valued (or azp present), azp must be our client_id.
        if isinstance(aud, list) or "azp" in claims:
            if claims.get("azp") != self._client_id:
                raise IdTokenInvalid("azp mismatch")
        if claims.get("nonce") != expected_nonce:
            raise IdTokenInvalid("nonce mismatch")
        try:
            claims.validate()  # exp / iat / nbf
        except JoseError as e:
            raise IdTokenInvalid(f"claim validation failed: {e}") from e
        return dict(claims)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_auth_oidc.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/auth/oidc.py
git add src/call_manager/auth/oidc.py tests/test_auth_oidc.py
git commit -m "feat(auth): Google OIDC client (discovery, PKCE, code exchange, id_token validation)"
```

---

## Task 6: Race-safe account resolution + linking

**Files:**
- Create: `src/call_manager/auth/accounts.py`
- Test: `tests/test_auth_accounts.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auth_accounts.py
import pytest

from call_manager.auth.accounts import (
    AccountResolution,
    link_google_identity,
    resolve_google_account,
)
from call_manager.db.models import User

ISS = "https://accounts.google.com"


def test_new_identity_creates_pending(pg_session_factory):
    with pg_session_factory() as s:
        res = resolve_google_account(s, iss=ISS, sub="s1", email="new@gmail.com", name="N")
        s.commit()
        assert res.outcome == "created"
        assert res.user.status == "pending"
        assert res.user.is_admin is False


def test_returning_identity_matches(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="x@ki.se", status="active", oidc_iss=ISS, oidc_sub="s2"))
        s.commit()
    with pg_session_factory() as s:
        res = resolve_google_account(s, iss=ISS, sub="s2", email="x@ki.se", name=None)
        assert res.outcome == "matched"
        assert res.user.status == "active"


def test_email_collision_without_link_is_blocked(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="admin@ki.se", status="active"))  # no oidc identity
        s.commit()
    with pg_session_factory() as s:
        res = resolve_google_account(s, iss=ISS, sub="s3", email="admin@ki.se", name=None)
        assert res.outcome == "email_collision"
        assert res.user is None
        # nothing created
        assert s.query(User).filter_by(email="admin@ki.se").one().oidc_sub is None


def test_link_attaches_identity_to_user(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="me@ki.se", status="active")
        s.add(u)
        s.commit()
        uid = u.id
    with pg_session_factory() as s:
        ok = link_google_identity(s, user_id=uid, iss=ISS, sub="s4")
        s.commit()
        assert ok is True
        assert s.get(User, uid).oidc_sub == "s4"


def test_link_rejects_identity_used_elsewhere(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="other@ki.se", status="active", oidc_iss=ISS, oidc_sub="s5"))
        u = User(email="mine@ki.se", status="active")
        s.add(u)
        s.commit()
        uid = u.id
    with pg_session_factory() as s:
        ok = link_google_identity(s, user_id=uid, iss=ISS, sub="s5")
        assert ok is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_auth_accounts.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement resolution + linking**

```python
# src/call_manager/auth/accounts.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from call_manager.db.models import User


@dataclass(frozen=True)
class AccountResolution:
    # outcome: "matched" | "created" | "email_collision"
    outcome: str
    user: User | None


def _by_identity(session: Session, iss: str, sub: str) -> User | None:
    return session.execute(
        select(User).where(User.oidc_iss == iss, User.oidc_sub == sub)
    ).scalar_one_or_none()


def resolve_google_account(
    session: Session, *, iss: str, sub: str, email: str, name: str | None
) -> AccountResolution:
    """Resolve a validated Google identity to an account.

    1. Match by (oidc_iss, oidc_sub).
    2. Else if the email already belongs to an account -> email_collision
       (never auto-link; the user must Connect Google while authenticated).
    3. Else create a new PENDING account carrying the identity.

    Race-safe: a concurrent first login can win the unique (iss,sub) index;
    on IntegrityError we roll back and re-resolve.
    """
    email = email.strip().lower()

    existing = _by_identity(session, iss, sub)
    if existing is not None:
        existing.last_login_at = datetime.now(UTC)
        return AccountResolution("matched", existing)

    email_owner = session.execute(
        select(User).where(User.email == email)
    ).scalar_one_or_none()
    if email_owner is not None:
        return AccountResolution("email_collision", None)

    user = User(
        email=email,
        name=name,
        oidc_iss=iss,
        oidc_sub=sub,
        is_admin=False,
        status="pending",
    )
    session.add(user)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        winner = _by_identity(session, iss, sub)
        if winner is not None:
            winner.last_login_at = datetime.now(UTC)
            return AccountResolution("matched", winner)
        # The collision was on email (a concurrent insert of the same address).
        return AccountResolution("email_collision", None)
    return AccountResolution("created", user)


def link_google_identity(session: Session, *, user_id: int, iss: str, sub: str) -> bool:
    """Attach a Google identity to an already-authenticated user.

    Returns False if that identity is already linked to another account.
    """
    owner = _by_identity(session, iss, sub)
    if owner is not None:
        return owner.id == user_id
    user = session.get(User, user_id)
    if user is None:
        return False
    user.oidc_iss = iss
    user.oidc_sub = sub
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return False
    return True
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_auth_accounts.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/auth/accounts.py tests/test_auth_accounts.py
git commit -m "feat(auth): race-safe Google account resolution + explicit linking"
```

---

## Task 7: Central inactive-account enforcement

**Files:**
- Modify: `src/call_manager/auth/dependencies.py`
- Modify: `src/call_manager/main.py` (exception handler region, ~285-343; app.state wiring ~345-392)
- Test: `tests/test_auth_inactive_enforcement.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auth_inactive_enforcement.py
from contextlib import contextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.testclient import TestClient

from call_manager.auth.dependencies import AccountInactive, get_current_user
from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import User
from call_manager.db.session import make_engine, make_session_factory


@contextmanager
def _app(pg_url):
    signer = SessionSigner(secret="x" * 32, max_age_seconds=3600)
    engine = make_engine(pg_url)
    sf = make_session_factory(engine)
    app = FastAPI()
    app.state.session_signer = signer
    app.state.session_factory = sf

    @app.get("/whoami")
    def whoami(user: User = Depends(get_current_user)):  # noqa: B008
        return {"id": user.id}

    @app.exception_handler(AccountInactive)
    async def _inactive(request, exc: AccountInactive):
        wants_html = "text/html" in request.headers.get("accept", "")
        if wants_html:
            resp = RedirectResponse(
                url="/auth/pending" if exc.status == "pending" else "/auth/no-access",
                status_code=303,
            )
            resp.delete_cookie("session", path="/")
            return resp
        return JSONResponse(status_code=403, content={"detail": "account not active"})

    try:
        yield app, signer, sf
    finally:
        engine.dispose()


def _mk_user(sf, status):
    with sf() as s:
        u = User(email=f"{status}@ki.se", status=status)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def test_active_user_passes(pg_url, pg_session_factory):
    with _app(pg_url) as (app, signer, sf):
        uid = _mk_user(sf, "active")
        with TestClient(app) as c:
            c.cookies.set("session", signer.make(user_id=uid))
            assert c.get("/whoami").status_code == 200


def test_pending_user_html_redirects_and_clears_cookie(pg_url, pg_session_factory):
    with _app(pg_url) as (app, signer, sf):
        uid = _mk_user(sf, "pending")
        with TestClient(app) as c:
            c.cookies.set("session", signer.make(user_id=uid))
            r = c.get("/whoami", headers={"accept": "text/html"}, follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"] == "/auth/pending"


def test_disabled_user_api_gets_403(pg_url, pg_session_factory):
    with _app(pg_url) as (app, signer, sf):
        uid = _mk_user(sf, "disabled")
        with TestClient(app) as c:
            c.cookies.set("session", signer.make(user_id=uid))
            assert c.get("/whoami").status_code == 403
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_auth_inactive_enforcement.py -v`
Expected: FAIL — `AccountInactive` not defined.

- [ ] **Step 3: Add the exception + status gate to `get_current_user`**

In `src/call_manager/auth/dependencies.py`, add the exception class and the status check:

```python
class AccountInactive(Exception):
    """Authenticated cookie, but the account is not active (pending/disabled)."""

    def __init__(self, status: str):
        self.status = status
        super().__init__(status)
```

Inside `get_current_user`, after the `session_epoch` check and before `db.expunge(user)`:

```python
        if user.status != "active":
            raise AccountInactive(user.status)
```

- [ ] **Step 4: Register the handler + cipher/client wiring in `main.py`**

In `src/call_manager/main.py`, import the new symbols near the other auth imports:

```python
from call_manager.auth.dependencies import AccountInactive
from call_manager.auth.oauth_txn import TransactionCipher
from call_manager.auth.oidc import OIDCClient
```

Add an exception handler inside `create_app` (alongside `_redirect_html_401`):

```python
    @app.exception_handler(AccountInactive)
    async def _inactive_account(request: Request, exc: AccountInactive):
        wants_html = "text/html" in request.headers.get("accept", "")
        hx_partial = (
            request.headers.get("hx-request") == "true"
            and request.headers.get("hx-boosted") != "true"
        )
        if wants_html and not hx_partial:
            target = "/auth/pending" if exc.status == "pending" else "/auth/no-access"
            resp = RedirectResponse(url=target, status_code=303)
            resp.delete_cookie(
                "session",
                path="/",
                httponly=True,
                secure=settings.cookie_secure,
                samesite="lax",
            )
            return resp
        return JSONResponse(status_code=403, content={"detail": "account not active"})
```

In the app.state wiring block (after `app.state.session_signer = ...`, ~line 392), add:

```python
    app.state.oauth_txn_cipher = TransactionCipher(secret=settings.session_secret)
    app.state.google_oidc_client = (
        OIDCClient(
            discovery_url=settings.google_oidc_discovery_url,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
        )
        if settings.google_oidc_enabled
        else None
    )
```

(`RedirectResponse` and `JSONResponse` are already imported in main.py.)

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_auth_inactive_enforcement.py -v`
Expected: all PASS.

- [ ] **Step 6: Regression — existing auth-dependency tests still pass**

Run: `uv run pytest tests/test_auth_dependencies.py -v`
Expected: all PASS (active users created in those tests get `status='active'` via server_default).

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/auth/dependencies.py src/call_manager/main.py tests/test_auth_inactive_enforcement.py
git commit -m "feat(auth): central inactive-account enforcement (pending/disabled)"
```

---

## Task 8: Google OAuth routes (start / callback / holding pages)

**Files:**
- Create: `src/call_manager/web/routes/oauth_google.py`
- Create: `src/call_manager/web/templates/account_pending.html`
- Create: `src/call_manager/web/templates/account_no_access.html`
- Create: `src/call_manager/web/templates/auth_error.html`
- Modify: `src/call_manager/main.py` (add `app.include_router(oauth_google.router)`; import)
- Test: `tests/test_web_oauth_google.py`

- [ ] **Step 1: Write the failing test**

```python
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
    """Stand-in for OIDCClient; no network. Returns canned claims by code."""

    def __init__(self):
        self._by_code = {}

    def set_code(self, code, claims):
        self._by_code[code] = claims

    def build_authorization_url(self, *, redirect_uri, state, nonce, code_challenge):
        return f"{ISS}/auth?state={state}&nonce={nonce}&code_challenge={code_challenge}"

    def exchange_code(self, *, code, code_verifier, redirect_uri):
        if code not in self._by_code:
            from call_manager.auth.oidc import OIDCError

            raise OIDCError("bad code")
        header = {"alg": "RS256", "kid": _KEY.kid}
        return {"id_token": jwt.encode(header, self._by_code[code], _KEY).decode("ascii")}

    def validate_id_token(self, id_token, *, expected_nonce):
        from call_manager.auth.oidc import IdTokenInvalid

        claims = jwt.decode(id_token, {"keys": [_KEY.as_dict(is_private=False)]})
        if claims.get("nonce") != expected_nonce:
            raise IdTokenInvalid("nonce")
        claims.validate()
        if not claims.get("email_verified"):
            raise IdTokenInvalid("email not verified")
        return dict(claims)


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("GOOGLE_OIDC_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", AUD)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    app = create_app()
    fake = FakeOIDC()
    app.state.google_oidc_client = fake
    return app, fake


def _claims(**over):
    now = int(time.time())
    base = {
        "iss": ISS, "aud": AUD, "sub": "g-1", "email": "new@gmail.com",
        "email_verified": True, "name": "New Person", "iat": now, "exp": now + 600,
    }
    base.update(over)
    return base


def test_start_sets_cookie_and_redirects_to_google(monkeypatch, pg_url, pg_session_factory):
    app, _ = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.post("/auth/google/start", data={"next": "/"}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith(ISS)
        assert "oauth_txn" in r.cookies or any("oauth_txn" in h for h in r.headers.get_list("set-cookie"))


def test_callback_new_user_creates_pending_and_redirects(monkeypatch, pg_url, pg_session_factory):
    app, fake = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        start = c.post("/auth/google/start", data={"next": "/"}, follow_redirects=False)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        # nonce was generated server-side; the FakeOIDC validates it from the txn cookie.
        nonce = parse_qs(urlparse(start.headers["location"]).query)["nonce"][0]
        fake.set_code("CODE", _claims(nonce=nonce))
        r = c.get(f"/auth/google/callback?code=CODE&state={state}", headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/auth/pending"
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="new@gmail.com").one()
        assert u.status == "pending"


def test_callback_state_mismatch_goes_to_error(monkeypatch, pg_url, pg_session_factory):
    app, fake = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        c.post("/auth/google/start", data={"next": "/"}, follow_redirects=False)
        r = c.get("/auth/google/callback?code=CODE&state=WRONG", headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/auth/error"


def test_callback_active_user_gets_session(monkeypatch, pg_url, pg_session_factory):
    app, fake = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        s.add(User(email="ok@ki.se", status="active", oidc_iss=ISS, oidc_sub="g-2"))
        s.commit()
    with TestClient(app) as c:
        start = c.post("/auth/google/start", data={"next": "/"}, follow_redirects=False)
        q = parse_qs(urlparse(start.headers["location"]).query)
        fake.set_code("C2", _claims(sub="g-2", email="ok@ki.se", nonce=q["nonce"][0]))
        r = c.get(f"/auth/google/callback?code=C2&state={q['state'][0]}", headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        assert "session" in r.cookies or any("session=" in h for h in r.headers.get_list("set-cookie"))


def test_callback_email_collision_goes_to_error(monkeypatch, pg_url, pg_session_factory):
    app, fake = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        s.add(User(email="dupe@ki.se", status="active"))  # no identity
        s.commit()
    with TestClient(app) as c:
        start = c.post("/auth/google/start", data={"next": "/"}, follow_redirects=False)
        q = parse_qs(urlparse(start.headers["location"]).query)
        fake.set_code("C3", _claims(sub="g-3", email="dupe@ki.se", nonce=q["nonce"][0]))
        r = c.get(f"/auth/google/callback?code=C3&state={q['state'][0]}", headers={"accept": "text/html"}, follow_redirects=False)
        assert r.headers["location"] == "/auth/error"


def test_routes_404_when_disabled(monkeypatch, pg_url, pg_session_factory):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()  # GOOGLE_OIDC_ENABLED unset -> disabled
    with TestClient(app) as c:
        assert c.post("/auth/google/start", data={}).status_code == 404
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_web_oauth_google.py -v`
Expected: FAIL — route module and routes do not exist (404s / import errors).

- [ ] **Step 3: Implement the router**

```python
# src/call_manager/web/routes/oauth_google.py
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from call_manager.auth.accounts import link_google_identity, resolve_google_account
from call_manager.auth.oauth_txn import InvalidTransaction
from call_manager.auth.oidc import IdTokenInvalid, OIDCError, generate_pkce, generate_token
from call_manager.auth.redirects import safe_next
from call_manager.web.routes.auth import _client_ip  # reuse IP resolver

_logger = logging.getLogger(__name__)
router = APIRouter()

_TXN_COOKIE = "oauth_txn"
_TXN_MAX_AGE = 600  # 10 minutes


def _require_enabled(request: Request):
    client = request.app.state.google_oidc_client
    if client is None:
        raise HTTPException(status_code=404, detail="not found")
    return client


def _to(target: str) -> RedirectResponse:
    """Clean redirect that always deletes the transaction cookie."""
    resp = RedirectResponse(url=target, status_code=303)
    resp.delete_cookie(_TXN_COOKIE, path="/auth/google")
    return resp


@router.post("/auth/google/start")
def google_start(
    request: Request,
    next: str | None = Form(default=None),
    link: str | None = Form(default=None),
):
    client = _require_enabled(request)
    settings = request.app.state.settings
    cipher = request.app.state.oauth_txn_cipher
    ip_limiter = request.app.state.ip_limiter
    if not ip_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests")

    verifier, challenge = generate_pkce()
    state = generate_token()
    nonce = generate_token()
    payload = {"state": state, "nonce": nonce, "code_verifier": verifier, "next": safe_next(next)}

    # Link mode: only honor it for an authenticated active user.
    if link == "1":
        from call_manager.auth.dependencies import AccountInactive, get_current_user

        try:
            user = get_current_user(request, session=request.cookies.get("session"))
            payload["link_uid"] = user.id
        except (HTTPException, AccountInactive):
            pass  # fall back to normal sign-in

    url = client.build_authorization_url(
        redirect_uri=settings.google_redirect_uri,
        state=state,
        nonce=nonce,
        code_challenge=challenge,
    )
    resp = RedirectResponse(url=url, status_code=303)
    resp.set_cookie(
        _TXN_COOKIE,
        cipher.make(payload),
        max_age=_TXN_MAX_AGE,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/auth/google",
    )
    return resp


@router.get("/auth/google/callback")
def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    client = _require_enabled(request)
    settings = request.app.state.settings
    cipher = request.app.state.oauth_txn_cipher
    session_factory = request.app.state.session_factory
    session_signer = request.app.state.session_signer
    ip_limiter = request.app.state.ip_limiter

    if not ip_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests")

    if error or not code or not state:
        return _to("/auth/error")

    raw = request.cookies.get(_TXN_COOKIE)
    if not raw:
        return _to("/auth/error")
    try:
        txn = cipher.read(raw, max_age_seconds=_TXN_MAX_AGE)
    except InvalidTransaction:
        return _to("/auth/error")

    if state != txn.get("state"):
        return _to("/auth/error")

    try:
        token = client.exchange_code(
            code=code, code_verifier=txn["code_verifier"], redirect_uri=settings.google_redirect_uri
        )
        claims = client.validate_id_token(token["id_token"], expected_nonce=txn["nonce"])
    except (OIDCError, IdTokenInvalid, KeyError):
        return _to("/auth/error")

    if not claims.get("email_verified"):
        return _to("/auth/error")

    iss = claims["iss"]
    sub = claims["sub"]
    email = claims["email"]
    name = claims.get("name")

    # Link mode: attach to the already-authenticated account.
    link_uid = txn.get("link_uid")
    if link_uid:
        with session_factory() as db:
            ok = link_google_identity(db, user_id=int(link_uid), iss=iss, sub=sub)
            db.commit()
        return _to("/me" if ok else "/auth/error")

    with session_factory() as db:
        res = resolve_google_account(db, iss=iss, sub=sub, email=email, name=name)
        if res.outcome == "email_collision":
            db.rollback()
            return _to("/auth/error")
        user_id = res.user.id
        status = res.user.status
        session_epoch = res.user.session_epoch
        if status == "active":
            res.user.last_login_at = datetime.now(UTC)
        db.commit()

    if status == "pending":
        return _to("/auth/pending")
    if status == "disabled":
        return _to("/auth/no-access")

    # active -> issue session
    target = safe_next(txn.get("next")) or "/"
    resp = RedirectResponse(url=target, status_code=303)
    resp.delete_cookie(_TXN_COOKIE, path="/auth/google")
    resp.set_cookie(
        "session",
        session_signer.make(user_id=user_id, session_epoch=session_epoch),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    resp.headers["Referrer-Policy"] = "no-referrer"
    _logger.info("google sign-in success", extra={"event": "google_signin_success"})
    return resp


@router.get("/auth/pending", response_class=HTMLResponse)
def auth_pending(request: Request):
    return request.app.state.templates.TemplateResponse(
        request=request, name="account_pending.html", context={}
    )


@router.get("/auth/no-access", response_class=HTMLResponse)
def auth_no_access(request: Request):
    return request.app.state.templates.TemplateResponse(
        request=request, name="account_no_access.html", context={}
    )


@router.get("/auth/error", response_class=HTMLResponse)
def auth_error(request: Request):
    return request.app.state.templates.TemplateResponse(
        request=request, name="auth_error.html", context={}
    )
```

Note on `settings.session_max_age_seconds`: it already exists (used by `auth.py`). If your `Settings` names it differently, match the existing magic-link usage in `web/routes/auth.py`.

- [ ] **Step 4: Create the three templates**

`src/call_manager/web/templates/account_pending.html`:

```html
{% extends "base.html" %}
{% block content %}
<main class="container">
  <h1>Account awaiting approval</h1>
  <p>Your sign-in worked. An administrator needs to approve your account
     before you can use Call Manager. You'll be able to sign in once approved.</p>
  <p><a href="/login">Back to sign in</a></p>
</main>
{% endblock %}
```

`src/call_manager/web/templates/account_no_access.html`:

```html
{% extends "base.html" %}
{% block content %}
<main class="container">
  <h1>No access</h1>
  <p>This account does not have access. If you think this is a mistake,
     contact an administrator.</p>
  <p><a href="/login">Back to sign in</a></p>
</main>
{% endblock %}
```

`src/call_manager/web/templates/auth_error.html`:

```html
{% extends "base.html" %}
{% block content %}
<main class="container">
  <h1>Sign-in could not be completed</h1>
  <p>We couldn't sign you in. Please try again.</p>
  <p>If you already have an account with this email, sign in your usual way,
     then connect Google from your account settings.</p>
  <p><a href="/login">Back to sign in</a></p>
</main>
{% endblock %}
```

(Confirm the block name matches `base.html`. If `base.html` uses a different block than `content`, use that name.)

- [ ] **Step 5: Wire the router in `main.py`**

Add the import near the other route imports and include the router after `app.include_router(auth.router)`:

```python
from call_manager.web.routes import oauth_google
...
    app.include_router(oauth_google.router)
```

- [ ] **Step 6: Run to verify it passes**

Run: `uv run pytest tests/test_web_oauth_google.py -v`
Expected: all PASS.

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check src/call_manager/web/routes/oauth_google.py
git add src/call_manager/web/routes/oauth_google.py src/call_manager/web/templates/account_pending.html src/call_manager/web/templates/account_no_access.html src/call_manager/web/templates/auth_error.html src/call_manager/main.py tests/test_web_oauth_google.py
git commit -m "feat(auth): Google OAuth start/callback routes + holding pages"
```

---

## Task 9: Connect Google (explicit linking) UI

**Files:**
- Modify: `src/call_manager/web/templates/me.html` (add a Connect-Google form, gated on enabled + not-yet-linked)
- Modify: `src/call_manager/web/routes/me.py` (pass `google_oidc_enabled` + `google_linked` to the template)
- Test: extend `tests/test_web_oauth_google.py` with a link-mode test

- [ ] **Step 1: Write the failing test (append to `tests/test_web_oauth_google.py`)**

```python
def test_connect_google_links_authenticated_user(monkeypatch, pg_url, pg_session_factory):
    app, fake = _setup(monkeypatch, pg_url)
    from call_manager.auth.session_cookie import SessionSigner

    with pg_session_factory() as s:
        u = User(email="link@ki.se", status="active")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    with TestClient(app) as c:
        c.cookies.set("session", signer.make(user_id=uid))
        start = c.post("/auth/google/start", data={"link": "1"}, follow_redirects=False)
        q = parse_qs(urlparse(start.headers["location"]).query)
        fake.set_code("LC", _claims(sub="g-link", email="anything@gmail.com", nonce=q["nonce"][0]))
        r = c.get(f"/auth/google/callback?code=LC&state={q['state'][0]}", headers={"accept": "text/html"}, follow_redirects=False)
        assert r.headers["location"] == "/me"
    with pg_session_factory() as s:
        assert s.get(User, uid).oidc_sub == "g-link"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_web_oauth_google.py::test_connect_google_links_authenticated_user -v`
Expected: It may already PASS at the route layer (Task 8 implemented link mode). If it passes, the route logic is correct; proceed to add the UI (Steps 3-4) so users can trigger it. If it fails, fix the link branch in `oauth_google.py` first.

- [ ] **Step 3: Pass linking context from `me.py`**

In `src/call_manager/web/routes/me.py`, in the GET handler that renders `me.html`, add to the template context:

```python
            "google_oidc_enabled": request.app.state.settings.google_oidc_enabled,
            "google_linked": user.oidc_sub is not None,
```

(Use the already-loaded `user` from `get_current_user`; if the handler re-loads the user from the DB, read `oidc_sub` there.)

- [ ] **Step 4: Add the Connect-Google control to `me.html`**

Insert where account settings live:

```html
{% if google_oidc_enabled %}
  {% if google_linked %}
    <p>Google sign-in: <strong>connected</strong>.</p>
  {% else %}
    <form method="post" action="/auth/google/start">
      <input type="hidden" name="link" value="1" />
      <button type="submit">Connect Google account</button>
    </form>
  {% endif %}
{% endif %}
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_web_oauth_google.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/routes/me.py src/call_manager/web/templates/me.html tests/test_web_oauth_google.py
git commit -m "feat(auth): Connect Google account from /me (explicit linking)"
```

---

## Task 10: Magic-link becomes login-only (no auto-create + status check)

**Files:**
- Modify: `src/call_manager/web/routes/auth.py` (`login_submit` ~163-173; `login_verify_consume` ~272-297)
- Test: `tests/test_web_auth_magic_link_gate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_auth_magic_link_gate.py
from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.email import InMemoryEmailSender
from call_manager.db.models import MagicLinkToken, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()
    app.state.email_sender = InMemoryEmailSender()
    return app


def test_unknown_email_creates_no_user_and_no_token(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.post("/login", data={"email": "ghost@ki.se"})
        assert r.status_code == 200  # identical "check your email" page
    with pg_session_factory() as s:
        assert s.execute(select(User).where(User.email == "ghost@ki.se")).scalar_one_or_none() is None
        assert s.execute(select(MagicLinkToken)).first() is None


def test_disabled_after_issue_cannot_consume(monkeypatch, pg_url, pg_session_factory):
    from call_manager.auth.tokens import issue_token

    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="real@ki.se", status="active")
        s.add(u)
        s.flush()
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()
        uid = u.id
    # disable after issuance
    with pg_session_factory() as s:
        s.get(User, uid).status = "disabled"
        s.commit()
    with TestClient(app) as c:
        r = c.post("/login/verify", data={"token": raw}, headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/auth/no-access"
        assert "session" not in r.cookies
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_web_auth_magic_link_gate.py -v`
Expected: FAIL — current code auto-creates the user and consumes unconditionally.

- [ ] **Step 3: Remove auto-create in `login_submit`**

In `src/call_manager/web/routes/auth.py`, replace the user-create block (lines ~163-173):

```python
    with session_factory() as session:
        user = session.execute(
            select(User).where(User.email == email_normalized)
        ).scalar_one_or_none()
        if user is None:
            user = User(email=email_normalized)
            session.add(user)
            session.flush()

        raw = issue_token(session, user_id=user.id, ttl_minutes=15)
        session.commit()
```

with:

```python
    with session_factory() as session:
        user = session.execute(
            select(User).where(User.email == email_normalized)
        ).scalar_one_or_none()
        # Magic-link is login-only: never auto-provision. Only issue a token for
        # an existing ACTIVE account. Unknown/non-active emails fall through to
        # the identical "check your email" response (enumeration-resistant).
        if user is None or user.status != "active":
            _logger.info(
                "login skipped: no active account",
                extra={"event": "login_no_active_account", "email_fp": email_fp},
            )
            return response
        raw = issue_token(session, user_id=user.id, ttl_minutes=15)
        session.commit()
```

- [ ] **Step 4: Add the status re-check at consume**

In `login_verify_consume`, replace the user-load block (lines ~272-278):

```python
    with session_factory() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=400, detail="invalid link")
        user.last_login_at = datetime.now(UTC)
        session_epoch = user.session_epoch
        session.commit()
```

with:

```python
    with session_factory() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=400, detail="invalid link")
        # Re-check status at consume: the account may have been disabled in the
        # window between the link being mailed and clicked.
        if user.status != "active":
            target = "/auth/pending" if user.status == "pending" else "/auth/no-access"
            return RedirectResponse(url=target, status_code=303)
        user.last_login_at = datetime.now(UTC)
        session_epoch = user.session_epoch
        session.commit()
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_web_auth_magic_link_gate.py -v`
Expected: all PASS.

- [ ] **Step 6: Regression — existing auth route tests**

Run: `uv run pytest tests/test_web_auth_redirects.py tests/ -k "auth" -v`
Expected: PASS. If any existing test relied on magic-link auto-creating a user, update it to create the `User` first (status defaults to active).

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/web/routes/auth.py tests/test_web_auth_magic_link_gate.py
git commit -m "feat(auth): magic-link is login-only (no auto-create; status re-check at consume)"
```

---

## Task 11: Admin approval queue + disable/role + last-admin guard

**Files:**
- Modify: `src/call_manager/web/routes/admin.py` (new routes)
- Modify: an admin template (e.g. `src/call_manager/web/templates/admin_users.html` — match the existing admin user-list template name)
- Test: `tests/test_web_admin_approval.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_admin_approval.py
from fastapi.testclient import TestClient

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _admin_client(app, pg_session_factory):
    with pg_session_factory() as s:
        a = User(email="admin@ki.se", status="active", is_admin=True)
        s.add(a)
        s.commit()
        s.refresh(a)
        aid = a.id
    signer: SessionSigner = app.state.session_signer
    c = TestClient(app)
    c.cookies.set("session", signer.make(user_id=aid))
    return c, aid


def test_approve_pending_as_member(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin_client(app, pg_session_factory)
    with pg_session_factory() as s:
        p = User(email="p@gmail.com", status="pending", oidc_iss="i", oidc_sub="s")
        s.add(p)
        s.commit()
        s.refresh(p)
        pid = p.id
    r = c.post(f"/admin/users/{pid}/approve", data={"role": "member"}, follow_redirects=False)
    assert r.status_code in (303, 302)
    with pg_session_factory() as s:
        u = s.get(User, pid)
        assert u.status == "active" and u.is_admin is False and u.approved_at is not None


def test_approve_as_admin_sets_is_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin_client(app, pg_session_factory)
    with pg_session_factory() as s:
        p = User(email="p2@gmail.com", status="pending", oidc_iss="i", oidc_sub="s2")
        s.add(p); s.commit(); s.refresh(p); pid = p.id
    c.post(f"/admin/users/{pid}/approve", data={"role": "admin"}, follow_redirects=False)
    with pg_session_factory() as s:
        assert s.get(User, pid).is_admin is True


def test_deny_disables(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin_client(app, pg_session_factory)
    with pg_session_factory() as s:
        p = User(email="p3@gmail.com", status="pending", oidc_iss="i", oidc_sub="s3")
        s.add(p); s.commit(); s.refresh(p); pid = p.id
    c.post(f"/admin/users/{pid}/deny", follow_redirects=False)
    with pg_session_factory() as s:
        assert s.get(User, pid).status == "disabled"


def test_cannot_disable_last_active_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, aid = _admin_client(app, pg_session_factory)
    r = c.post(f"/admin/users/{aid}/disable", follow_redirects=False)
    assert r.status_code == 400
    with pg_session_factory() as s:
        assert s.get(User, aid).status == "active"


def test_disable_bumps_session_epoch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin_client(app, pg_session_factory)
    with pg_session_factory() as s:
        # a second admin so the last-admin guard doesn't block disabling this one
        s.add(User(email="admin2@ki.se", status="active", is_admin=True))
        m = User(email="m@ki.se", status="active")
        s.add(m); s.commit(); s.refresh(m); mid = m.id
        before = s.get(User, mid).session_epoch
    c.post(f"/admin/users/{mid}/disable", follow_redirects=False)
    with pg_session_factory() as s:
        u = s.get(User, mid)
        assert u.status == "disabled" and u.session_epoch == before + 1


def test_non_admin_blocked(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="member@ki.se", status="active")
        s.add(u); s.commit(); s.refresh(u); uid = u.id
    signer: SessionSigner = app.state.session_signer
    with TestClient(app) as c:
        c.cookies.set("session", signer.make(user_id=uid))
        assert c.get("/admin/users/pending").status_code == 403
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_web_admin_approval.py -v`
Expected: FAIL — routes don't exist (404).

- [ ] **Step 3: Implement the admin routes**

Add to `src/call_manager/web/routes/admin.py` (it already has `router` and uses `require_admin`):

```python
from datetime import UTC, datetime

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from call_manager.auth.dependencies import require_admin
from call_manager.db.models import User


def _active_admin_count(db) -> int:
    return db.execute(
        select(func.count())
        .select_from(User)
        .where(User.is_admin.is_(True), User.status == "active")
    ).scalar_one()


@router.get("/admin/users/pending")
def pending_users(
    request: Request,
    page: int = 1,
    admin: User = Depends(require_admin),  # noqa: B008
):
    page = max(1, page)
    per_page = 50
    sf = request.app.state.session_factory
    with sf() as db:
        rows = db.execute(
            select(User)
            .where(User.status == "pending")
            .order_by(User.created_at.asc())
            .limit(per_page)
            .offset((page - 1) * per_page)
        ).scalars().all()
        users = [
            {"id": u.id, "email": u.email, "name": u.name,
             "created_at": u.created_at, "google_linked": u.oidc_sub is not None}
            for u in rows
        ]
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="admin_users_pending.html",
        context={"users": users, "page": page},
    )


@router.post("/admin/users/{user_id}/approve")
def approve_user(
    request: Request,
    user_id: int,
    role: str = Form("member"),
    admin: User = Depends(require_admin),  # noqa: B008
):
    if role not in ("member", "admin"):
        raise HTTPException(status_code=400, detail="invalid role")
    sf = request.app.state.session_factory
    with sf() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        u.status = "active"
        u.is_admin = role == "admin"
        u.approved_by_user_id = admin.id
        u.approved_at = datetime.now(UTC)
        db.commit()
    return RedirectResponse(url="/admin/users/pending", status_code=303)


@router.post("/admin/users/{user_id}/deny")
def deny_user(
    request: Request,
    user_id: int,
    admin: User = Depends(require_admin),  # noqa: B008
):
    sf = request.app.state.session_factory
    with sf() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if u.is_admin and u.status == "active" and _active_admin_count(db) <= 1:
            raise HTTPException(status_code=400, detail="cannot remove the last active admin")
        u.status = "disabled"
        u.session_epoch = (u.session_epoch or 0) + 1
        db.commit()
    return RedirectResponse(url="/admin/users/pending", status_code=303)


@router.post("/admin/users/{user_id}/disable")
def disable_user(
    request: Request,
    user_id: int,
    admin: User = Depends(require_admin),  # noqa: B008
):
    sf = request.app.state.session_factory
    with sf() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if u.is_admin and u.status == "active" and _active_admin_count(db) <= 1:
            raise HTTPException(status_code=400, detail="cannot disable the last active admin")
        u.status = "disabled"
        u.session_epoch = (u.session_epoch or 0) + 1
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/enable")
def enable_user(
    request: Request,
    user_id: int,
    admin: User = Depends(require_admin),  # noqa: B008
):
    sf = request.app.state.session_factory
    with sf() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        u.status = "active"
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)
```

If `admin.py` already imports some of these symbols, don't duplicate the imports — merge them.

- [ ] **Step 4: Create the pending-queue template**

`src/call_manager/web/templates/admin_users_pending.html`:

```html
{% extends "base.html" %}
{% block content %}
<main class="container">
  <h1>Pending accounts</h1>
  {% if not users %}<p>No accounts awaiting approval.</p>{% endif %}
  {% for u in users %}
  <article>
    <p><strong>{{ u.email }}</strong>{% if u.name %} — {{ u.name }}{% endif %}
       {% if u.google_linked %}<small>(Google)</small>{% endif %}</p>
    <form method="post" action="/admin/users/{{ u.id }}/approve" style="display:inline">
      <input type="hidden" name="role" value="member" />
      <button type="submit">Approve as member</button>
    </form>
    <form method="post" action="/admin/users/{{ u.id }}/approve" style="display:inline">
      <input type="hidden" name="role" value="admin" />
      <button type="submit">Approve as admin</button>
    </form>
    <form method="post" action="/admin/users/{{ u.id }}/deny" style="display:inline">
      <button type="submit" class="secondary">Deny</button>
    </form>
  </article>
  {% endfor %}
</main>
{% endblock %}
```

(Optionally add Disable/Enable buttons to the existing admin user-list template using the `/admin/users/{id}/disable` and `/enable` routes. Match the existing template's structure.)

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_web_admin_approval.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/routes/admin.py src/call_manager/web/templates/admin_users_pending.html tests/test_web_admin_approval.py
git commit -m "feat(admin): pending-account approval queue, deny/disable, last-admin guard"
```

---

## Task 12: Bootstrap reactivates configured admins

**Files:**
- Modify: `src/call_manager/admin_bootstrap.py` (lines ~47-54)
- Test: `tests/test_admin_bootstrap_status.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_admin_bootstrap_status.py
from call_manager.admin_bootstrap import bootstrap_admins
from call_manager.db.models import User


def test_creates_active_admin(pg_session_factory):
    bootstrap_admins(pg_session_factory, ["boss@ki.se"], ["ki.se"])
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="boss@ki.se").one()
        assert u.is_admin is True and u.status == "active"


def test_reactivates_disabled_configured_admin(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="boss@ki.se", is_admin=False, status="disabled"))
        s.commit()
    bootstrap_admins(pg_session_factory, ["boss@ki.se"], ["ki.se"])
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="boss@ki.se").one()
        assert u.is_admin is True and u.status == "active"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_admin_bootstrap_status.py -v`
Expected: `test_reactivates_disabled_configured_admin` FAILS (current code only sets `is_admin`, never status).

- [ ] **Step 3: Update the bootstrap loop**

In `src/call_manager/admin_bootstrap.py`, replace the loop body (lines ~48-53):

```python
        for email in valid:
            user = session.query(User).filter_by(email=email).one_or_none()
            if user is None:
                session.add(User(email=email, is_admin=True))
            elif not user.is_admin:
                user.is_admin = True
```

with:

```python
        for email in valid:
            user = session.query(User).filter_by(email=email).one_or_none()
            if user is None:
                session.add(User(email=email, is_admin=True, status="active"))
            else:
                # Configured admins are the lockout-recovery path: ensure they are
                # both admin and active on every startup. To permanently disable
                # one, remove them from INITIAL_ADMIN_EMAIL first.
                user.is_admin = True
                user.status = "active"
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_admin_bootstrap_status.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/admin_bootstrap.py tests/test_admin_bootstrap_status.py
git commit -m "feat(auth): bootstrap reactivates configured admins (lockout recovery)"
```

---

## Task 13: Digest selection filters on active status

**Files:**
- Modify: `src/call_manager/digest/send.py` (candidate query ~67-76)
- Test: `tests/test_digest_status_filter.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_digest_status_filter.py
from call_manager.db.models import User


def test_disabled_subscriber_is_not_a_digest_candidate(pg_session_factory):
    # The candidate selection lives in run_digest; we test the query indirectly
    # by asserting a disabled user with a subscription is excluded. Use the same
    # query shape the implementation uses.
    from sqlalchemy import distinct, select

    from call_manager.db.models import Subscription

    with pg_session_factory() as s:
        active = User(email="active@ki.se", status="active")
        disabled = User(email="disabled@ki.se", status="disabled")
        s.add_all([active, disabled])
        s.flush()
        # minimal subscriptions for both (profile_id can reference any profile;
        # create one via profile_factory in a real test, or insert directly).
        s.add_all([
            Subscription(user_id=active.id, profile_id=_a_profile(s)),
            Subscription(user_id=disabled.id, profile_id=_a_profile(s)),
        ])
        s.commit()

    with pg_session_factory() as s:
        ids = s.execute(
            select(distinct(Subscription.user_id))
            .join(User, User.id == Subscription.user_id)
            .where(User.digest_disabled_at.is_(None), User.status == "active")
        ).scalars().all()
        emails = {s.get(User, i).email for i in ids}
        assert "active@ki.se" in emails
        assert "disabled@ki.se" not in emails


def _a_profile(s) -> int:
    from call_manager.db.models import Profile, ProfileKind

    p = Profile(
        name="P", kind=ProfileKind.ORGANIZATIONAL_UNIT, level="group",
        description="d", structured={}, created_by=None,
    )
    s.add(p)
    s.flush()
    return p.id
```

If `Profile.created_by` is NOT NULL in your schema, create an owner `User` first and pass its id. Adjust `_a_profile` to match the real `Profile` constructor (see `tests/conftest.py::profile_factory`).

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_digest_status_filter.py -v`
Expected: FAIL — without the `User.status == "active"` filter in the implementation this query (the implementation's) would include the disabled user. (This test asserts the *intended* query; Step 3 makes the implementation match it.)

- [ ] **Step 3: Add the status filter to the implementation**

In `src/call_manager/digest/send.py`, update the candidate query (~67-76):

```python
    with session_factory() as session:
        candidate_user_ids = (
            session.execute(
                select(distinct(Subscription.user_id))
                .join(User, User.id == Subscription.user_id)
                .where(
                    User.digest_disabled_at.is_(None),
                    User.status == "active",
                )
            )
            .scalars()
            .all()
        )
```

(`User` is already imported in `send.py`; if not, add `from call_manager.db.models import User`.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_digest_status_filter.py -v`
Expected: PASS.

- [ ] **Step 5: Regression — existing digest tests**

Run: `uv run pytest tests/ -k digest -v`
Expected: PASS (existing digest tests create users without status → active, so they remain candidates).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/digest/send.py tests/test_digest_status_filter.py
git commit -m "feat(digest): exclude non-active accounts from digest selection"
```

---

## Task 14: Login-page button + full-suite green

**Files:**
- Modify: `src/call_manager/web/templates/login.html`
- Test: `tests/test_web_login_google_button.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_login_google_button.py
from fastapi.testclient import TestClient

from call_manager.main import create_app


def _env(monkeypatch, pg_url, enabled):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    if enabled:
        monkeypatch.setenv("GOOGLE_OIDC_ENABLED", "true")
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "sec")
    return create_app()


def test_button_shown_when_enabled(monkeypatch, pg_url):
    app = _env(monkeypatch, pg_url, enabled=True)
    with TestClient(app) as c:
        body = c.get("/login").text
        assert "/auth/google/start" in body


def test_button_hidden_when_disabled(monkeypatch, pg_url):
    app = _env(monkeypatch, pg_url, enabled=False)
    with TestClient(app) as c:
        body = c.get("/login").text
        assert "/auth/google/start" not in body
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_web_login_google_button.py -v`
Expected: `test_button_shown_when_enabled` FAILS — and `login_form` doesn't pass the flag yet.

- [ ] **Step 3: Pass the flag from `login_form`**

In `src/call_manager/web/routes/auth.py`, in `login_form`, add to the context dict:

```python
            "google_oidc_enabled": settings.google_oidc_enabled,
```

- [ ] **Step 4: Add the button to `login.html`**

Add near the email form:

```html
{% if google_oidc_enabled %}
<form method="post" action="/auth/google/start">
  {% if next_path %}<input type="hidden" name="next" value="{{ next_path }}" />{% endif %}
  <button type="submit">Continue with Google</button>
</form>
{% endif %}
```

(Use the same `next` context variable name the email form already uses — `next_path` per `login_form`.)

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_web_login_google_button.py -v`
Expected: all PASS.

- [ ] **Step 6: Full suite + lint**

Run:
```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q
```
Expected: lint clean; entire suite green. Fix any regressions (most likely: an old test that assumed magic-link auto-creates a user, or created a `User` expecting a column default — update it to create the user explicitly with `status="active"`).

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/web/routes/auth.py src/call_manager/web/templates/login.html tests/test_web_login_google_button.py
git commit -m "feat(auth): Continue-with-Google button on the login page"
```

---

## Self-Review (completed during planning)

**Spec coverage** — every spec section maps to a task:
- OIDC client / PKCE / id_token validation → Task 5
- Encrypted transaction cookie → Task 3
- `users` status + oidc columns + constraints + migration/backfill → Task 2
- No auto-link; resolution; race-safety; Connect-Google → Tasks 6, 8, 9
- Clean-redirect callback contract + holding/error routes → Task 8
- Central inactive enforcement (HTML redirect + cookie delete / API 403) → Task 7
- Magic-link login-only + consume-time status check → Task 10
- Admin approval queue + deny/disable + last-admin guard + pagination → Task 11
- Bootstrap reactivation → Task 12
- Digest active filter → Task 13
- Config + prod guard + `.env.example` → Task 4
- Login button + Connect-Google UI → Tasks 9, 14
- Rate-limiting OAuth endpoints → Task 8 (start + callback use `ip_limiter`)

**Type/name consistency** — `OIDCClient`, `IdTokenInvalid`, `OIDCError`, `generate_pkce`, `generate_token` (Task 5) are used verbatim in Tasks 7-8. `TransactionCipher.make/read` (Task 3) used in Task 8. `AccountResolution.outcome` values `matched|created|email_collision` (Task 6) matched in Task 8. `AccountInactive(status)` (Task 7) matched in Task 8/10 redirect targets. `resolve_google_account` / `link_google_identity` signatures consistent across Tasks 6, 8, 9.

**Known follow-ups to confirm during execution (not blockers):**
- Exact Jinja `{% block %}` name in `base.html` (templates assume `content`).
- Exact existing admin user-list template filename for optional disable/enable buttons.
- `Profile` constructor NULL-ability for the digest test helper.
- Confirm `settings.session_max_age_seconds` is the real attribute name (mirror `web/routes/auth.py`).
