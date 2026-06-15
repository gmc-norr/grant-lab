# Local Password Auth + Magic-Link Removal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace magic-link login with local email+password (self-register → admin issues credential on approval), keep Google OAuth2 + the approval gate, and add a credential break-glass admin — removing magic-link without breaking the shared email sender used by ingestion alerts.

**Architecture:** A small argon2id `passwords` module backs a password field on `users`. `/register` creates a credential-less `pending` request; the admin issues the initial password on approval. `/login` verifies the password (dummy-hash on every no-credential path) and branches on `status`. Magic-link routes/tokens/table/templates are deleted; `auth/email.py` is retained for ingestion alerts. `session_epoch` is bumped on every credential change.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x + Alembic + PostgreSQL 16, Jinja2/HTMX, argon2-cffi, pytest + testcontainers, `uv`/`ruff`.

**Spec:** [docs/superpowers/specs/2026-06-15-local-password-auth-and-magic-link-removal-design.md](../specs/2026-06-15-local-password-auth-and-magic-link-removal-design.md)

**Conventions:**
- `uv run pytest` / `uv run ruff check` / `uv run ruff format`. **Every task ends with the whole affected set green and a commit.** When a task deletes a symbol, it also removes/rewrites *every* test that imports it, in the same task (so the suite never goes red between tasks).
- Migrations: head is `0048`. Add **`0049`** (add `password_hash`, early) and **`0050`** (drop `magic_link_tokens`, late) — split because the test fixture truncates every *model* table, so the table can only be dropped once the `MagicLinkToken` model is gone too.
- Test apps: `create_app()` with `monkeypatch.setenv(...)`, then `app.state` overrides (see `tests/test_web_oauth_google.py`). DB tests use `pg_session_factory` / `pg_url`.
- **Magic-link-dependent tests to handle during this plan:** `test_auth_tokens.py`, `test_login_flow.py`, `test_web_auth_magic_link_gate.py`, `test_web_auth.py` (Task 7); `test_models.py` (Task 8). `test_auth_email.py` / `test_email_smtp.py` test the *retained* sender classes and stay (only delete cases that exercise magic-link login, if any).

---

## Task 1: Add argon2-cffi dependency

**Files:** Modify `pyproject.toml`

- [ ] **Step 1: Add the dependency** — in `pyproject.toml` `dependencies`, next to `authlib`:

```toml
    "argon2-cffi>=23.1",
```

- [ ] **Step 2: Sync + verify** — Run: `uv sync && uv run python -c "import argon2; print('ok')"` → prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add argon2-cffi for local password hashing"
```

---

## Task 2: Password hashing module

**Files:** Create `src/call_manager/auth/passwords.py`; Test `tests/test_auth_passwords.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auth_passwords.py
import pytest

from call_manager.auth.passwords import (
    MIN_PASSWORD_LENGTH,
    WeakPassword,
    hash_password,
    validate_password,
    verify_password,
)


def test_hash_and_verify_roundtrip():
    h = hash_password("correct horse battery")
    assert h != "correct horse battery"
    assert verify_password(h, "correct horse battery") is True
    assert verify_password(h, "wrong") is False


def test_validate_rejects_short():
    with pytest.raises(WeakPassword):
        validate_password("a" * (MIN_PASSWORD_LENGTH - 1))
    validate_password("a" * MIN_PASSWORD_LENGTH)  # no raise


def test_hash_rejects_short():
    with pytest.raises(WeakPassword):
        hash_password("short")


def test_verify_none_hash_is_false_but_runs():
    assert verify_password(None, "anything") is False
    assert verify_password(None, "x" * 16) is False


def test_verify_tolerates_malformed_hash():
    assert verify_password("not-a-real-argon2-hash", "whatever") is False
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_auth_passwords.py -v` → FAIL (no module).

- [ ] **Step 3: Implement**

```python
# src/call_manager/auth/passwords.py
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 12

_ph = PasswordHasher()
# Precomputed dummy hash: verifying against it on the no-credential path costs
# the same as a real verify, so timing doesn't reveal whether a credential exists.
_DUMMY_HASH = _ph.hash("x" * 16)


class WeakPassword(ValueError):
    """Password does not meet the minimum policy."""


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(f"password must be at least {MIN_PASSWORD_LENGTH} characters")


def hash_password(password: str) -> str:
    validate_password(password)
    return _ph.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Verify `password` against `password_hash`. When `password_hash` is None,
    verify against a dummy hash (timing parity) and always return False."""
    target = password_hash if password_hash is not None else _DUMMY_HASH
    try:
        _ph.verify(target, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return password_hash is not None
```

- [ ] **Step 4: Run to verify it passes** — `uv run pytest tests/test_auth_passwords.py -v` → PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/auth/passwords.py
git add src/call_manager/auth/passwords.py tests/test_auth_passwords.py
git commit -m "feat(auth): argon2id password hashing module"
```

---

## Task 3: Add `password_hash` to users (model + migration 0049)

**Files:** Modify `src/call_manager/db/models.py`; Create `alembic/versions/0049_add_password_hash.py`; Test `tests/test_user_password_hash_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_user_password_hash_model.py
from call_manager.db.models import User


def test_password_hash_defaults_none_and_persists(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="pw@ki.se", status="active")
        s.add(u)
        s.commit()
        s.refresh(u)
        assert u.password_hash is None
        u.password_hash = "argon2-hash-here"
        s.commit()
        s.refresh(u)
        assert u.password_hash == "argon2-hash-here"
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_user_password_hash_model.py -v` → FAIL.

- [ ] **Step 3: Add the column** — in `class User`, after the `oidc_sub` column:

```python
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
```

(`Text` already imported.) Leave `MagicLinkToken` + the relationship for Task 8.

- [ ] **Step 4: Create migration 0049**

```python
# alembic/versions/0049_add_password_hash.py
"""add users.password_hash

