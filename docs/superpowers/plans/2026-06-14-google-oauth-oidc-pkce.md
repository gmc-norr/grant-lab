# Google OIDC + OAuth Authorization Code with PKCE — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let people sign in with Google via OIDC (Authorization Code + PKCE) behind an admin approval gate — self-service sign-in creates a `pending` account with no access until an admin approves it and sets member/admin; magic-link is kept but becomes login-only.

**Architecture:** A discovery-URL-driven OIDC client (Authlib Jose + httpx) drives a `/auth/google/start` → `/auth/google/callback` flow. Transaction state (PKCE verifier, state, nonce, next) rides in a short-lived **encrypted** cookie. Accounts are resolved only by the stable `(oidc_iss, oidc_sub)` pair (no email auto-linking). A new `users.status` column (`pending`/`active`/`disabled`) is enforced centrally in `get_current_user`, in admin transitions, and in digest selection. Admins approve/deny/disable/change-role via new admin routes.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x + Alembic + PostgreSQL 16, Jinja2/HTMX, Authlib, cryptography (Fernet), httpx, pytest + testcontainers.

**Spec:** [docs/superpowers/specs/2026-06-14-google-oauth-oidc-pkce-design.md](../specs/2026-06-14-google-oauth-oidc-pkce-design.md)

**Revision note:** This plan was revised after a Codex review of its first draft. Key changes: DB default is `pending` (not `active`) — fail closed; hardened id_token validation (RS256-only, essential claims, JWKS rotation); callback always sets `no-referrer` + deletes the txn cookie + redirects clean; admin transitions are state-restricted with a serialized last-admin guard; a concrete `/admin/users` management page; Connect-Google revalidates the initiating session; non-tautological digest test; timing-oracle residual risk documented.

**Conventions used below:**
- Run tests with `uv run pytest` and lint with `uv run ruff check` / `uv run ruff format`.
- Each task ends with a commit. Keep commits green.
- **`users.status` fails closed: the default is `pending`** at both the ORM and DB layer. A forgotten `User(...)` therefore has *no* access rather than silently being active. The only paths that create `active` users are (a) the migration backfill of pre-existing rows and (b) admin bootstrap, which sets it explicitly. **Any test that creates a user who must authenticate or act must pass `status="active"` explicitly** (Task 2 updates the shared fixtures; Task 14 greens the rest).

---

## Task 1: Add dependencies (Authlib, cryptography)

**Files:**
- Modify: `pyproject.toml` (dependencies array)

- [ ] **Step 1: Add the two dependencies**

In `pyproject.toml`, add to the `dependencies` array (next to `httpx`):

```toml
    "authlib>=1.3",
    "cryptography>=43.0",
```

- [ ] **Step 2: Sync the environment**

Run: `uv sync`
Expected: resolves and installs `authlib` and `cryptography`.

- [ ] **Step 3: Verify imports**

Run: `uv run python -c "import authlib.jose, cryptography.fernet; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add authlib + cryptography for Google OIDC"
```

---

## Task 2: `users` status + OIDC columns (model + migration + fixtures)

**Files:**
- Modify: `src/call_manager/db/models.py` (User class, lines ~35-75)
- Create: `alembic/versions/<rev>_users_status_and_oidc.py`
- Modify: `tests/conftest.py` (`profile_factory` — owner user must be active)
- Test: `tests/test_user_status_oidc_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_user_status_oidc_model.py
import pytest
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import User


def test_new_user_defaults_to_pending(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="a@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        assert u.status == "pending"  # fails CLOSED
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
Expected: errors/failures — `User` has no `status`/`oidc_sub`/`oidc_iss` (and the migration doesn't exist yet).

- [ ] **Step 3: Add columns + constraints to the model**

In `src/call_manager/db/models.py`, replace `User.__table_args__` with:

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

Add these columns after `is_admin` (keep `is_admin`):

```python
    # Fails CLOSED: a User created without an explicit status is `pending`
    # (no access). Only the migration backfill and admin bootstrap make a
    # user `active`.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )
    oidc_iss: Mapped[str | None] = mapped_column(Text, nullable=True)
    oidc_sub: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

(`Index`, `Text`, `BigInteger`, `ForeignKey`, `CheckConstraint`, `text` are already imported.)

- [ ] **Step 4: Find the current migration head**

Run: `uv run alembic heads`
Expected: prints one revision id. Note it as `<HEAD_REV>`.

- [ ] **Step 5: Create the migration (backfill existing rows to active, then default pending)**