Revision ID: 0049
Revises: 0048
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "password_hash")
```

- [ ] **Step 5: Run to verify it passes** — `uv run pytest tests/test_user_password_hash_model.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/db/models.py alembic/versions/0049_add_password_hash.py tests/test_user_password_hash_model.py
git commit -m "feat(db): add users.password_hash (migration 0049)"
```

---

## Task 4: Config — `INITIAL_ADMIN_PASSWORD`

**Files:** Modify `src/call_manager/config.py`, `.env.example`; Test `tests/test_config_initial_admin_password.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_initial_admin_password.py
import pytest

from call_manager.config import Settings


def _base(monkeypatch, **extra):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x/y")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    return Settings()


def test_default_none(monkeypatch):
    assert _base(monkeypatch).initial_admin_password is None


def test_empty_is_none(monkeypatch):
    assert _base(monkeypatch, INITIAL_ADMIN_PASSWORD="   ").initial_admin_password is None


def test_short_nonempty_rejected(monkeypatch):
    with pytest.raises(ValueError, match="INITIAL_ADMIN_PASSWORD"):
        _base(monkeypatch, INITIAL_ADMIN_PASSWORD="short")


def test_valid_kept_unmodified(monkeypatch):
    # surrounding spaces are part of the secret — must NOT be stripped
    s = _base(monkeypatch, INITIAL_ADMIN_PASSWORD="  spaced-strong-pw  ")
    assert s.initial_admin_password == "  spaced-strong-pw  "
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_config_initial_admin_password.py -v` → FAIL.

- [ ] **Step 3: Add the field + validator** — add the field near `initial_admin_emails`:

```python
    initial_admin_password: str | None = Field(default=None, alias="INITIAL_ADMIN_PASSWORD")
```

Add the validator (use `.strip()` only to *detect* whitespace-only; return the
original value unchanged so a password with surrounding spaces survives):

```python
    @field_validator("initial_admin_password", mode="after")
    @classmethod
    def _validate_initial_admin_password(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None  # None / whitespace-only -> unset (no trivial credential)
        if len(v) < 12:
            raise ValueError(
                "INITIAL_ADMIN_PASSWORD must be at least 12 characters (or empty/unset)."
            )
        return v  # unchanged — surrounding whitespace may be part of the password
```

- [ ] **Step 4: Update `.env.example`**

```bash
# Break-glass admin credential. While set, every configured INITIAL_ADMIN_EMAIL
# admin's password is (re)set to this on each startup. Must be >= 12 chars
# (empty = unset). UNSET it once you've logged in and want to manage the password
# in-app, otherwise a restart overwrites your chosen password.
# INITIAL_ADMIN_PASSWORD=
```

- [ ] **Step 5: Run to verify it passes** — `uv run pytest tests/test_config_initial_admin_password.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/config.py .env.example tests/test_config_initial_admin_password.py
git commit -m "feat(config): INITIAL_ADMIN_PASSWORD (>=12 or unset; preserved verbatim)"
```

---

## Task 5: Bootstrap admin applies the password

**Files:** Modify `src/call_manager/admin_bootstrap.py`, `src/call_manager/main.py`; Test `tests/test_admin_bootstrap_password.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_admin_bootstrap_password.py
from call_manager.admin_bootstrap import bootstrap_admins
from call_manager.auth.passwords import verify_password
from call_manager.db.models import User


def test_sets_password_when_provided(pg_session_factory):
    bootstrap_admins(pg_session_factory, ["boss@ki.se"], ["ki.se"], password="a-strong-enough-pw")
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="boss@ki.se").one()
        assert u.is_admin and u.status == "active"
        assert verify_password(u.password_hash, "a-strong-enough-pw") is True


def test_reapplies_and_bumps_epoch_on_second_run(pg_session_factory):
    bootstrap_admins(pg_session_factory, ["boss@ki.se"], ["ki.se"], password="a-strong-enough-pw")
    with pg_session_factory() as s:
        ep1 = s.query(User).filter_by(email="boss@ki.se").one().session_epoch
    bootstrap_admins(pg_session_factory, ["boss@ki.se"], ["ki.se"], password="a-strong-enough-pw")
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="boss@ki.se").one()
        assert u.session_epoch == ep1 + 1
        assert verify_password(u.password_hash, "a-strong-enough-pw") is True


def test_no_password_leaves_hash_untouched(pg_session_factory):
    bootstrap_admins(pg_session_factory, ["boss@ki.se"], ["ki.se"], password=None)
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="boss@ki.se").one()
        assert u.is_admin and u.status == "active" and u.password_hash is None
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_admin_bootstrap_password.py -v` → FAIL.

- [ ] **Step 3: Update `bootstrap_admins`** — signature + import + loop:

```python
def bootstrap_admins(
    session_factory: sessionmaker,
    emails: Iterable[str],
    allowed_domains: list[str],
    password: str | None = None,
) -> None:
```

Add at the top: `from call_manager.auth.passwords import hash_password`. Replace the
`with session_factory() as session:` loop body:

```python
    with session_factory() as session:
        for email in valid:
            user = session.query(User).filter_by(email=email).one_or_none()
            if user is None:
                user = User(email=email, is_admin=True, status="active")
                session.add(user)
            else:
                # Configured admins are the lockout-recovery path: ensure both
                # admin AND active on every startup. To permanently disable one,
                # remove them from INITIAL_ADMIN_EMAIL first.
                user.is_admin = True
                user.status = "active"
            if password is not None:
                # Break-glass: (re)apply the configured password every startup and
                # bump the epoch so any session under an old password is revoked.
                user.password_hash = hash_password(password)
                user.session_epoch = (user.session_epoch or 0) + 1
        session.commit()
```

- [ ] **Step 4: Pass the password from `main.py`** — in the `lifespan` `bootstrap_admins(...)` call (~line 67):

```python
    bootstrap_admins(
        app.state.session_factory,
        emails=app.state.settings.initial_admin_emails,
        allowed_domains=app.state.settings.allowed_email_domains,
        password=app.state.settings.initial_admin_password,
    )
```

- [ ] **Step 5: Run to verify it passes** — `uv run pytest tests/test_admin_bootstrap_password.py tests/test_admin_bootstrap_status.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/admin_bootstrap.py src/call_manager/main.py tests/test_admin_bootstrap_password.py
git commit -m "feat(auth): bootstrap admin applies INITIAL_ADMIN_PASSWORD (break-glass)"
```

---

## Task 6: Registration request route

**Files:** Create `src/call_manager/web/routes/register.py`, `src/call_manager/web/templates/register.html`, `src/call_manager/web/templates/register_received.html`; Modify `src/call_manager/main.py`; Test `tests/test_web_register.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_register.py
from fastapi.testclient import TestClient
from sqlalchemy import select

import call_manager.web.routes.register as reg
from call_manager.db.models import User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def test_register_creates_pending_request_no_password_truncates_name(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.post("/register", data={"email": "new@ki.se", "name": "z" * 300},
                   follow_redirects=False)
        assert r.status_code == 200
    with pg_session_factory() as s:
        u = s.execute(select(User).where(User.email == "new@ki.se")).scalar_one()
        assert u.status == "pending" and u.password_hash is None and len(u.name) == 200


def test_duplicate_via_integrity_error_no_second_row_identical_body(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        s.add(User(email="dupe@ki.se", status="active")); s.commit()
    with TestClient(app) as c:
        fresh = c.post("/register", data={"email": "fresh@ki.se"})
        dup = c.post("/register", data={"email": "dupe@ki.se"})
        assert dup.status_code == 200
        assert dup.text == fresh.text  # identical body -> no enumeration
    with pg_session_factory() as s:
        assert len(s.execute(select(User).where(User.email == "dupe@ki.se")).scalars().all()) == 1


def test_dummy_hash_invoked_on_every_eligible_attempt(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        s.add(User(email="dupe@ki.se", status="active")); s.commit()
    calls = []
    real = reg.verify_password
    monkeypatch.setattr(reg, "verify_password", lambda h, p: (calls.append(h), real(h, p))[1])
    with TestClient(app) as c:
        c.post("/register", data={"email": "new2@ki.se"})  # fresh
        c.post("/register", data={"email": "dupe@ki.se"})  # duplicate
    assert calls == [None, None]  # dummy-hash on BOTH paths


def test_register_domain_disallowed(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        c.post("/register", data={"email": "x@evil.com"})
    with pg_session_factory() as s:
        assert s.execute(select(User).where(User.email == "x@evil.com")).scalar_one_or_none() is None
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_web_register.py -v` → FAIL (404).

- [ ] **Step 3: Implement the router**

```python
# src/call_manager/web/routes/register.py
from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import IntegrityError

from call_manager.auth.domain import domain_allowed
from call_manager.auth.passwords import verify_password
from call_manager.db.models import User
from call_manager.web.routes.auth import MAX_EMAIL_LEN, _client_ip, _email_fingerprint

_logger = logging.getLogger(__name__)
router = APIRouter()

_NAME_MAX = 200  # users.name is VARCHAR(200)


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    settings = request.app.state.settings
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="register.html",
        context={"allowed_domains": settings.allowed_email_domains},
    )


@router.post("/register", response_class=HTMLResponse)
def register_submit(request: Request, email: str = Form(...), name: str = Form(default="")):
    settings = request.app.state.settings
    ip_limiter = request.app.state.ip_limiter
    email_limiter = request.app.state.email_limiter
    session_factory = request.app.state.session_factory

    def _received() -> HTMLResponse:
        return request.app.state.templates.TemplateResponse(
            request=request, name="register_received.html", context={}
        )

    if len(email) > MAX_EMAIL_LEN:
        return _received()
    email_normalized = email.strip().lower()
    if not ip_limiter.allow(_client_ip(request)):
        return _received()
    if not domain_allowed(email_normalized, settings.allowed_email_domains):
        return _received()
    if not email_limiter.allow(email_normalized):
        return _received()

    # Eligible attempt: ONE dummy argon2 for timing parity (registration sets no
    # password) — on BOTH the fresh and the duplicate path. Then insert-then-catch
    # (no pre-SELECT): the unique-email constraint makes a duplicate raise
    # IntegrityError, which we treat identically. Race-safe vs concurrent
    # local/Google registration.
    verify_password(None, "timing-parity")
    with session_factory() as session:
        session.add(
            User(email=email_normalized, name=(name.strip()[:_NAME_MAX] or None), status="pending")
        )
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            _logger.info(
                "register: email already exists",
                extra={"event": "register_duplicate", "email_fp": _email_fingerprint(email_normalized)},
            )
            return _received()
        session.commit()

    _logger.info(
        "register: request created",
        extra={"event": "register_created", "email_fp": _email_fingerprint(email_normalized)},
    )
    return _received()
```

- [ ] **Step 4: Create `register.html`**

```html
{% extends "base.html" %}
{% block title %}Request access — Call Manager{% endblock %}
{% block content %}
<div class="narrow"><div class="auth-card">
  <hgroup style="text-align:center">
    <h1>Request access</h1>
    <p>Enter your hospital email. An administrator will review your request and
       set up your account.</p>
  </hgroup>
  <form method="post" action="/register">
    <label>Email
      <input type="email" name="email" required autofocus autocomplete="email"
             placeholder="you@{{ allowed_domains[0] if allowed_domains else 'example.org' }}">
    </label>
    <label>Name (optional)
      <input type="text" name="name" autocomplete="name" maxlength="200">
    </label>
    {% if allowed_domains %}<small>Use your hospital email ({{ allowed_domains | join(' or ') }}).</small>{% endif %}
    <button type="submit">Request access</button>
  </form>
  <p style="text-align:center"><a href="/login">Back to sign in</a></p>
</div></div>
{% endblock %}
```

- [ ] **Step 5: Create `register_received.html`** (dedicated copy — not the sign-in page)

```html
{% extends "base.html" %}
{% block title %}Request received — Call Manager{% endblock %}
{% block content %}
<div class="narrow"><div class="auth-card">
  <hgroup style="text-align:center">
    <h1>Request received</h1>
    <p>Thanks — your access request has been recorded. An administrator will review
       it and set up your account. You'll be able to sign in once it's approved.</p>
  </hgroup>
  <p style="text-align:center"><a href="/login">Back to sign in</a></p>
</div></div>
{% endblock %}
```

- [ ] **Step 6: Wire the router in `main.py`**

```python
from call_manager.web.routes import register
...
    app.include_router(register.router)
```

- [ ] **Step 7: Run to verify + lint** — `uv run pytest tests/test_web_register.py -v` → PASS; `uv run ruff check src/call_manager/web/routes/register.py` → clean.

- [ ] **Step 8: Commit**

```bash
git add src/call_manager/web/routes/register.py src/call_manager/web/templates/register.html src/call_manager/web/templates/register_received.html src/call_manager/main.py tests/test_web_register.py
git commit -m "feat(auth): /register access-request (no password; insert-then-catch; uniform timing)"
```

---

## Task 7: Password login; remove magic-link routes/tokens/templates **and their tests**

**Files:**
- Modify: `src/call_manager/web/routes/auth.py`, `src/call_manager/web/templates/login.html`
- Delete: `src/call_manager/auth/tokens.py`, `login_sent.html`, `login_confirm.html`, `login_link_expired.html`, `tests/test_auth_tokens.py`, `tests/test_login_flow.py`, `tests/test_web_auth_magic_link_gate.py`
- Modify: `tests/test_web_auth.py` (drop magic-link cases)
- Test: `tests/test_web_login_password.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_login_password.py
from fastapi.testclient import TestClient

import call_manager.web.routes.auth as authmod
from call_manager.auth.passwords import hash_password
from call_manager.db.models import User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def _user(sf, *, email, status, password=None):
    with sf() as s:
        u = User(email=email, status=status,
                 password_hash=hash_password(password) if password else None)
        s.add(u); s.commit(); s.refresh(u); return u.id


def test_active_user_logs_in(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _user(app.state.session_factory, email="a@ki.se", status="active", password="correct-horse-1")
    with TestClient(app) as c:
        r = c.post("/login", data={"email": "a@ki.se", "password": "correct-horse-1"},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert "session=" in "".join(r.headers.get_list("set-cookie"))


def test_failure_responses_are_identical(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _user(app.state.session_factory, email="a@ki.se", status="active", password="correct-horse-1")
    _user(app.state.session_factory, email="g@ki.se", status="active", password=None)  # oauth-only
    with TestClient(app) as c:
        wrong = c.post("/login", data={"email": "a@ki.se", "password": "nope-long-enough"})
        unknown = c.post("/login", data={"email": "ghost@ki.se", "password": "nope-long-enough"})
        oauth = c.post("/login", data={"email": "g@ki.se", "password": "nope-long-enough"})
    assert wrong.status_code == unknown.status_code == oauth.status_code == 200
    assert "invalid email or password" in wrong.text.lower()
    assert wrong.text == unknown.text == oauth.text  # no enumeration by body
    for r in (wrong, unknown, oauth):
        assert "set-cookie" not in {k.lower() for k in r.headers}


def test_dummy_hash_invoked_for_unknown_email(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    seen = []
    real = authmod.verify_password
    monkeypatch.setattr(authmod, "verify_password", lambda h, p: (seen.append(h), real(h, p))[1])
    with TestClient(app) as c:
        c.post("/login", data={"email": "ghost@ki.se", "password": "whatever-long"})
    assert seen == [None]  # verified against dummy (stored hash is None)


def test_pending_and_disabled_branch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _user(app.state.session_factory, email="p@ki.se", status="pending", password="correct-horse-1")
    _user(app.state.session_factory, email="d@ki.se", status="disabled", password="correct-horse-1")
    with TestClient(app) as c:
        rp = c.post("/login", data={"email": "p@ki.se", "password": "correct-horse-1"}, follow_redirects=False)
        rd = c.post("/login", data={"email": "d@ki.se", "password": "correct-horse-1"}, follow_redirects=False)
    assert rp.headers["location"] == "/auth/pending"
    assert rd.headers["location"] == "/auth/no-access"


def test_repeated_bad_logins_rate_limited(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _user(app.state.session_factory, email="a@ki.se", status="active", password="correct-horse-1")
    with TestClient(app) as c:
        for _ in range(5):  # per-email limiter is 5/15min
            c.post("/login", data={"email": "a@ki.se", "password": "bad-but-long"})
        # 6th attempt with the CORRECT password is now rate-limited -> no session
        r = c.post("/login", data={"email": "a@ki.se", "password": "correct-horse-1"},
                   follow_redirects=False)
    assert r.status_code == 200 and "set-cookie" not in {k.lower() for k in r.headers}


def test_magic_link_routes_gone(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        assert c.get("/login/verify?token=x", follow_redirects=False).status_code == 404
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_web_login_password.py -v` → FAIL.

- [ ] **Step 3: Rewrite `auth.py`** — replace the imports (lines 1-17):

```python
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Cookie, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from call_manager.auth.passwords import verify_password
from call_manager.auth.redirects import safe_next
from call_manager.auth.session_cookie import InvalidSession
from call_manager.db.models import User
```

Keep `_email_fingerprint`, `_client_ip`, `MAX_EMAIL_LEN`. Replace `login_form` to add `error`:

```python
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str | None = None):
    settings = request.app.state.settings
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "next_path": safe_next(next),
            "allowed_domains": settings.allowed_email_domains,
            "google_oidc_enabled": settings.google_oidc_enabled,
            "error": None,
        },
    )
```

Replace the whole `login_submit` AND delete `login_verify_confirm` + `login_verify_consume`:

```python
@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str | None = Form(default=None),
):
    settings = request.app.state.settings
    email_limiter = request.app.state.email_limiter
    ip_limiter = request.app.state.ip_limiter
    session_factory = request.app.state.session_factory
    session_signer = request.app.state.session_signer
    safe = safe_next(next)

    def _denied() -> HTMLResponse:
        # One generic message for unknown email / wrong password / no-credential /
        # rate-limited. Status 200 so the global 401->/login handler can't loop.
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "next_path": safe,
                "allowed_domains": settings.allowed_email_domains,
                "google_oidc_enabled": settings.google_oidc_enabled,
                "error": "Invalid email or password.",
            },
        )

    if len(email) > MAX_EMAIL_LEN:
        return _denied()
    email_normalized = email.strip().lower()
    email_fp = _email_fingerprint(email_normalized)

    if not ip_limiter.allow(_client_ip(request)) or not email_limiter.allow(email_normalized):
        _logger.info("login rate-limited", extra={"event": "login_rate_limited", "email_fp": email_fp})
        return _denied()

    with session_factory() as session:
        user = session.execute(
            select(User).where(User.email == email_normalized)
        ).scalar_one_or_none()
        # verify_password runs argon2 even when user is None / has no hash
        # (dummy path), so timing doesn't reveal account existence.
        stored = user.password_hash if user is not None else None
        if not verify_password(stored, password):
            _logger.info("login failed", extra={"event": "login_failed", "email_fp": email_fp})
            return _denied()
        if user.status != "active":
            target = "/auth/pending" if user.status == "pending" else "/auth/no-access"
            return RedirectResponse(url=target, status_code=303)
        user.last_login_at = datetime.now(UTC)
        user_id = user.id
        session_epoch = user.session_epoch
        session.commit()

    response = RedirectResponse(url=safe or "/", status_code=303)
    response.set_cookie(
        "session",
        session_signer.make(user_id=user_id, session_epoch=session_epoch),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    _logger.info("login success", extra={"event": "login_success", "email_fp": email_fp})
    return response
```

`logout_submit` unchanged.

- [ ] **Step 4: Rewrite `login.html`** — replace the `<hgroup>`…first `<form>`…Google block (lines 16-37) with:

```html
    <hgroup style="text-align:center">
      <h1>Sign in</h1>
      <p>Enter your email and password.</p>
    </hgroup>
    {% if error %}<p role="alert" style="color:var(--pico-color-red-500,#b00)">{{ error }}</p>{% endif %}
    <form method="post" action="/login">
      {% if next_path %}<input type="hidden" name="next" value="{{ next_path }}">{% endif %}
      <label>Email
        <input type="email" name="email" required autofocus autocomplete="email"
               placeholder="you@{{ allowed_domains[0] if allowed_domains else 'example.org' }}">
      </label>
      <label>Password
        <input type="password" name="password" required autocomplete="current-password">
      </label>
      <button type="submit">Sign in</button>
    </form>
    {% if google_oidc_enabled %}
    <form method="post" action="/auth/google/start">
      {% if next_path %}<input type="hidden" name="next" value="{{ next_path }}" />{% endif %}
      <button type="submit" class="secondary">Continue with Google</button>
    </form>
    {% endif %}
    <p style="text-align:center"><a href="/register">Request access</a></p>
```

- [ ] **Step 5: Delete magic-link source + dependent tests**

```bash
git rm src/call_manager/auth/tokens.py \
       src/call_manager/web/templates/login_sent.html \
       src/call_manager/web/templates/login_confirm.html \
       src/call_manager/web/templates/login_link_expired.html \
       tests/test_auth_tokens.py \
       tests/test_login_flow.py \
       tests/test_web_auth_magic_link_gate.py
```

- [ ] **Step 6: Fix `tests/test_web_auth.py`** — open it and remove every test that imports
`call_manager.auth.tokens`, calls `issue_token`/`consume_token`, or hits
`/login/verify` / `login_sent` / `login_confirm` (the magic-link flow). Keep tests
that still apply (e.g. `logout`, security headers). If the file becomes only
magic-link tests, `git rm` it. Re-run it after editing:
`uv run pytest tests/test_web_auth.py -v` → PASS (or removed).

- [ ] **Step 7: Run the affected set green**

```bash
uv run pytest tests/test_web_login_password.py tests/test_web_auth.py -v
uv run ruff check src/call_manager/web/routes/auth.py
```
Expected: PASS; ruff clean.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(auth): password login; remove magic-link routes/tokens/templates + their tests"
```

---

## Task 8: Remove `MagicLinkToken` model + drop table (migration 0050)

**Files:** Modify `src/call_manager/db/models.py`, `tests/test_models.py`; Create `alembic/versions/0050_drop_magic_link_tokens.py`; Test `tests/test_migration_0050_magic_link.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migration_0050_magic_link.py
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from call_manager.db.base import Base
from call_manager.db.session import make_engine


def test_model_no_longer_defines_table():
    assert "magic_link_tokens" not in Base.metadata.tables


def test_table_dropped_at_head(migrated_engine):
    assert "magic_link_tokens" not in inspect(migrated_engine).get_table_names()


def test_0049_0050_round_trip(pg_url):
    # Serial test suite: downgrade past both, assert, then restore head.
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    eng = make_engine(pg_url)
    try:
        command.downgrade(cfg, "0048")
        cols = {c["name"] for c in inspect(eng).get_columns("users")}
        assert "password_hash" not in cols
        assert "magic_link_tokens" in inspect(eng).get_table_names()
        command.upgrade(cfg, "head")
        cols = {c["name"] for c in inspect(eng).get_columns("users")}
        assert "password_hash" in cols
        assert "magic_link_tokens" not in inspect(eng).get_table_names()
    finally:
        eng.dispose()
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_migration_0050_magic_link.py -v` → FAIL.

- [ ] **Step 3: Remove the model + relationship** — in `src/call_manager/db/models.py`:
delete the `magic_link_tokens: Mapped[list[MagicLinkToken]] = relationship(...)` line
from `class User`, and delete the entire `class MagicLinkToken(Base): ...` block.

- [ ] **Step 4: Fix `tests/test_models.py`** — remove `MagicLinkToken` from the
import on line 6 and delete any test function that constructs/queries it (e.g. a
token-insert/expiry test). Keep the User/Profile tests. Re-run:
`uv run pytest tests/test_models.py -v` → PASS.

- [ ] **Step 5: Create migration 0050** (downgrade recreates the table with the
**named** constraints from `0001`):

```python
# alembic/versions/0050_drop_magic_link_tokens.py
"""drop magic_link_tokens

Revision ID: 0050
Revises: 0049
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("magic_link_tokens")


def downgrade() -> None:
    op.create_table(
        "magic_link_tokens",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="fk_mlt_user_id"),
        sa.UniqueConstraint("token_hash", name="uq_mlt_token_hash"),
    )
```

- [ ] **Step 6: Run to verify it passes** — `uv run pytest tests/test_migration_0050_magic_link.py tests/test_models.py -v` → PASS.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/db/models.py tests/test_models.py alembic/versions/0050_drop_magic_link_tokens.py tests/test_migration_0050_magic_link.py
git commit -m "feat(db): drop magic_link_tokens table + model (migration 0050; round-trip tested)"
```

---

## Task 9: Admin approve issues password (bumps epoch); admin reset

**Files:** Modify `src/call_manager/web/routes/admin.py`, `admin_users_pending.html`, `admin_users.html`, `tests/test_web_admin_users.py`; Test `tests/test_web_admin_passwords.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_admin_passwords.py
from fastapi.testclient import TestClient

from call_manager.auth.passwords import verify_password
from call_manager.db.models import User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def _admin_client(app, sf):
    with sf() as s:
        a = User(email="admin@ki.se", status="active", is_admin=True)
        s.add(a); s.commit(); s.refresh(a); aid = a.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=aid))
    return c


def test_approve_local_requires_password_and_bumps_epoch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url); c = _admin_client(app, app.state.session_factory)
    with pg_session_factory() as s:
        p = User(email="local@ki.se", status="pending")  # no oidc, no password
        s.add(p); s.commit(); s.refresh(p); pid, ep0 = p.id, p.session_epoch
    assert c.post(f"/admin/users/{pid}/approve", data={"role": "member"},
                  follow_redirects=False).status_code == 400  # no password -> rejected
    c.post(f"/admin/users/{pid}/approve",
           data={"role": "member", "initial_password": "issued-strong-pw"}, follow_redirects=False)
    with pg_session_factory() as s:
        u = s.get(User, pid)
        assert u.status == "active" and verify_password(u.password_hash, "issued-strong-pw")
        assert u.session_epoch == ep0 + 1  # credential issued -> epoch bumped


def test_approve_oauth_pending_without_password(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url); c = _admin_client(app, app.state.session_factory)
    with pg_session_factory() as s:
        p = User(email="goog@ki.se", status="pending", oidc_iss="i", oidc_sub="s")
        s.add(p); s.commit(); s.refresh(p); pid = p.id
    assert c.post(f"/admin/users/{pid}/approve", data={"role": "member"},
                  follow_redirects=False).status_code == 303
    with pg_session_factory() as s:
        assert s.get(User, pid).status == "active"


def test_admin_reset_password_bumps_epoch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url); c = _admin_client(app, app.state.session_factory)
    with pg_session_factory() as s:
        u = User(email="u@ki.se", status="active"); s.add(u); s.commit(); s.refresh(u)
        uid, ep0 = u.id, u.session_epoch
    c.post(f"/admin/users/{uid}/reset-password", data={"new_password": "fresh-strong-pw"},
           follow_redirects=False)
    with pg_session_factory() as s:
        u = s.get(User, uid)
        assert verify_password(u.password_hash, "fresh-strong-pw") and u.session_epoch == ep0 + 1