Create `alembic/versions/<NN>_users_status_and_oidc.py` (next numeric prefix):

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
    # Add nullable first so we can backfill pre-existing (trusted) rows to
    # 'active', THEN set the going-forward default to 'pending' (fail closed).
    op.add_column("users", sa.Column("status", sa.String(length=16), nullable=True))
    op.execute("UPDATE users SET status = 'active' WHERE status IS NULL")
    op.alter_column("users", "status", server_default="pending", nullable=False)

    op.add_column("users", sa.Column("oidc_iss", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("oidc_sub", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("approved_by_user_id", sa.BigInteger(), nullable=True))
    op.add_column("users", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_users_approved_by", "users", "users",
        ["approved_by_user_id"], ["id"], ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_users_status_valid", "users", "status IN ('pending', 'active', 'disabled')"
    )
    op.create_check_constraint(
        "ck_users_oidc_paired_null", "users", "(oidc_iss IS NULL) = (oidc_sub IS NULL)"
    )
    op.create_index(
        "uq_users_oidc_identity", "users", ["oidc_iss", "oidc_sub"],
        unique=True, postgresql_where=sa.text("oidc_sub IS NOT NULL"),
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

- [ ] **Step 6: Update the shared test fixture to create active users**

In `tests/conftest.py`, `profile_factory._make`, change the owner creation:

```python
            u = User(email=f"owner-{len(created)}@ki.se")
```

to:

```python
            u = User(email=f"owner-{len(created)}@ki.se", status="active")
```

- [ ] **Step 7: Run the model tests**

Run: `uv run pytest tests/test_user_status_oidc_model.py -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/call_manager/db/models.py alembic/versions/ tests/conftest.py tests/test_user_status_oidc_model.py
git commit -m "feat(db): users.status (fail-closed) + oidc identity columns/constraints"
```

---

## Task 3: Encrypted OAuth transaction cookie

**Files:**
- Create: `src/call_manager/auth/oauth_txn.py`
- Test: `tests/test_auth_oauth_txn.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_auth_oauth_txn.py -v`
Expected: FAIL — module does not exist.

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
        try:
            raw = self._fernet.decrypt(token.encode("ascii"), ttl=max_age_seconds)
        except (InvalidToken, ValueError, TypeError) as e:
            raise InvalidTransaction(str(e)) from e
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise InvalidTransaction("payload is not an object")
        return data
```

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
- Modify: `src/call_manager/config.py`
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
    s = _base(monkeypatch, GOOGLE_OIDC_ENABLED="true",
              GOOGLE_CLIENT_ID="cid", GOOGLE_CLIENT_SECRET="csecret")
    assert s.google_oidc_enabled is True
    assert s.google_client_id == "cid"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_config_google_oidc.py -v`
Expected: FAIL — `Settings` has no `google_oidc_enabled`.

- [ ] **Step 3: Add the fields** (near `app_base_url`):

```python
    google_oidc_enabled: bool = Field(default=False, alias="GOOGLE_OIDC_ENABLED")
    google_client_id: str | None = Field(default=None, alias="GOOGLE_CLIENT_ID")
    google_client_secret: str | None = Field(default=None, alias="GOOGLE_CLIENT_SECRET")
    google_oidc_discovery_url: str = Field(
        default="https://accounts.google.com/.well-known/openid-configuration",
        alias="GOOGLE_OIDC_DISCOVERY_URL",
    )
```

- [ ] **Step 4: Add the computed redirect URI + prod guard**

Next to `cookie_secure`:

```python
    @computed_field  # type: ignore[prop-decorator]
    @property
    def google_redirect_uri(self) -> str:
        return self.app_base_url.rstrip("/") + "/auth/google/callback"
```

Next to `_require_strong_secret_in_prod`:

```python
    @model_validator(mode="after")
    def _require_google_credentials_when_enabled(self):
        if self.google_oidc_enabled and (
            not self.google_client_id or not self.google_client_secret
        ):
            raise ValueError(
                "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required when "
                "GOOGLE_OIDC_ENABLED is true."
            )
        return self
```

- [ ] **Step 5: Update `.env.example`**

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

## Task 5: OIDC client (RS256-only, essential claims, JWKS rotation, PKCE)

**Files:**
- Create: `src/call_manager/auth/oidc.py`
- Test: `tests/test_auth_oidc.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auth_oidc.py
import time

import httpx
import pytest
from authlib.jose import JsonWebKey, jwt

from call_manager.auth.oidc import (
    IdTokenInvalid, OIDCClient, generate_pkce, generate_token,
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
    jwks = {"keys": [(keys or _KEY).as_dict(is_private=False)]} if keys is None or hasattr(keys, "as_dict") else keys
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_auth_oidc.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the client**

```python
# src/call_manager/auth/oidc.py
from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import httpx
from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError

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

    def __init__(self, *, discovery_url: str, client_id: str, client_secret: str,
                 http_client: httpx.Client | None = None):
        self._discovery_url = discovery_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http_client or httpx.Client(timeout=10.0)
        self._metadata: dict | None = None
        self._jwks = None
        # Pin algorithms: never let the token header pick (alg-confusion defense).
        self._jwt = JsonWebToken(["RS256"])

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
                self._jwks = JsonWebKey.import_key_set(r.json())
            except (httpx.HTTPError, ValueError, KeyError) as e:
                raise OIDCError(f"jwks fetch failed: {e}") from e
        return self._jwks

    def build_authorization_url(self, *, redirect_uri, state, nonce, code_challenge) -> str:
        params = {
            "response_type": "code", "client_id": self._client_id,
            "redirect_uri": redirect_uri, "scope": "openid email profile",
            "state": state, "nonce": nonce,
            "code_challenge": code_challenge, "code_challenge_method": "S256",
        }
        return self._meta()["authorization_endpoint"] + "?" + urlencode(params)

    def exchange_code(self, *, code, code_verifier, redirect_uri) -> dict:
        data = {
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "client_id": self._client_id,
            "client_secret": self._client_secret, "code_verifier": code_verifier,
        }
        try:
            r = self._http.post(self._meta()["token_endpoint"], data=data)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise OIDCError(f"token exchange failed: {e}") from e

    def _decode(self, id_token: str, claims_options: dict):
        # Decode with the cached JWKS; on an unknown signing key (rotation),
        # refetch the JWKS ONCE and retry.
        try:
            return self._jwt.decode(id_token, self._jwk_set(), claims_options=claims_options)
        except (JoseError, ValueError, KeyError):
            return self._jwt.decode(
                id_token, self._jwk_set(force=True), claims_options=claims_options
            )

    def validate_id_token(self, id_token: str, *, expected_nonce: str) -> dict:
        issuer = self._meta()["issuer"]
        claims_options = {
            "iss": {"essential": True, "value": issuer},
            "exp": {"essential": True},
            "iat": {"essential": True},
            "sub": {"essential": True},
            "email": {"essential": True},
            "aud": {"essential": True},
        }
        try:
            claims = self._decode(id_token, claims_options)
            claims.validate()  # exp / iat / essential presence
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_auth_oidc.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/auth/oidc.py
git add src/call_manager/auth/oidc.py tests/test_auth_oidc.py
git commit -m "feat(auth): hardened Google OIDC client (RS256, essential claims, JWKS rotation, PKCE)"
```

---

## Task 6: Race-safe account resolution + linking

**Files:**
- Create: `src/call_manager/auth/accounts.py`
- Test: `tests/test_auth_accounts.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auth_accounts.py
import call_manager.auth.accounts as acc
from call_manager.auth.accounts import (
    link_google_identity, resolve_google_account,
)
from call_manager.db.models import User

ISS = "https://accounts.google.com"


def test_new_identity_creates_pending(pg_session_factory):
    with pg_session_factory() as s:
        res = resolve_google_account(s, iss=ISS, sub="s1", email="new@gmail.com", name="N")
        s.commit()
        assert res.outcome == "created"
        assert res.user.status == "pending" and res.user.is_admin is False


def test_returning_identity_matches(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="x@ki.se", status="active", oidc_iss=ISS, oidc_sub="s2"))
        s.commit()
    with pg_session_factory() as s:
        res = resolve_google_account(s, iss=ISS, sub="s2", email="x@ki.se", name=None)
        assert res.outcome == "matched" and res.user.status == "active"


def test_email_collision_blocked(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="admin@ki.se", status="active"))  # no identity
        s.commit()
    with pg_session_factory() as s:
        res = resolve_google_account(s, iss=ISS, sub="s3", email="admin@ki.se", name=None)
        assert res.outcome == "email_collision" and res.user is None
        assert s.query(User).filter_by(email="admin@ki.se").one().oidc_sub is None


def test_concurrent_insert_reresolves(pg_session_factory, monkeypatch):
    # Simulate the race: a competitor commits the same identity AFTER our first
    # existence check returns None. The flush hits the unique index -> we roll
    # back and re-resolve to the winner.
    with pg_session_factory() as s0:
        s0.add(User(email="pre@ki.se", status="active", oidc_iss=ISS, oidc_sub="race"))
        s0.commit()

    real = acc._by_identity
    calls = {"n": 0}

    def flaky(session, iss, sub):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # stale read: pretend not found
        return real(session, iss, sub)

    monkeypatch.setattr(acc, "_by_identity", flaky)
    with pg_session_factory() as s:
        res = resolve_google_account(s, iss=ISS, sub="race", email="newcomer@ki.se", name=None)
        assert res.outcome == "matched"
        assert res.user.email == "pre@ki.se"


def test_link_attaches_identity(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="me@ki.se", status="active")
        s.add(u); s.commit(); uid = u.id
    with pg_session_factory() as s:
        assert link_google_identity(s, user_id=uid, iss=ISS, sub="s4") is True
        s.commit()
        assert s.get(User, uid).oidc_sub == "s4"


def test_link_rejects_used_identity(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="other@ki.se", status="active", oidc_iss=ISS, oidc_sub="s5"))
        u = User(email="mine@ki.se", status="active")
        s.add(u); s.commit(); uid = u.id
    with pg_session_factory() as s:
        assert link_google_identity(s, user_id=uid, iss=ISS, sub="s5") is False
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
    outcome: str  # "matched" | "created" | "email_collision"
    user: User | None