def test_reset_rejects_short(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url); c = _admin_client(app, app.state.session_factory)
    with pg_session_factory() as s:
        u = User(email="u2@ki.se", status="active"); s.add(u); s.commit(); s.refresh(u); uid = u.id
    assert c.post(f"/admin/users/{uid}/reset-password", data={"new_password": "short"},
                  follow_redirects=False).status_code == 400
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_web_admin_passwords.py -v` → FAIL.

- [ ] **Step 3: Update `admin.py`** — add import `from call_manager.auth.passwords import WeakPassword, hash_password`. Replace `approve_user`:

```python
@router.post("/admin/users/{user_id}/approve")
def approve_user(
    request: Request,
    user_id: int,
    role: str = Form("member"),
    initial_password: str = Form(default=""),
    admin: User = Depends(require_admin),  # noqa: B008
):
    if role not in ("member", "admin"):
        raise HTTPException(status_code=400, detail="invalid role")
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if u.status != "pending":
            raise HTTPException(status_code=400, detail="user is not pending")
        # Local (non-OAuth) accounts need an admin-issued password to log in.
        if u.oidc_sub is None and not initial_password:
            raise HTTPException(status_code=400, detail="initial password required")
        if initial_password:
            try:
                u.password_hash = hash_password(initial_password)
            except WeakPassword as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            u.session_epoch = (u.session_epoch or 0) + 1  # credential issued -> revoke prior
        u.status = "active"
        u.is_admin = role == "admin"
        u.approved_by_user_id = admin.id
        u.approved_at = datetime.now(UTC)
        db.commit()
    return RedirectResponse(url="/admin/users/pending", status_code=303)
```

Add the reset route next to the other `/admin/users/{id}/...` routes:

```python
@router.post("/admin/users/{user_id}/reset-password")
def reset_password(
    request: Request,
    user_id: int,
    new_password: str = Form(...),
    admin: User = Depends(require_admin),  # noqa: B008
):
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        try:
            u.password_hash = hash_password(new_password)
        except WeakPassword as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        u.session_epoch = (u.session_epoch or 0) + 1  # revoke the target's sessions
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)
```

- [ ] **Step 4: Update admin templates** — in `admin_users_pending.html`, add an
`initial_password` field to both approve forms (required only for non-Google accounts):

```html
    <form method="post" action="/admin/users/{{ u.id }}/approve" style="display:inline">
      <input type="hidden" name="role" value="member" />
      {% if not u.google_linked %}<input type="password" name="initial_password" placeholder="initial password" required>{% endif %}
      <button type="submit">Approve as member</button>
    </form>
    <form method="post" action="/admin/users/{{ u.id }}/approve" style="display:inline">
      <input type="hidden" name="role" value="admin" />
      {% if not u.google_linked %}<input type="password" name="initial_password" placeholder="initial password" required>{% endif %}
      <button type="submit">Approve as admin</button>
    </form>
```

In `admin_users.html`, add a reset control per row:

```html
            <form method="post" action="/admin/users/{{ u.id }}/reset-password" style="display:inline">
              <input type="password" name="new_password" placeholder="new password" required>
              <button type="submit" class="secondary">Reset password</button></form>