def _by_identity(session: Session, iss: str, sub: str) -> User | None:
    return session.execute(
        select(User).where(User.oidc_iss == iss, User.oidc_sub == sub)
    ).scalar_one_or_none()


def resolve_google_account(session: Session, *, iss: str, sub: str, email: str,
                           name: str | None) -> AccountResolution:
    """1. match (iss,sub); 2. email exists -> collision (never auto-link);
    3. else create a PENDING account. Race-safe via unique-index re-resolution."""
    email = email.strip().lower()

    existing = _by_identity(session, iss, sub)
    if existing is not None:
        existing.last_login_at = datetime.now(UTC)
        return AccountResolution("matched", existing)

    if session.execute(select(User).where(User.email == email)).scalar_one_or_none():
        return AccountResolution("email_collision", None)

    user = User(email=email, name=name, oidc_iss=iss, oidc_sub=sub,
                is_admin=False, status="pending")
    session.add(user)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        winner = _by_identity(session, iss, sub)
        if winner is not None:
            winner.last_login_at = datetime.now(UTC)
            return AccountResolution("matched", winner)
        return AccountResolution("email_collision", None)
    return AccountResolution("created", user)


def link_google_identity(session: Session, *, user_id: int, iss: str, sub: str) -> bool:
    """Attach a Google identity to an authenticated user.
    False if that identity already belongs to another account."""
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
Expected: all PASS (including the re-resolution race test).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/auth/accounts.py tests/test_auth_accounts.py
git commit -m "feat(auth): race-safe Google account resolution + explicit linking"
```

---

## Task 7: Central inactive-account enforcement

**Files:**
- Modify: `src/call_manager/auth/dependencies.py`
- Modify: `src/call_manager/main.py` (exception handlers + app.state wiring)
- Test: `tests/test_auth_inactive_enforcement.py`

- [ ] **Step 1: Write the failing test (exercises the REAL handler via create_app + /me)**

```python
# tests/test_auth_inactive_enforcement.py
from fastapi.testclient import TestClient

from call_manager.auth.dependencies import AccountInactive, get_current_user
from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import User
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _user(sf, status):
    with sf() as s:
        u = User(email=f"{status}@ki.se", status=status)
        s.add(u); s.commit(); s.refresh(u)
        return u.id