```

- [ ] **Step 5: Fix existing approve tests** — in `tests/test_web_admin_users.py`, the
local (non-`oidc_sub`) pending users in the approve tests now need `initial_password`
passed (else 400). Add `"initial_password": "issued-strong-pw"` to those approve
POSTs. Re-run: `uv run pytest tests/test_web_admin_users.py -v` → PASS.

- [ ] **Step 6: Run to verify it passes** — `uv run pytest tests/test_web_admin_passwords.py tests/test_web_admin_users.py -v` → PASS.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/web/routes/admin.py src/call_manager/web/templates/admin_users_pending.html src/call_manager/web/templates/admin_users.html tests/test_web_admin_passwords.py tests/test_web_admin_users.py
git commit -m "feat(admin): issue password on approval (bumps epoch); admin reset-password"
```

---

## Task 10: Change own password (`/me/password`)

**Files:** Modify `src/call_manager/web/routes/me.py`, `me.html`; Test `tests/test_web_me_password.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_me_password.py
from fastapi.testclient import TestClient

from call_manager.auth.passwords import hash_password, verify_password
from call_manager.db.models import User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def _client(app, sf, password=None):
    with sf() as s:
        u = User(email="u@ki.se", status="active",
                 password_hash=hash_password(password) if password else None)
        s.add(u); s.commit(); s.refresh(u); uid, ep = u.id, u.session_epoch
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid, session_epoch=ep))
    return c, uid


def test_change_password_requires_current_and_bumps_epoch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, uid = _client(app, app.state.session_factory, password="old-strong-pw")
    with pg_session_factory() as s:
        ep0 = s.get(User, uid).session_epoch
    r = c.post("/me/password",
               data={"current_password": "old-strong-pw", "new_password": "new-strong-pw"},
               follow_redirects=False)
    assert r.status_code == 303
    assert "session=" in "".join(r.headers.get_list("set-cookie"))  # re-minted
    with pg_session_factory() as s:
        u = s.get(User, uid)
        assert verify_password(u.password_hash, "new-strong-pw") and u.session_epoch == ep0 + 1


def test_change_password_wrong_current_rejected(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, uid = _client(app, app.state.session_factory, password="old-strong-pw")
    assert c.post("/me/password",
                  data={"current_password": "WRONG", "new_password": "new-strong-pw"},
                  follow_redirects=False).status_code == 400


def test_oauth_only_user_cannot_set_first_password(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, uid = _client(app, app.state.session_factory, password=None)  # no hash
    assert c.post("/me/password",
                  data={"current_password": "", "new_password": "new-strong-pw"},
                  follow_redirects=False).status_code == 400
    with pg_session_factory() as s:
        assert s.get(User, uid).password_hash is None
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_web_me_password.py -v` → FAIL.