def test_active_user_reaches_me(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    uid = _user(app.state.session_factory, "active")
    with TestClient(app) as c:
        c.cookies.set("session", app.state.session_signer.make(user_id=uid))
        assert c.get("/me", headers={"accept": "text/html"}).status_code == 200


def test_pending_user_html_redirects_and_clears_cookie(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    uid = _user(app.state.session_factory, "pending")
    with TestClient(app) as c:
        c.cookies.set("session", app.state.session_signer.make(user_id=uid))
        r = c.get("/me", headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/auth/pending"
        set_cookie = " ".join(r.headers.get_list("set-cookie"))
        assert "session=" in set_cookie and ("Max-Age=0" in set_cookie or "expires=" in set_cookie.lower())


def test_disabled_user_api_gets_403(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    uid = _user(app.state.session_factory, "disabled")
    with TestClient(app) as c:
        c.cookies.set("session", app.state.session_signer.make(user_id=uid))
        assert c.get("/me").status_code == 403  # no Accept: text/html -> API path


def test_get_current_user_raises_account_inactive(pg_url, pg_session_factory):
    # Direct unit check of the dependency raising the typed exception.
    import pytest
    from fastapi import FastAPI, Request
    from call_manager.db.session import make_engine, make_session_factory

    signer = SessionSigner(secret="x" * 32, max_age_seconds=3600)
    engine = make_engine(pg_url)
    sf = make_session_factory(engine)
    try:
        with sf() as s:
            u = User(email="p@ki.se", status="pending")
            s.add(u); s.commit(); s.refresh(u); uid = u.id
        app = FastAPI(); app.state.session_signer = signer; app.state.session_factory = sf
        scope = {"type": "http", "headers": [], "app": app}
        req = Request(scope)
        with pytest.raises(AccountInactive):
            get_current_user(req, session=signer.make(user_id=uid))
    finally:
        engine.dispose()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_auth_inactive_enforcement.py -v`
Expected: FAIL — `AccountInactive` not defined; `/me` returns 200 for pending users.

- [ ] **Step 3: Add the exception + status gate to `get_current_user`**

In `src/call_manager/auth/dependencies.py`:

```python
class AccountInactive(Exception):
    """Authenticated cookie, but the account is not active (pending/disabled)."""

    def __init__(self, status: str):
        self.status = status
        super().__init__(status)
```

Inside `get_current_user`, after the `session_epoch` check, before `db.expunge(user)`:

```python
        if user.status != "active":
            raise AccountInactive(user.status)
```

- [ ] **Step 4: Register handler + wire cipher/client in `main.py`**

Imports near the other auth imports:

```python
from call_manager.auth.dependencies import AccountInactive
from call_manager.auth.oauth_txn import TransactionCipher
from call_manager.auth.oidc import OIDCClient
```

Handler inside `create_app` (next to `_redirect_html_401`):

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
                "session", path="/", httponly=True,
                secure=settings.cookie_secure, samesite="lax",
            )
            return resp
        return JSONResponse(status_code=403, content={"detail": "account not active"})
```

In the app.state wiring block (after `app.state.session_signer = ...`):

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

(`RedirectResponse`, `JSONResponse` already imported in main.py.)

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_auth_inactive_enforcement.py -v`
Expected: all PASS.

- [ ] **Step 6: Regression**

Run: `uv run pytest tests/test_auth_dependencies.py -v`
Expected: PASS — but note `_mk_app` in that file creates users via `User(email=...)` which now default to `pending`. Update those creations to `User(email=..., status="active")` so the existing "valid cookie" tests still authenticate.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/auth/dependencies.py src/call_manager/main.py tests/test_auth_dependencies.py tests/test_auth_inactive_enforcement.py
git commit -m "feat(auth): central inactive-account enforcement (redirect+clear cookie / 403)"
```

---

## Task 8: Google OAuth routes (start / callback / holding pages)

**Files:**
- Create: `src/call_manager/web/routes/oauth_google.py`
- Create: `account_pending.html`, `account_no_access.html`, `auth_error.html` under `src/call_manager/web/templates/`
- Modify: `src/call_manager/main.py` (include router)
- Test: `tests/test_web_oauth_google.py`

- [ ] **Step 1: Write the failing test (full clean-callback matrix)**

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
        return {"id_token": jwt.encode({"alg": "RS256", "kid": _KEY.kid}, self._codes[code], _KEY).decode()}

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
        "DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver",
        "GOOGLE_OIDC_ENABLED": "true", "GOOGLE_CLIENT_ID": AUD, "GOOGLE_CLIENT_SECRET": "secret",
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    fake = FakeOIDC()
    app.state.google_oidc_client = fake
    return app, fake


def _claims(**over):
    now = int(time.time())
    base = {"iss": ISS, "aud": AUD, "sub": "g-1", "email": "new@gmail.com",
            "email_verified": True, "name": "New", "iat": now, "exp": now + 600}
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
        fake.set_code("C", _claims(nonce=nonce))
        r = c.get(f"/auth/google/callback?code=C&state={state}",
                  headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/auth/pending"
        assert r.headers.get("referrer-policy") == "no-referrer"
        assert "oauth_txn=" in "".join(r.headers.get_list("set-cookie"))  # cleared
    with pg_session_factory() as s:
        assert s.query(User).filter_by(email="new@gmail.com").one().status == "pending"


def test_callback_active_user_gets_session(monkeypatch, pg_url, pg_session_factory):
    app, fake = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        s.add(User(email="ok@ki.se", status="active", oidc_iss=ISS, oidc_sub="g-2")); s.commit()
    with TestClient(app) as c:
        state, nonce = _start(c)
        fake.set_code("C2", _claims(sub="g-2", email="ok@ki.se", nonce=nonce))
        r = c.get(f"/auth/google/callback?code=C2&state={state}",
                  headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert "session=" in "".join(r.headers.get_list("set-cookie"))


def test_callback_disabled_user_no_access(monkeypatch, pg_url, pg_session_factory):
    app, fake = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        s.add(User(email="no@ki.se", status="disabled", oidc_iss=ISS, oidc_sub="g-9")); s.commit()
    with TestClient(app) as c:
        state, nonce = _start(c)
        fake.set_code("C9", _claims(sub="g-9", email="no@ki.se", nonce=nonce))
        r = c.get(f"/auth/google/callback?code=C9&state={state}",
                  headers={"accept": "text/html"}, follow_redirects=False)
        assert r.headers["location"] == "/auth/no-access"


def test_callback_email_collision_error(monkeypatch, pg_url, pg_session_factory):
    app, fake = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        s.add(User(email="dupe@ki.se", status="active")); s.commit()
    with TestClient(app) as c:
        state, nonce = _start(c)
        fake.set_code("C3", _claims(sub="g-3", email="dupe@ki.se", nonce=nonce))
        r = c.get(f"/auth/google/callback?code=C3&state={state}",
                  headers={"accept": "text/html"}, follow_redirects=False)
        assert r.headers["location"] == "/auth/error"


def test_callback_state_mismatch_error(monkeypatch, pg_url, pg_session_factory):
    app, _ = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        _start(c)
        r = c.get("/auth/google/callback?code=X&state=WRONG",
                  headers={"accept": "text/html"}, follow_redirects=False)
        assert r.headers["location"] == "/auth/error"
        assert r.headers.get("referrer-policy") == "no-referrer"


def test_callback_provider_error_param(monkeypatch, pg_url, pg_session_factory):
    app, _ = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        state, _ = _start(c)
        r = c.get(f"/auth/google/callback?error=access_denied&state={state}",
                  follow_redirects=False)
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
        "DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    with TestClient(app) as c:
        assert c.post("/auth/google/start", data={}).status_code == 404
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_web_oauth_google.py -v`
Expected: FAIL — route module/routes don't exist.

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
from call_manager.db.models import User
from call_manager.web.routes.auth import _client_ip

_logger = logging.getLogger(__name__)
router = APIRouter()

_TXN_COOKIE = "oauth_txn"
_TXN_MAX_AGE = 600  # 10 minutes


def _require_enabled(request: Request):
    client = request.app.state.google_oidc_client
    if client is None:
        raise HTTPException(status_code=404, detail="not found")
    return client


def _clean(target: str) -> RedirectResponse:
    """Every callback exit goes through here: delete txn cookie, suppress
    Referer (the callback URL carries ?code=), redirect to a clean URL."""
    resp = RedirectResponse(url=target, status_code=303)
    resp.delete_cookie(_TXN_COOKIE, path="/auth/google")
    resp.headers["Referrer-Policy"] = "no-referrer"
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
    if not request.app.state.ip_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests")

    verifier, challenge = generate_pkce()
    state, nonce = generate_token(), generate_token()
    payload = {"state": state, "nonce": nonce, "code_verifier": verifier, "next": safe_next(next)}

    if link == "1":
        # Connect-Google: require an authenticated ACTIVE session; fail closed.
        from call_manager.auth.dependencies import AccountInactive, get_current_user

        try:
            user = get_current_user(request, session=request.cookies.get("session"))
        except (HTTPException, AccountInactive):
            return RedirectResponse(url="/login", status_code=303)
        payload["link_uid"] = user.id
        payload["link_epoch"] = user.session_epoch

    url = client.build_authorization_url(
        redirect_uri=settings.google_redirect_uri, state=state, nonce=nonce,
        code_challenge=challenge,
    )
    resp = RedirectResponse(url=url, status_code=303)
    resp.set_cookie(
        _TXN_COOKIE, cipher.make(payload), max_age=_TXN_MAX_AGE, httponly=True,
        secure=settings.cookie_secure, samesite="lax", path="/auth/google",
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

    # Rate-limit within the clean-redirect contract (no raw 429 that skips cleanup).
    if not request.app.state.ip_limiter.allow(_client_ip(request)):
        return _clean("/auth/error")
    if error or not code or not state:
        return _clean("/auth/error")

    raw = request.cookies.get(_TXN_COOKIE)
    if not raw:
        return _clean("/auth/error")
    try:
        txn = cipher.read(raw, max_age_seconds=_TXN_MAX_AGE)
    except InvalidTransaction:
        return _clean("/auth/error")
    if state != txn.get("state"):
        return _clean("/auth/error")

    try:
        token = client.exchange_code(
            code=code, code_verifier=txn["code_verifier"],
            redirect_uri=settings.google_redirect_uri,
        )
        claims = client.validate_id_token(token["id_token"], expected_nonce=txn["nonce"])
    except (OIDCError, IdTokenInvalid, KeyError, TypeError):
        return _clean("/auth/error")

    iss, sub = claims.get("iss"), claims.get("sub")
    email, name = claims.get("email"), claims.get("name")
    if not iss or not sub or not email:
        return _clean("/auth/error")

    # Connect-Google (link mode): revalidate the initiating session, fail closed.
    link_uid = txn.get("link_uid")
    if link_uid is not None:
        with session_factory() as db:
            u = db.get(User, int(link_uid))
            if u is None or u.status != "active" or u.session_epoch != txn.get("link_epoch"):
                return _clean("/auth/error")
            ok = link_google_identity(db, user_id=u.id, iss=iss, sub=sub)
            db.commit()
        return _clean("/me" if ok else "/auth/error")

    with session_factory() as db:
        res = resolve_google_account(db, iss=iss, sub=sub, email=email, name=name)
        if res.outcome == "email_collision":
            db.rollback()
            return _clean("/auth/error")
        user_id, status, session_epoch = res.user.id, res.user.status, res.user.session_epoch
        if status == "active":
            res.user.last_login_at = datetime.now(UTC)
        db.commit()

    if status == "pending":
        return _clean("/auth/pending")
    if status == "disabled":
        return _clean("/auth/no-access")

    target = safe_next(txn.get("next")) or "/"
    resp = _clean(target)
    resp.set_cookie(
        "session", session_signer.make(user_id=user_id, session_epoch=session_epoch),
        max_age=settings.session_max_age_seconds, httponly=True,
        secure=settings.cookie_secure, samesite="lax",
    )
    _logger.info("google sign-in success", extra={"event": "google_signin_success"})
    return resp


@router.get("/auth/pending", response_class=HTMLResponse)
def auth_pending(request: Request):
    return request.app.state.templates.TemplateResponse(
        request=request, name="account_pending.html", context={})


@router.get("/auth/no-access", response_class=HTMLResponse)
def auth_no_access(request: Request):
    return request.app.state.templates.TemplateResponse(
        request=request, name="account_no_access.html", context={})


@router.get("/auth/error", response_class=HTMLResponse)
def auth_error(request: Request):
    return request.app.state.templates.TemplateResponse(
        request=request, name="auth_error.html", context={})
```

> Implementation note: replace the awkward inline `__import__` for `User` with a normal top-of-file import — `from call_manager.db.models import User` — and load the user with `db.get(User, int(link_uid))`. (The inline form above only avoids a circular-import worry that does not actually exist here; use the clean import.)

- [ ] **Step 4: Create the three templates**

`account_pending.html`:

```html
{% extends "base.html" %}
{% block content %}
<main class="container">
  <h1>Account awaiting approval</h1>
  <p>Your sign-in worked. An administrator needs to approve your account before
     you can use Call Manager. You'll be able to sign in once approved.</p>
  <p><a href="/login">Back to sign in</a></p>
</main>
{% endblock %}
```

`account_no_access.html`:

```html
{% extends "base.html" %}
{% block content %}
<main class="container">
  <h1>No access</h1>
  <p>This account does not have access. If you think this is a mistake, contact
     an administrator.</p>
  <p><a href="/login">Back to sign in</a></p>
</main>
{% endblock %}
```

`auth_error.html`:

```html
{% extends "base.html" %}
{% block content %}
<main class="container">
  <h1>Sign-in could not be completed</h1>
  <p>We couldn't sign you in. Please try again.</p>
  <p>If you already have an account with this email, sign in your usual way, then
     connect Google from your account settings.</p>
  <p><a href="/login">Back to sign in</a></p>
</main>
{% endblock %}
```

(Confirm the `{% block %}` name matches `base.html`.)

- [ ] **Step 5: Wire the router in `main.py`**

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
git commit -m "feat(auth): Google OAuth start/callback (clean-redirect contract) + holding pages"
```

---

## Task 9: Connect Google (explicit linking) UI

**Files:**
- Modify: `src/call_manager/web/routes/me.py` (template context)
- Modify: `src/call_manager/web/templates/me.html`
- Test: append to `tests/test_web_oauth_google.py`

- [ ] **Step 1: Write the failing test**

```python
def test_connect_google_links_active_user(monkeypatch, pg_url, pg_session_factory):
    app, fake = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="link@ki.se", status="active"); s.add(u); s.commit(); s.refresh(u); uid = u.id
    with TestClient(app) as c:
        c.cookies.set("session", app.state.session_signer.make(user_id=uid))
        start = c.post("/auth/google/start", data={"link": "1"}, follow_redirects=False)
        q = parse_qs(urlparse(start.headers["location"]).query)
        fake.set_code("LC", _claims(sub="g-link", email="whatever@gmail.com", nonce=q["nonce"][0]))
        r = c.get(f"/auth/google/callback?code=LC&state={q['state'][0]}",
                  headers={"accept": "text/html"}, follow_redirects=False)
        assert r.headers["location"] == "/me"
    with pg_session_factory() as s:
        assert s.get(User, uid).oidc_sub == "g-link"


def test_connect_google_unauthenticated_redirects_to_login(monkeypatch, pg_url, pg_session_factory):
    app, _ = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.post("/auth/google/start", data={"link": "1"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
```

- [ ] **Step 2: Run to verify it fails / passes at route layer**

Run: `uv run pytest tests/test_web_oauth_google.py -k connect_google -v`
Expected: these pass once Task 8's link branch is correct (link mode + fail-closed). If they fail, fix the link branch in `oauth_google.py` first. Then add the UI (Steps 3-4) so users can trigger it.

- [ ] **Step 3: Pass linking context from `me.py`**

In the GET handler that renders `me.html`, add to context:

```python
            "google_oidc_enabled": request.app.state.settings.google_oidc_enabled,
            "google_linked": user.oidc_sub is not None,
```

- [ ] **Step 4: Add the control to `me.html`**

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
git commit -m "feat(auth): Connect Google from /me (revalidates initiating session)"
```

---

## Task 10: Magic-link becomes login-only (no auto-create + status re-check)

**Files:**
- Modify: `src/call_manager/web/routes/auth.py` (`login_submit` ~163-173; `login_verify_consume` ~272-278)
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
    for k, v in {
        "DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    app.state.email_sender = InMemoryEmailSender()
    return app


def test_unknown_email_creates_no_user_no_token(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        assert c.post("/login", data={"email": "ghost@ki.se"}).status_code == 200
    with pg_session_factory() as s:
        assert s.execute(select(User).where(User.email == "ghost@ki.se")).scalar_one_or_none() is None
        assert s.execute(select(MagicLinkToken)).first() is None


def test_disabled_after_issue_cannot_consume(monkeypatch, pg_url, pg_session_factory):
    from call_manager.auth.tokens import issue_token
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="real@ki.se", status="active"); s.add(u); s.flush()
        raw = issue_token(s, user_id=u.id, ttl_minutes=15); s.commit(); uid = u.id
    with pg_session_factory() as s:
        s.get(User, uid).status = "disabled"; s.commit()
    with TestClient(app) as c:
        r = c.post("/login/verify", data={"token": raw},
                   headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/auth/no-access"
        assert "session=" not in "".join(h for h in r.headers.get_list("set-cookie"))
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_web_auth_magic_link_gate.py -v`
Expected: FAIL — current code auto-creates and consumes unconditionally.

- [ ] **Step 3: Remove auto-create in `login_submit`**

Replace the user-create block (~163-173):

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
        # an existing ACTIVE account. The response body is identical on every
        # branch (enumeration-resistant *body*); note the timing residual risk
        # documented in the spec — bounded by the per-email/per-IP limiters.
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

Replace the user-load block (~272-278):

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
        # Re-check status at consume: the account may have been disabled between
        # the link being mailed and clicked.
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

- [ ] **Step 6: Regression on the real auth tests**

Run: `uv run pytest tests/test_auth_redirects.py tests/test_web_calls.py -v`
Expected: PASS. Any test that relied on magic-link auto-creating a user, or created a `User` then logged in, must create it explicitly with `status="active"` (e.g. `tests/test_web_calls.py::_logged_in` — change `User(email=email)` to `User(email=email, status="active")`).

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/web/routes/auth.py tests/test_web_auth_magic_link_gate.py tests/test_web_calls.py
git commit -m "feat(auth): magic-link login-only (no auto-create; status re-check at consume)"
```

---

## Task 11: Admin user management (queue + list + state-restricted transitions)

**Files:**
- Modify: `src/call_manager/web/routes/admin.py`
- Create: `src/call_manager/web/templates/admin_users_pending.html`
- Create: `src/call_manager/web/templates/admin_users.html`
- Modify: `src/call_manager/web/templates/admin.html` (links to the two new pages)
- Test: `tests/test_web_admin_users.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_admin_users.py
from fastapi.testclient import TestClient

from call_manager.db.models import User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def _admin(app, pg_session_factory, email="admin@ki.se"):
    with pg_session_factory() as s:
        a = User(email=email, status="active", is_admin=True)
        s.add(a); s.commit(); s.refresh(a); aid = a.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=aid))
    return c, aid


def _pending(pg_session_factory, email, sub):
    with pg_session_factory() as s:
        p = User(email=email, status="pending", oidc_iss="i", oidc_sub=sub)
        s.add(p); s.commit(); s.refresh(p); return p.id


def test_pending_queue_lists_pending(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url); c, _ = _admin(app, pg_session_factory)
    _pending(pg_session_factory, "p@gmail.com", "s0")
    r = c.get("/admin/users/pending")
    assert r.status_code == 200 and "p@gmail.com" in r.text


def test_approve_member(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url); c, _ = _admin(app, pg_session_factory)
    pid = _pending(pg_session_factory, "p1@gmail.com", "s1")
    c.post(f"/admin/users/{pid}/approve", data={"role": "member"}, follow_redirects=False)
    with pg_session_factory() as s:
        u = s.get(User, pid)
        assert u.status == "active" and u.is_admin is False and u.approved_at is not None


def test_approve_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url); c, _ = _admin(app, pg_session_factory)
    pid = _pending(pg_session_factory, "p2@gmail.com", "s2")
    c.post(f"/admin/users/{pid}/approve", data={"role": "admin"}, follow_redirects=False)
    with pg_session_factory() as s:
        assert s.get(User, pid).is_admin is True


def test_approve_rejects_non_pending(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url); c, aid = _admin(app, pg_session_factory)
    # the admin is active, not pending -> approve must refuse (can't demote via approve)
    r = c.post(f"/admin/users/{aid}/approve", data={"role": "member"}, follow_redirects=False)
    assert r.status_code == 400
    with pg_session_factory() as s:
        assert s.get(User, aid).is_admin is True


def test_deny_only_pending(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url); c, _ = _admin(app, pg_session_factory)
    pid = _pending(pg_session_factory, "p3@gmail.com", "s3")
    c.post(f"/admin/users/{pid}/deny", follow_redirects=False)
    with pg_session_factory() as s:
        assert s.get(User, pid).status == "disabled"


def test_enable_only_disabled(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url); c, _ = _admin(app, pg_session_factory)
    pid = _pending(pg_session_factory, "p4@gmail.com", "s4")  # pending, not disabled
    r = c.post(f"/admin/users/{pid}/enable", follow_redirects=False)
    assert r.status_code == 400  # can't activate a pending user without approval
    with pg_session_factory() as s:
        assert s.get(User, pid).status == "pending"


def test_disable_bumps_epoch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url); c, _ = _admin(app, pg_session_factory)
    with pg_session_factory() as s:
        s.add(User(email="admin2@ki.se", status="active", is_admin=True))  # keep >1 admin
        m = User(email="m@ki.se", status="active"); s.add(m); s.commit(); s.refresh(m)
        mid, before = m.id, m.session_epoch
    c.post(f"/admin/users/{mid}/disable", follow_redirects=False)
    with pg_session_factory() as s:
        u = s.get(User, mid)
        assert u.status == "disabled" and u.session_epoch == before + 1


def test_change_role_demote_guarded(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url); c, aid = _admin(app, pg_session_factory)
    # only one active admin -> demoting them must fail
    r = c.post(f"/admin/users/{aid}/role", data={"role": "member"}, follow_redirects=False)
    assert r.status_code == 400
    with pg_session_factory() as s:
        assert s.get(User, aid).is_admin is True


def test_cannot_disable_last_active_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url); c, aid = _admin(app, pg_session_factory)
    r = c.post(f"/admin/users/{aid}/disable", follow_redirects=False)
    assert r.status_code == 400
    with pg_session_factory() as s:
        assert s.get(User, aid).status == "active"


def test_non_admin_blocked(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="member@ki.se", status="active"); s.add(u); s.commit(); s.refresh(u); uid = u.id
    with TestClient(app) as c:
        c.cookies.set("session", app.state.session_signer.make(user_id=uid))
        assert c.get("/admin/users/pending").status_code == 403
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_web_admin_users.py -v`
Expected: FAIL — routes don't exist.

- [ ] **Step 3: Implement the admin routes**

Add to `src/call_manager/web/routes/admin.py` (merge imports with existing ones):

```python
from datetime import UTC, datetime

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from call_manager.auth.dependencies import require_admin
from call_manager.db.models import User


def _lock_active_admin_count(db) -> int:
    """Count active admins while LOCKING those rows, so two concurrent
    demote/disable transactions serialize and can't both pass the guard."""
    db.execute(
        select(User.id).where(User.is_admin.is_(True), User.status == "active").with_for_update()
    ).all()
    return db.execute(
        select(func.count()).select_from(User).where(
            User.is_admin.is_(True), User.status == "active")
    ).scalar_one()


@router.get("/admin/users/pending")
def pending_users(request: Request, page: int = 1, admin: User = Depends(require_admin)):  # noqa: B008
    page = max(1, page)
    per_page = 50
    with request.app.state.session_factory() as db:
        rows = db.execute(
            select(User).where(User.status == "pending")
            .order_by(User.created_at.asc()).limit(per_page).offset((page - 1) * per_page)
        ).scalars().all()
        users = [{"id": u.id, "email": u.email, "name": u.name,
                  "created_at": u.created_at, "google_linked": u.oidc_sub is not None}
                 for u in rows]
    return request.app.state.templates.TemplateResponse(
        request=request, name="admin_users_pending.html", context={"users": users, "page": page})


@router.get("/admin/users")
def list_users(request: Request, page: int = 1, admin: User = Depends(require_admin)):  # noqa: B008
    page = max(1, page)
    per_page = 50
    with request.app.state.session_factory() as db:
        rows = db.execute(
            select(User).where(User.status != "pending")
            .order_by(User.email.asc()).limit(per_page).offset((page - 1) * per_page)
        ).scalars().all()
        users = [{"id": u.id, "email": u.email, "name": u.name,
                  "status": u.status, "is_admin": u.is_admin} for u in rows]
        total = db.execute(
            select(func.count()).select_from(User).where(User.status != "pending")
        ).scalar_one()
    return request.app.state.templates.TemplateResponse(
        request=request, name="admin_users.html",
        context={"users": users, "page": page, "per_page": per_page, "total": total})


@router.post("/admin/users/{user_id}/approve")
def approve_user(request: Request, user_id: int, role: str = Form("member"),
                 admin: User = Depends(require_admin)):  # noqa: B008
    if role not in ("member", "admin"):
        raise HTTPException(status_code=400, detail="invalid role")
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if u.status != "pending":
            raise HTTPException(status_code=400, detail="user is not pending")
        u.status = "active"
        u.is_admin = role == "admin"
        u.approved_by_user_id = admin.id
        u.approved_at = datetime.now(UTC)
        db.commit()
    return RedirectResponse(url="/admin/users/pending", status_code=303)


@router.post("/admin/users/{user_id}/deny")
def deny_user(request: Request, user_id: int, admin: User = Depends(require_admin)):  # noqa: B008
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if u.status != "pending":
            raise HTTPException(status_code=400, detail="user is not pending")
        u.status = "disabled"
        db.commit()
    return RedirectResponse(url="/admin/users/pending", status_code=303)


@router.post("/admin/users/{user_id}/disable")
def disable_user(request: Request, user_id: int, admin: User = Depends(require_admin)):  # noqa: B008
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if u.status != "active":
            raise HTTPException(status_code=400, detail="user is not active")
        if u.is_admin and _lock_active_admin_count(db) <= 1:
            raise HTTPException(status_code=400, detail="cannot disable the last active admin")
        u.status = "disabled"
        u.session_epoch = (u.session_epoch or 0) + 1
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/enable")
def enable_user(request: Request, user_id: int, admin: User = Depends(require_admin)):  # noqa: B008
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if u.status != "disabled":
            raise HTTPException(status_code=400, detail="user is not disabled")
        u.status = "active"
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/role")
def change_role(request: Request, user_id: int, role: str = Form(...),
                admin: User = Depends(require_admin)):  # noqa: B008
    if role not in ("member", "admin"):
        raise HTTPException(status_code=400, detail="invalid role")
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if u.status != "active":
            raise HTTPException(status_code=400, detail="user is not active")
        demoting = u.is_admin and role == "member"
        if demoting and _lock_active_admin_count(db) <= 1:
            raise HTTPException(status_code=400, detail="cannot demote the last active admin")
        u.is_admin = role == "admin"
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)
```

- [ ] **Step 4: Create the templates**

`admin_users_pending.html`:

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
      <input type="hidden" name="role" value="member" /><button type="submit">Approve as member</button>
    </form>
    <form method="post" action="/admin/users/{{ u.id }}/approve" style="display:inline">
      <input type="hidden" name="role" value="admin" /><button type="submit">Approve as admin</button>
    </form>
    <form method="post" action="/admin/users/{{ u.id }}/deny" style="display:inline">
      <button type="submit" class="secondary">Deny</button>
    </form>
  </article>
  {% endfor %}
  <p><a href="/admin/users">All users</a> · <a href="/admin">Admin home</a></p>
</main>
{% endblock %}
```

`admin_users.html`:

```html
{% extends "base.html" %}
{% block content %}
<main class="container">
  <h1>Users</h1>
  <table>
    <thead><tr><th>Email</th><th>Status</th><th>Role</th><th>Actions</th></tr></thead>
    <tbody>
    {% for u in users %}
      <tr>
        <td>{{ u.email }}</td><td>{{ u.status }}</td>
        <td>{{ "admin" if u.is_admin else "member" }}</td>
        <td>
          {% if u.status == "active" %}
            <form method="post" action="/admin/users/{{ u.id }}/disable" style="display:inline">
              <button type="submit" class="secondary">Disable</button></form>
            <form method="post" action="/admin/users/{{ u.id }}/role" style="display:inline">
              <input type="hidden" name="role" value="{{ "member" if u.is_admin else "admin" }}" />
              <button type="submit">{{ "Make member" if u.is_admin else "Make admin" }}</button></form>
          {% elif u.status == "disabled" %}
            <form method="post" action="/admin/users/{{ u.id }}/enable" style="display:inline">
              <button type="submit">Enable</button></form>
          {% endif %}
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  <p>Showing page {{ page }} ({{ total }} total).
     {% if page > 1 %}<a href="/admin/users?page={{ page - 1 }}">Prev</a>{% endif %}
     {% if total > page * per_page %}<a href="/admin/users?page={{ page + 1 }}">Next</a>{% endif %}
  </p>
  <p><a href="/admin/users/pending">Pending approvals</a> · <a href="/admin">Admin home</a></p>
</main>
{% endblock %}
```

Add links to `admin.html` (in the dashboard nav): `<a href="/admin/users/pending">Pending approvals</a>` and `<a href="/admin/users">Users</a>`.

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_web_admin_users.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/routes/admin.py src/call_manager/web/templates/admin_users_pending.html src/call_manager/web/templates/admin_users.html src/call_manager/web/templates/admin.html tests/test_web_admin_users.py
git commit -m "feat(admin): user management — approval queue, list, state-restricted transitions, last-admin guard"
```

---

## Task 12: Bootstrap reactivates configured admins

**Files:**
- Modify: `src/call_manager/admin_bootstrap.py` (loop ~48-53)
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
        s.add(User(email="boss@ki.se", is_admin=False, status="disabled")); s.commit()
    bootstrap_admins(pg_session_factory, ["boss@ki.se"], ["ki.se"])
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="boss@ki.se").one()
        assert u.is_admin is True and u.status == "active"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_admin_bootstrap_status.py -v`
Expected: `test_creates_active_admin` FAILS (new users default `pending`); `test_reactivates...` FAILS (status never set).

- [ ] **Step 3: Update the bootstrap loop**

Replace the loop body (~48-53) with:

```python
        for email in valid:
            user = session.query(User).filter_by(email=email).one_or_none()
            if user is None:
                session.add(User(email=email, is_admin=True, status="active"))
            else:
                # Configured admins are the lockout-recovery path: ensure both
                # admin AND active on every startup. To permanently disable one,
                # remove them from INITIAL_ADMIN_EMAIL first.
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

## Task 13: Digest excludes non-active accounts

**Files:**
- Modify: `src/call_manager/digest/send.py` (extract candidate query into a tested helper + add status filter)
- Modify: `src/call_manager/digest/assemble.py` (guard `assemble_for_user` ~75-77)
- Test: `tests/test_digest_status_filter.py`

- [ ] **Step 1: Write the failing test (against the REAL helper, not a copy)**

```python
# tests/test_digest_status_filter.py
from datetime import UTC, datetime

from call_manager.digest.assemble import assemble_for_user
from call_manager.digest.send import candidate_user_ids
from call_manager.db.models import Profile, ProfileKind, Subscription, User


def _profile_owned_by(s, owner_id) -> int:
    p = Profile(name="P", kind=ProfileKind.ORGANIZATIONAL_UNIT, level="group",
                description="d", structured={}, created_by=owner_id)
    s.add(p); s.flush(); return p.id


def test_candidate_query_excludes_non_active(pg_session_factory):
    with pg_session_factory() as s:
        active = User(email="active@ki.se", status="active")
        disabled = User(email="disabled@ki.se", status="disabled")
        pending = User(email="pending@ki.se", status="pending")
        s.add_all([active, disabled, pending]); s.flush()
        for u in (active, disabled, pending):
            s.add(Subscription(user_id=u.id, profile_id=_profile_owned_by(s, u.id)))
        s.commit()
        ids = set(candidate_user_ids(s))
        emails = {s.get(User, i).email for i in ids}
    assert "active@ki.se" in emails
    assert "disabled@ki.se" not in emails and "pending@ki.se" not in emails


def test_assemble_returns_none_for_non_active(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="d@ki.se", status="disabled"); s.add(u); s.flush()
        s.add(Subscription(user_id=u.id, profile_id=_profile_owned_by(s, u.id))); s.commit()
        assert assemble_for_user(s, user_id=u.id, now=datetime.now(UTC)) is None
```

(If `Subscription` requires `digest_enabled`, set it `True`. Match the real `Subscription` constructor — see `db/models.py`.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_digest_status_filter.py -v`
Expected: FAIL — `candidate_user_ids` is not importable yet, and `assemble_for_user` lacks the status guard.

- [ ] **Step 3: Extract + filter the candidate query in `send.py`**

Add a module-level helper and call it from `run_digest`. Replace the inline candidate query (~67-76) with a call:

```python
def candidate_user_ids(session) -> list[int]:
    """Active, digest-enabled subscribers. Excludes disabled/pending accounts
    (status) and bounce-disabled digests (digest_disabled_at)."""
    return list(
        session.execute(
            select(distinct(Subscription.user_id))
            .join(User, User.id == Subscription.user_id)
            .where(User.digest_disabled_at.is_(None), User.status == "active")
        ).scalars().all()
    )
```

And in `run_digest`:

```python
    with session_factory() as session:
        candidate_ids = candidate_user_ids(session)
```

(Then iterate `candidate_ids`. `User`, `select`, `distinct`, `Subscription` are imported in `send.py`; add any that aren't.)

- [ ] **Step 4: Guard `assemble_for_user` (defense-in-depth)**

In `src/call_manager/digest/assemble.py`, change the early guard (~75-77):

```python
    user = session.get(User, user_id)
    if user is None or user.digest_disabled_at is not None:
        return None
```

to:

```python
    user = session.get(User, user_id)
    if user is None or user.status != "active" or user.digest_disabled_at is not None:
        return None
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_digest_status_filter.py -v`
Expected: all PASS.

- [ ] **Step 6: Regression**

Run: `uv run pytest tests/ -k digest -v`
Expected: PASS. Update any digest test that created a subscriber via `User(email=...)` to pass `status="active"`.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/digest/send.py src/call_manager/digest/assemble.py tests/test_digest_status_filter.py
git commit -m "feat(digest): exclude non-active accounts (selection + assemble guard)"
```

---

## Task 14: Login-page button + whole-suite green

**Files:**
- Modify: `src/call_manager/web/routes/auth.py` (`login_form` context)
- Modify: `src/call_manager/web/templates/login.html`
- Test: `tests/test_web_login_google_button.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_login_google_button.py
from fastapi.testclient import TestClient

from call_manager.main import create_app


def _env(monkeypatch, pg_url, enabled):
    for k, v in {
        "DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    if enabled:
        monkeypatch.setenv("GOOGLE_OIDC_ENABLED", "true")
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "sec")
    return create_app()


def test_button_shown_when_enabled(monkeypatch, pg_url):
    app = _env(monkeypatch, pg_url, enabled=True)
    with TestClient(app) as c:
        assert "/auth/google/start" in c.get("/login").text


def test_button_hidden_when_disabled(monkeypatch, pg_url):
    app = _env(monkeypatch, pg_url, enabled=False)
    with TestClient(app) as c:
        assert "/auth/google/start" not in c.get("/login").text
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_web_login_google_button.py -v`
Expected: `test_button_shown_when_enabled` FAILS.

- [ ] **Step 3: Pass the flag from `login_form`**

In `login_form`, add to the context dict:

```python
            "google_oidc_enabled": settings.google_oidc_enabled,
```

- [ ] **Step 4: Add the button to `login.html`**

```html
{% if google_oidc_enabled %}
<form method="post" action="/auth/google/start">
  {% if next_path %}<input type="hidden" name="next" value="{{ next_path }}" />{% endif %}
  <button type="submit">Continue with Google</button>
</form>
{% endif %}
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_web_login_google_button.py -v`
Expected: all PASS.

- [ ] **Step 6: Whole suite + lint**

Run:
```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q
```
Expected: lint clean; entire suite green. The likely regressions are tests that create a `User` without `status` and then authenticate — fix each by adding `status="active"`. Grep helpers: `grep -rn "User(email=" tests | grep -v "status="` and patch the ones whose user must be active.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/web/routes/auth.py src/call_manager/web/templates/login.html tests/test_web_login_google_button.py
git commit -m "feat(auth): Continue-with-Google button on the login page"
```

---

## Self-Review (completed during planning, updated after Codex round)

**Spec coverage** — every spec section maps to a task:
- OIDC client / PKCE / hardened id_token validation (RS256, essential claims, JWKS rotation) → Task 5
- Encrypted transaction cookie → Task 3
- `users` status (fail-closed default) + oidc columns + constraints + backfill → Task 2
- No auto-link; race-safe resolution; Connect-Google (session revalidation) → Tasks 6, 8, 9
- Clean-redirect callback contract (delete cookie + no-referrer + clean URL, all error/rate-limit/provider paths) → Task 8
- Central inactive enforcement (HTML redirect + cookie delete / API 403) → Task 7
- Magic-link login-only + consume-time status check + timing residual risk → Task 10 (+ spec)
- Admin approval queue + list page + state-restricted approve/deny/enable/disable/role + serialized last-admin guard + pagination → Task 11
- Bootstrap reactivation → Task 12
- Digest active filter (tested helper + assemble guard) → Task 13
- Config + prod guard + `.env.example` → Task 4
- Login button + Connect-Google UI → Tasks 9, 14
- Rate-limiting OAuth endpoints → Task 8

**Codex-round fixes verified present:**
1. Fail-closed default `pending` (Task 2 model + migration backfill; bootstrap explicit active Task 12).
2. RS256-only `JsonWebToken`, essential claims, `email_verified` in client, JWKS refetch-on-rotation (Task 5).
3. `_clean()` sets `no-referrer` + deletes cookie on EVERY callback exit incl. rate-limit/provider-error/missing-params (Task 8).
4. Admin transitions state-restricted (approve/deny pending-only, enable disabled-only, role-change route) + `with_for_update` serialized last-admin guard (Task 11).
5. Concrete `/admin/users` management page + pagination (Task 11).
6. Connect-Google revalidates `link_uid` status + `session_epoch`, fails closed when unauthenticated (Tasks 8, 9).
7. Magic-link timing oracle documented as residual risk (Task 10 + spec), not over-claimed.
8. Non-tautological digest test against the real `candidate_user_ids` helper + `assemble_for_user` guard; Task 6 has a real re-resolution race test; Task 7 asserts cookie deletion via the real handler; Task 8 has the full callback matrix; Task 10 references the correct `tests/test_auth_redirects.py`.

**Type/name consistency:** `OIDCClient`/`IdTokenInvalid`/`OIDCError`/`generate_pkce`/`generate_token` (Task 5) used verbatim in Tasks 7-8. `TransactionCipher.make/read` (Task 3) in Task 8. `AccountResolution.outcome` ∈ {matched, created, email_collision} (Task 6) matched in Task 8. `AccountInactive(status)` (Task 7) matched in Tasks 8/10. `candidate_user_ids` (Task 13) imported by its test. `_clean()` used for all callback exits (Task 8).

**Follow-ups to confirm during execution (not blockers):**
- Exact `{% block %}` name in `base.html` (templates assume `content`).
- `Subscription` constructor fields (`digest_enabled` default) for the digest test.
- Confirm `settings.session_max_age_seconds` attribute name (mirror `web/routes/auth.py`).