- [ ] **Step 3: Implement the route** — in `me.py` add import
`from call_manager.auth.passwords import WeakPassword, hash_password, verify_password`,
add `"has_password": user.password_hash is not None,` to the `me_page` context dict,
and add:

```python
@router.post("/me/password")
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    user: User = Depends(get_current_user),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    session_signer = request.app.state.session_signer
    settings = request.app.state.settings
    with session_factory() as session:
        db_user = session.get(User, user.id)
        if db_user is None:
            raise HTTPException(status_code=401, detail="user not found")
        # Require a current credential: an OAuth-only account (no hash) can't mint
        # a first password from a session alone.
        if db_user.password_hash is None or not verify_password(db_user.password_hash, current_password):
            raise HTTPException(status_code=400, detail="current password is incorrect")
        try:
            db_user.password_hash = hash_password(new_password)
        except WeakPassword as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        db_user.session_epoch = (db_user.session_epoch or 0) + 1  # revoke other sessions
        new_epoch = db_user.session_epoch
        session.commit()

    response = RedirectResponse(url="/me", status_code=303)  # re-mint THIS browser
    response.set_cookie(
        "session",
        session_signer.make(user_id=user.id, session_epoch=new_epoch),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    return response
```

- [ ] **Step 4: Add the form to `me.html`** (only when the user already has a password):

```html
{% if has_password %}
<section>
  <h3>Change password</h3>
  <form method="post" action="/me/password">
    <label>Current password <input type="password" name="current_password" required autocomplete="current-password"></label>
    <label>New password <input type="password" name="new_password" required autocomplete="new-password"></label>
    <button type="submit">Change password</button>
  </form>
</section>
{% endif %}
```

- [ ] **Step 5: Run to verify it passes** — `uv run pytest tests/test_web_me_password.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/routes/me.py src/call_manager/web/templates/me.html tests/test_web_me_password.py
git commit -m "feat(auth): change own password on /me (re-mints cookie; bumps epoch)"
```

---

## Task 11: NoOp email fallback; whole-suite green

**Files:** Modify `src/call_manager/auth/email.py`, `src/call_manager/main.py`; Test `tests/test_app_email_sender_fallback.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app_email_sender_fallback.py
from call_manager.auth.email import ConsoleEmailSender, NoOpEmailSender
from call_manager.main import create_app


def _env(monkeypatch, pg_url, base_url, secret):
    for k, v in {
        "DATABASE_URL": pg_url, "SESSION_SECRET": secret,
        "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": base_url,
    }.items():
        monkeypatch.setenv(k, v)


def test_prod_without_sender_uses_noop(monkeypatch, pg_url):
    # https + no RESEND/SMTP: previously refused to start; now NoOp (drops alerts,
    # no body logged) instead of console-logging alert contents in prod.
    _env(monkeypatch, pg_url, "https://app.example.org", "x" * 40)
    app = create_app()
    try:
        assert isinstance(app.state.email_sender, NoOpEmailSender)
    finally:
        app.state.engine.dispose()


def test_dev_without_sender_uses_console(monkeypatch, pg_url):
    _env(monkeypatch, pg_url, "http://testserver", "test-secret-at-least-16-chars")
    app = create_app()
    try:
        assert isinstance(app.state.email_sender, ConsoleEmailSender)
    finally:
        app.state.engine.dispose()
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_app_email_sender_fallback.py -v` → FAIL (`NoOpEmailSender` missing; https path raises).

- [ ] **Step 3: Add `NoOpEmailSender`** to `src/call_manager/auth/email.py` (after `ConsoleEmailSender`):

```python
@dataclass
class NoOpEmailSender:
    """Drops messages without delivering OR logging their contents. Production
    fallback when no transport is configured: best-effort ingestion alerts are
    discarded rather than console-logged (alert bodies include error details)."""

    def send(self, *, to: str, subject: str, text_body: str) -> None:
        _logger.info("email dropped (no transport configured)", extra={"event": "email_dropped"})
```

- [ ] **Step 4: Relax the guard in `main.py`** — import `NoOpEmailSender`, and replace
the `elif not settings.cookie_secure: ... else: raise RuntimeError(...)` block (~252-259):

```python
    elif not settings.cookie_secure:
        email_sender = ConsoleEmailSender()  # dev over plain HTTP
    else:
        # Prod with no transport: drop best-effort alerts rather than refuse to
        # start or console-log alert bodies. (Magic-link login — the original
        # reason for the hard requirement — has been removed.)
        email_sender = NoOpEmailSender()
```

Update the comment block above the `if settings.resend_api_key:` selection to drop
the magic-link rationale.

- [ ] **Step 5: Run to verify it passes** — `uv run pytest tests/test_app_email_sender_fallback.py -v` → PASS.

- [ ] **Step 6: Whole-suite green + lint**

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q
```
Expected: lint clean; suite green. Confirm the **ingestion-alert** tests still pass
(the sender is retained) and the retained-sender tests `tests/test_auth_email.py` /
`tests/test_email_smtp.py` pass (delete only any case that exercised magic-link
*login*, if present). The full run may flake on the testcontainers Postgres under
load — re-run any flagged file in isolation to confirm it's infra, not a regression.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(auth): NoOp email fallback in prod (no body logged); whole suite green"
```

---

## Self-Review (completed during planning; updated after Codex round)

**Spec coverage** — every section maps to a task: password module (2); `password_hash` + migrations (3, 8); config `INITIAL_ADMIN_PASSWORD` verbatim (4); bootstrap break-glass + epoch (5); registration request, uniform timing, insert-then-catch, name truncation, dedicated received page (6); password login, dummy-hash all no-cred paths, status branch, magic-link removal + dependent-test cleanup (7); drop table/model + round-trip (8); admin issues password (epoch) + reset (9); change-own-password re-mint + OAuth-only refusal (10); NoOp prod email + whole-suite green (11).

**Codex-round fixes present:**
1. Per-task green: dependent tests removed/rewritten in the *same* task (`test_auth_tokens`/`test_login_flow`/`test_web_auth_magic_link_gate`/`test_web_auth` in 7; `test_models` in 8).
2. Registration timing parity: one dummy argon2 on **every eligible attempt**, insert-then-catch (no pre-SELECT); tests assert identical bodies, dummy invocation on both paths, and the IntegrityError duplicate path.
3. Approval bumps `session_epoch` when a password is issued (tested).
4. Spec + plan agree on `0049`+`0050`; round-trip down/up test; `0050` downgrade recreates `fk_mlt_user_id`/`uq_mlt_token_hash`.
5. Registration truncates name to 200; dedicated `register_received.html` (no false "your sign-in worked").
6. Prod fallback is `NoOpEmailSender` (no body logged); the app-start test disposes `app.state.engine`.
7. Login tests prove identical failure responses, dummy-hash invocation, and repeated-bad-login rate limiting.
8. Config validator detects whitespace-only via `.strip()` but returns the password verbatim.

**Type/name consistency:** `hash_password`/`verify_password`/`validate_password`/
`WeakPassword`/`MIN_PASSWORD_LENGTH` (2) used verbatim in 5/6/7/9/10. `NoOpEmailSender`
(11) imported in main + test. `bootstrap_admins(..., password=...)` matches the call.
Migrations chain `0048 → 0049 → 0050`. Form fields `initial_password` / `new_password` /
`current_password` match their routes + tests.

**Follow-ups to confirm during execution (not blockers):**
- `tests/test_web_auth.py` content — keep non-magic-link cases, remove the rest (Step 6, Task 7).
- Exact insertion points in `admin_users*.html` (match existing row/form markup).
- `tests/test_auth_email.py` / `tests/test_email_smtp.py` — keep (retained senders).
