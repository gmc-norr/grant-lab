# Local Password Auth + Magic-Link Removal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace magic-link login with local email+password (self-register → admin issues credential on approval), keep Google OAuth2 + the approval gate, and add a credential break-glass admin — removing magic-link without breaking the shared email sender used by ingestion alerts.

**Architecture:** A small argon2id `passwords` module backs a password field on `users`. `/register` creates a credential-less `pending` request; the admin issues the initial password on approval. `/login` verifies the password (dummy-hash on every no-credential path to defeat enumeration) and branches on `status`. Magic-link routes/tokens/table/templates are deleted; `auth/email.py` is retained for ingestion alerts. `session_epoch` is bumped on every credential change.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x + Alembic + PostgreSQL 16, Jinja2/HTMX, argon2-cffi, pytest + testcontainers, `uv`/`ruff`.

**Spec:** [docs/superpowers/specs/2026-06-15-local-password-auth-and-magic-link-removal-design.md](../specs/2026-06-15-local-password-auth-and-magic-link-removal-design.md)

**Conventions:**
- `uv run pytest` / `uv run ruff check` / `uv run ruff format`. Each task ends green + a commit.
- Migrations: head is `0048`. This plan adds **`0049`** (add `password_hash`, early) and **`0050`** (drop `magic_link_tokens`, late) — split so each task stays green (dropping the table requires the `MagicLinkToken` model to be gone too, since the test fixture truncates every model table).
- Test apps are built via `create_app()` with `monkeypatch.setenv(...)`, then `app.state` overrides (see `tests/test_web_oauth_google.py`). DB tests use the `pg_session_factory` / `pg_url` fixtures.

---

## Task 1: Add argon2-cffi dependency

**Files:** Modify `pyproject.toml`

- [ ] **Step 1: Add the dependency**

In `pyproject.toml` `dependencies`, next to `authlib`:

```toml
    "argon2-cffi>=23.1",
```

- [ ] **Step 2: Sync + verify import**

Run: `uv sync && uv run python -c "import argon2; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add argon2-cffi for local password hashing"
```

---

## Task 2: Password hashing module

**Files:**
- Create: `src/call_manager/auth/passwords.py`
- Test: `tests/test_auth_passwords.py`

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
    # No stored credential -> always False (even if the candidate equals the
    # dummy seed), but it still does argon2 work (timing equalization).
    assert verify_password(None, "anything") is False
    assert verify_password(None, "x" * 16) is False


def test_verify_tolerates_malformed_hash():
    assert verify_password("not-a-real-argon2-hash", "whatever") is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_auth_passwords.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement**

```python
# src/call_manager/auth/passwords.py
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 12

_ph = PasswordHasher()
# Precomputed dummy hash: verifying a candidate against this on the
# no-credential path costs the same as a real verify, so response time does
# not reveal whether an account/credential exists.
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
    """Verify `password` against `password_hash`.

    When `password_hash` is None (no local credential), verify against a dummy
    hash so timing matches the real path, and always return False.
    """
    target = password_hash if password_hash is not None else _DUMMY_HASH
    try:
        _ph.verify(target, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return password_hash is not None
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_auth_passwords.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/auth/passwords.py
git add src/call_manager/auth/passwords.py tests/test_auth_passwords.py
git commit -m "feat(auth): argon2id password hashing module"
```

---

## Task 3: Add `password_hash` to users (model + migration 0049)

**Files:**
- Modify: `src/call_manager/db/models.py` (User class)
- Create: `alembic/versions/0049_add_password_hash.py`
- Test: `tests/test_user_password_hash_model.py`

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

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_user_password_hash_model.py -v`
Expected: FAIL — `User` has no `password_hash`.

- [ ] **Step 3: Add the column to the model**

In `src/call_manager/db/models.py`, in `class User`, add after the `oidc_sub` column:

```python
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
```

(`Text` is already imported.) Leave `MagicLinkToken` and the `magic_link_tokens`
relationship untouched for now (removed in Task 8).

- [ ] **Step 4: Create migration 0049**

`alembic/versions/0049_add_password_hash.py`:

```python
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

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_user_password_hash_model.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/db/models.py alembic/versions/0049_add_password_hash.py tests/test_user_password_hash_model.py
git commit -m "feat(db): add users.password_hash (migration 0049)"
```

---

## Task 4: Config — `INITIAL_ADMIN_PASSWORD`

**Files:**
- Modify: `src/call_manager/config.py`
- Modify: `.env.example`
- Test: `tests/test_config_initial_admin_password.py`

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


def test_valid_kept(monkeypatch):
    s = _base(monkeypatch, INITIAL_ADMIN_PASSWORD="a-strong-enough-pw")
    assert s.initial_admin_password == "a-strong-enough-pw"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_config_initial_admin_password.py -v`
Expected: FAIL — no `initial_admin_password`.

- [ ] **Step 3: Add the field + validator**

In `src/call_manager/config.py`, add the field near `initial_admin_emails`:

```python
    initial_admin_password: str | None = Field(default=None, alias="INITIAL_ADMIN_PASSWORD")
```

Add a validator next to the other `field_validator`s:

```python
    @field_validator("initial_admin_password", mode="after")
    @classmethod
    def _validate_initial_admin_password(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None  # empty/whitespace -> unset (no trivial admin credential)
        if len(v) < 12:
            raise ValueError(
                "INITIAL_ADMIN_PASSWORD must be at least 12 characters "
                "(or empty/unset)."
            )
        return v
```

- [ ] **Step 4: Update `.env.example`**

Append under the admin/auth section:

```bash
# Break-glass admin credential. While set, every configured INITIAL_ADMIN_EMAIL
# admin's password is (re)set to this value on each startup. Must be >= 12 chars
# (empty = unset). UNSET it once you've logged in and want to manage the password
# in-app, otherwise a restart will overwrite your chosen password.
# INITIAL_ADMIN_PASSWORD=
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_config_initial_admin_password.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/config.py .env.example tests/test_config_initial_admin_password.py
git commit -m "feat(config): INITIAL_ADMIN_PASSWORD (>=12 or unset)"
```

---

## Task 5: Bootstrap admin applies the password

**Files:**
- Modify: `src/call_manager/admin_bootstrap.py`
- Modify: `src/call_manager/main.py` (pass the password into `bootstrap_admins`)
- Test: `tests/test_admin_bootstrap_password.py`

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

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_admin_bootstrap_password.py -v`
Expected: FAIL — `bootstrap_admins` has no `password` kwarg.

- [ ] **Step 3: Update `bootstrap_admins`**

In `src/call_manager/admin_bootstrap.py`, change the signature and loop:

```python
def bootstrap_admins(
    session_factory: sessionmaker,
    emails: Iterable[str],
    allowed_domains: list[str],
    password: str | None = None,
) -> None:
```

Add the import at the top:

```python
from call_manager.auth.passwords import hash_password
```

Replace the `with session_factory() as session:` loop body with:

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
                # bump the epoch so any prior session minted under an old password
                # is revoked. Unset INITIAL_ADMIN_PASSWORD to manage in-app.
                user.password_hash = hash_password(password)
                user.session_epoch = (user.session_epoch or 0) + 1
        session.commit()
```

- [ ] **Step 4: Pass the password from `main.py`**

In `src/call_manager/main.py`, find the `bootstrap_admins(...)` call (in `lifespan`,
~line 67-71) and add the `password=` argument:

```python
    bootstrap_admins(
        app.state.session_factory,
        emails=app.state.settings.initial_admin_emails,
        allowed_domains=app.state.settings.allowed_email_domains,
        password=app.state.settings.initial_admin_password,
    )
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_admin_bootstrap_password.py tests/test_admin_bootstrap_status.py -v`
Expected: all PASS (the existing status test still passes — `password` defaults None).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/admin_bootstrap.py src/call_manager/main.py tests/test_admin_bootstrap_password.py
git commit -m "feat(auth): bootstrap admin applies INITIAL_ADMIN_PASSWORD (break-glass)"
```

---

## Task 6: Registration request route

**Files:**
- Create: `src/call_manager/web/routes/register.py`
- Create: `src/call_manager/web/templates/register.html`
- Modify: `src/call_manager/main.py` (include router)
- Test: `tests/test_web_register.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_register.py
from sqlalchemy import select

from call_manager.db.models import User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def test_register_creates_pending_request_no_password(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        r = c.post("/register", data={"email": "new@ki.se", "name": "New Person"},
                   follow_redirects=False)
        assert r.status_code in (200, 303)
    with pg_session_factory() as s:
        u = s.execute(select(User).where(User.email == "new@ki.se")).scalar_one()
        assert u.status == "pending" and u.password_hash is None and u.name == "New Person"


def test_register_duplicate_email_creates_no_second_row(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    from fastapi.testclient import TestClient
    with pg_session_factory() as s:
        s.add(User(email="dupe@ki.se", status="active")); s.commit()
    with TestClient(app) as c:
        r = c.post("/register", data={"email": "dupe@ki.se"}, follow_redirects=False)
        assert r.status_code in (200, 303)
    with pg_session_factory() as s:
        assert len(s.execute(select(User).where(User.email == "dupe@ki.se")).scalars().all()) == 1


def test_register_domain_disallowed(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        c.post("/register", data={"email": "x@evil.com"}, follow_redirects=False)
    with pg_session_factory() as s:
        assert s.execute(select(User).where(User.email == "x@evil.com")).scalar_one_or_none() is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_web_register.py -v`
Expected: FAIL — `/register` 404.

- [ ] **Step 3: Implement the router**

```python
# src/call_manager/web/routes/register.py
from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.auth.domain import domain_allowed
from call_manager.auth.passwords import _DUMMY_HASH  # noqa: F401  (timing parity import)
from call_manager.db.models import User
from call_manager.web.routes.auth import MAX_EMAIL_LEN, _client_ip, _email_fingerprint

_logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    settings = request.app.state.settings
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="register.html",
        context={"allowed_domains": settings.allowed_email_domains},
    )


@router.post("/register", response_class=HTMLResponse)
def register_submit(
    request: Request,
    email: str = Form(...),
    name: str = Form(default=""),
):
    settings = request.app.state.settings
    ip_limiter = request.app.state.ip_limiter
    email_limiter = request.app.state.email_limiter
    session_factory = request.app.state.session_factory

    # Identical response on every branch (no enumeration of who already exists).
    def _received() -> HTMLResponse:
        return request.app.state.templates.TemplateResponse(
            request=request, name="account_pending.html", context={}
        )

    if len(email) > MAX_EMAIL_LEN:
        return _received()
    email_normalized = email.strip().lower()
    client_ip = _client_ip(request)
    if not ip_limiter.allow(client_ip):
        return _received()
    if not domain_allowed(email_normalized, settings.allowed_email_domains):
        return _received()
    if not email_limiter.allow(email_normalized):
        return _received()

    with session_factory() as session:
        # Race-safe: insert and catch a uniqueness conflict, mirroring the OIDC
        # resolver. On any duplicate, do the same argon2 dummy work a fresh
        # insert's later password step would NOT do — equalize timing so the
        # duplicate path doesn't leak existence.
        existing = session.execute(
            select(User).where(User.email == email_normalized)
        ).scalar_one_or_none()
        if existing is not None:
            from call_manager.auth.passwords import verify_password

            verify_password(None, "timing-parity")
            _logger.info(
                "register: email already exists",
                extra={"event": "register_duplicate", "email_fp": _email_fingerprint(email_normalized)},
            )
            return _received()
        user = User(email=email_normalized, name=name.strip() or None, status="pending")
        session.add(user)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            from call_manager.auth.passwords import verify_password

            verify_password(None, "timing-parity")
            return _received()
        session.commit()

    _logger.info(
        "register: request created",
        extra={"event": "register_created", "email_fp": _email_fingerprint(email_normalized)},
    )
    return _received()
```

(The `_DUMMY_HASH` import line is unnecessary if it trips ruff; the real timing
work is `verify_password(None, ...)`. Drop the F401 import and keep only the
`verify_password` calls.)

- [ ] **Step 4: Create `register.html`**

```html
{% extends "base.html" %}
{% block title %}Request access — Call Manager{% endblock %}
{% block content %}
<div class="narrow">
  <div class="auth-card">
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
        <input type="text" name="name" autocomplete="name">
      </label>
      {% if allowed_domains %}
      <small>Use your hospital email ({{ allowed_domains | join(' or ') }}).</small>
      {% endif %}
      <button type="submit">Request access</button>
    </form>
    <p style="text-align:center"><a href="/login">Back to sign in</a></p>
  </div>
</div>
{% endblock %}
```

- [ ] **Step 5: Wire the router in `main.py`**

```python
from call_manager.web.routes import register
...
    app.include_router(register.router)
```

- [ ] **Step 6: Run to verify it passes**

Run: `uv run pytest tests/test_web_register.py -v`
Expected: all PASS.

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check src/call_manager/web/routes/register.py
git add src/call_manager/web/routes/register.py src/call_manager/web/templates/register.html src/call_manager/main.py tests/test_web_register.py
git commit -m "feat(auth): /register access-request route (no password; race-safe; enumeration-resistant)"
```

---

## Task 7: Rewrite login as password auth; delete magic-link routes/tokens/templates

**Files:**
- Modify: `src/call_manager/web/routes/auth.py`
- Modify: `src/call_manager/web/templates/login.html`
- Delete: `src/call_manager/auth/tokens.py`, `login_sent.html`, `login_confirm.html`, `login_link_expired.html`
- Test: `tests/test_web_login_password.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_login_password.py
from fastapi.testclient import TestClient

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


def _user(sf, *, email, status, password=None, is_admin=False):
    with sf() as s:
        u = User(email=email, status=status, is_admin=is_admin,
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


def test_wrong_password_generic_error(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _user(app.state.session_factory, email="a@ki.se", status="active", password="correct-horse-1")
    with TestClient(app) as c:
        r = c.post("/login", data={"email": "a@ki.se", "password": "nope"}, follow_redirects=False)
        assert r.status_code == 200
        assert "invalid email or password" in r.text.lower()
        assert "set-cookie" not in {k.lower() for k in r.headers}


def test_unknown_email_generic_error(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.post("/login", data={"email": "ghost@ki.se", "password": "whatever-long"})
        assert "invalid email or password" in r.text.lower()


def test_oauth_only_account_cannot_password_login(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _user(app.state.session_factory, email="g@ki.se", status="active", password=None)  # no hash
    with TestClient(app) as c:
        r = c.post("/login", data={"email": "g@ki.se", "password": "anything-long"})
        assert "invalid email or password" in r.text.lower()


def test_pending_and_disabled_branch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _user(app.state.session_factory, email="p@ki.se", status="pending", password="correct-horse-1")
    _user(app.state.session_factory, email="d@ki.se", status="disabled", password="correct-horse-1")
    with TestClient(app) as c:
        rp = c.post("/login", data={"email": "p@ki.se", "password": "correct-horse-1"},
                    follow_redirects=False)
        assert rp.headers["location"] == "/auth/pending"
        rd = c.post("/login", data={"email": "d@ki.se", "password": "correct-horse-1"},
                    follow_redirects=False)
        assert rd.headers["location"] == "/auth/no-access"


def test_magic_link_routes_gone(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        assert c.get("/login/verify?token=x", follow_redirects=False).status_code == 404
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_web_login_password.py -v`
Expected: FAIL — login still expects magic-link; `/login/verify` still exists.

- [ ] **Step 3: Rewrite `auth.py`**

Replace the imports block (lines 1-17) — drop magic-link imports, add password +
session-cookie make:

```python
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Cookie, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from call_manager.auth.passwords import verify_password
from call_manager.auth.redirects import safe_next
from call_manager.auth.session_cookie import InvalidSession
from call_manager.db.models import User
from sqlalchemy import select
```

Keep `_email_fingerprint`, `_client_ip`, `MAX_EMAIL_LEN` unchanged.

Replace `login_form` to pass an optional `error`:

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

Replace the entire `login_submit` (and DELETE `login_verify_confirm` +
`login_verify_consume`) with:

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
        # One generic message for unknown email / wrong password / no-credential
        # account; status 200 so the global 401->/login handler can't loop.
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
    client_ip = _client_ip(request)
    email_fp = _email_fingerprint(email_normalized)

    if not ip_limiter.allow(client_ip) or not email_limiter.allow(email_normalized):
        _logger.info("login rate-limited", extra={"event": "login_rate_limited", "email_fp": email_fp})
        return _denied()

    with session_factory() as session:
        user = session.execute(
            select(User).where(User.email == email_normalized)
        ).scalar_one_or_none()
        # verify_password runs argon2 even when user is None / has no hash
        # (dummy-hash path) so timing doesn't reveal account existence.
        stored = user.password_hash if user is not None else None
        if not verify_password(stored, password):
            _logger.info("login failed", extra={"event": "login_failed", "email_fp": email_fp})
            return _denied()
        # Valid credential. Branch on status.
        status = user.status
        if status != "active":
            target = "/auth/pending" if status == "pending" else "/auth/no-access"
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

`logout_submit` stays unchanged.

- [ ] **Step 4: Rewrite `login.html`**

Replace the `<hgroup>` … first `<form>` … Google block (lines 16-37) with:

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

- [ ] **Step 5: Delete magic-link files**

```bash
git rm src/call_manager/auth/tokens.py \
       src/call_manager/web/templates/login_sent.html \
       src/call_manager/web/templates/login_confirm.html \
       src/call_manager/web/templates/login_link_expired.html
```

- [ ] **Step 6: Run to verify it passes + no stale imports**

Run: `uv run pytest tests/test_web_login_password.py -v`
Expected: all PASS.
Run: `uv run ruff check src/call_manager/web/routes/auth.py src/call_manager/web/routes/register.py`
Expected: clean (register.py imports `_client_ip`/`MAX_EMAIL_LEN`/`_email_fingerprint` from auth — still present).

- [ ] **Step 7: Commit**

```bash
git add -A src/call_manager/web/routes/auth.py src/call_manager/web/templates/login.html tests/test_web_login_password.py
git commit -m "feat(auth): password login; remove magic-link routes/tokens/templates"
```

---

## Task 8: Remove `MagicLinkToken` model + drop table (migration 0050)

**Files:**
- Modify: `src/call_manager/db/models.py` (remove `MagicLinkToken` + relationship)
- Create: `alembic/versions/0050_drop_magic_link_tokens.py`
- Test: `tests/test_migration_0050_drops_magic_link.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migration_0050_drops_magic_link.py
from sqlalchemy import inspect

from call_manager.db.models import Base


def test_magic_link_tokens_table_is_gone(migrated_engine):
    insp = inspect(migrated_engine)
    assert "magic_link_tokens" not in insp.get_table_names()


def test_model_no_longer_defines_it():
    assert "magic_link_tokens" not in Base.metadata.tables
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_migration_0050_drops_magic_link.py -v`
Expected: FAIL — table + model still present.

- [ ] **Step 3: Remove the model + relationship**

In `src/call_manager/db/models.py`:
- Delete the `magic_link_tokens: Mapped[list[MagicLinkToken]] = relationship(...)`
  line from `class User`.
- Delete the entire `class MagicLinkToken(Base): ...` block.

- [ ] **Step 4: Create migration 0050**

`alembic/versions/0050_drop_magic_link_tokens.py`:

```python
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
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_migration_0050_drops_magic_link.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/db/models.py alembic/versions/0050_drop_magic_link_tokens.py tests/test_migration_0050_drops_magic_link.py
git commit -m "feat(db): drop magic_link_tokens table + model (migration 0050)"
```

---

## Task 9: Admin approve issues a password; admin reset-password

**Files:**
- Modify: `src/call_manager/web/routes/admin.py`
- Modify: `src/call_manager/web/templates/admin_users_pending.html`, `admin_users.html`
- Test: `tests/test_web_admin_passwords.py`

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


def _admin_client(app, pg_session_factory):
    with pg_session_factory() as s:
        a = User(email="admin@ki.se", status="active", is_admin=True)
        s.add(a); s.commit(); s.refresh(a); aid = a.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=aid))
    return c


def test_approve_local_requires_initial_password(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url); c = _admin_client(app, pg_session_factory)
    with pg_session_factory() as s:
        p = User(email="local@ki.se", status="pending")  # no oidc, no password
        s.add(p); s.commit(); s.refresh(p); pid = p.id
    # No password -> rejected
    assert c.post(f"/admin/users/{pid}/approve", data={"role": "member"},
                  follow_redirects=False).status_code == 400
    # With password -> active + hash set
    c.post(f"/admin/users/{pid}/approve",
           data={"role": "member", "initial_password": "issued-strong-pw"}, follow_redirects=False)
    with pg_session_factory() as s:
        u = s.get(User, pid)
        assert u.status == "active" and verify_password(u.password_hash, "issued-strong-pw")


def test_approve_oauth_pending_without_password(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url); c = _admin_client(app, pg_session_factory)
    with pg_session_factory() as s:
        p = User(email="goog@ki.se", status="pending", oidc_iss="i", oidc_sub="s")
        s.add(p); s.commit(); s.refresh(p); pid = p.id
    assert c.post(f"/admin/users/{pid}/approve", data={"role": "member"},
                  follow_redirects=False).status_code == 303
    with pg_session_factory() as s:
        assert s.get(User, pid).status == "active"


def test_admin_reset_password_bumps_epoch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url); c = _admin_client(app, pg_session_factory)
    with pg_session_factory() as s:
        u = User(email="u@ki.se", status="active"); s.add(u); s.commit(); s.refresh(u)
        uid, ep0 = u.id, u.session_epoch
    c.post(f"/admin/users/{uid}/reset-password", data={"new_password": "fresh-strong-pw"},
           follow_redirects=False)
    with pg_session_factory() as s:
        u = s.get(User, uid)
        assert verify_password(u.password_hash, "fresh-strong-pw") and u.session_epoch == ep0 + 1


def test_reset_rejects_short(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url); c = _admin_client(app, pg_session_factory)
    with pg_session_factory() as s:
        u = User(email="u2@ki.se", status="active"); s.add(u); s.commit(); s.refresh(u); uid = u.id
    assert c.post(f"/admin/users/{uid}/reset-password", data={"new_password": "short"},
                  follow_redirects=False).status_code == 400
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_web_admin_passwords.py -v`
Expected: FAIL — approve ignores password; no reset route.

- [ ] **Step 3: Update `admin.py`**

Add the import near the other admin imports:

```python
from call_manager.auth.passwords import WeakPassword, hash_password
```

Replace the `approve_user` body with (requires a password for non-oidc accounts):

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
        # A local (non-OAuth) account needs an admin-issued password to log in.
        if u.oidc_sub is None:
            if not initial_password:
                raise HTTPException(status_code=400, detail="initial password required")
            try:
                u.password_hash = hash_password(initial_password)
            except WeakPassword as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
        elif initial_password:
            # Optional even for OAuth accounts.
            try:
                u.password_hash = hash_password(initial_password)
            except WeakPassword as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
        u.status = "active"
        u.is_admin = role == "admin"
        u.approved_by_user_id = admin.id
        u.approved_at = datetime.now(UTC)
        db.commit()
    return RedirectResponse(url="/admin/users/pending", status_code=303)
```

Add a reset-password route (place next to the other `/admin/users/{id}/...` routes):

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

- [ ] **Step 4: Update admin templates**

In `admin_users_pending.html`, add an `initial_password` input to the approve
forms (member + admin):

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

In `admin_users.html`, add a reset-password control per row:

```html
            <form method="post" action="/admin/users/{{ u.id }}/reset-password" style="display:inline">
              <input type="password" name="new_password" placeholder="new password" required>
              <button type="submit" class="secondary">Reset password</button></form>
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_web_admin_passwords.py tests/test_web_admin_users.py -v`
Expected: all PASS (existing admin tests unaffected: an OAuth-pending approve has
`oidc_sub` set, so no password required; the existing local approve tests should
be updated to pass `initial_password` — update `test_web_admin_users.py` approve
cases that create a pending user **without** `oidc_sub`).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/routes/admin.py src/call_manager/web/templates/admin_users_pending.html src/call_manager/web/templates/admin_users.html tests/test_web_admin_passwords.py tests/test_web_admin_users.py
git commit -m "feat(admin): issue password on approval (local); admin reset-password (bumps epoch)"
```

---

## Task 10: Change own password (`/me/password`)

**Files:**
- Modify: `src/call_manager/web/routes/me.py`
- Modify: `src/call_manager/web/templates/me.html`
- Test: `tests/test_web_me_password.py`

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


def _client(app, sf, **kw):
    with sf() as s:
        u = User(email="u@ki.se", status="active",
                 password_hash=hash_password(kw["password"]) if kw.get("password") else None)
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
    # Fresh cookie minted so the user isn't logged out by their own change.
    assert "session=" in "".join(r.headers.get_list("set-cookie"))
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

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_web_me_password.py -v`
Expected: FAIL — `/me/password` 404/405.

- [ ] **Step 3: Implement the route**

In `src/call_manager/web/routes/me.py`, add imports:

```python
from call_manager.auth.passwords import WeakPassword, hash_password, verify_password
```

Add the route:

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
        # No self-set-first-password: a current credential is required, so an
        # OAuth-only account (no hash) cannot mint one from a session alone.
        if db_user.password_hash is None or not verify_password(
            db_user.password_hash, current_password
        ):
            raise HTTPException(status_code=400, detail="current password is incorrect")
        try:
            db_user.password_hash = hash_password(new_password)
        except WeakPassword as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        db_user.session_epoch = (db_user.session_epoch or 0) + 1  # revoke other sessions
        new_epoch = db_user.session_epoch
        session.commit()

    # Re-mint THIS browser's cookie at the new epoch so the user stays logged in.
    response = RedirectResponse(url="/me", status_code=303)
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

- [ ] **Step 4: Add the form to `me.html`**

Add a change-password section (shown only when the user already has a password —
OAuth-only users get one via admin reset). Pass `has_password` from `me_page`
context: in `me.py` `me_page`, add `"has_password": user.password_hash is not None,`
to the context dict. Then in `me.html`:

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

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_web_me_password.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/routes/me.py src/call_manager/web/templates/me.html tests/test_web_me_password.py
git commit -m "feat(auth): change own password on /me (re-mints cookie; bumps epoch)"
```

---

## Task 11: Relax email-sender guard; whole-suite green

**Files:**
- Modify: `src/call_manager/main.py` (email-sender selection ~252-259)
- Test: `tests/test_app_starts_without_email_sender.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app_starts_without_email_sender.py
from call_manager.auth.email import ConsoleEmailSender
from call_manager.main import create_app


def test_https_without_sender_starts_with_console(monkeypatch, pg_url):
    # Previously refused to start (magic links would be console-logged). With
    # magic-link gone, alerts are best-effort -> ConsoleEmailSender, no crash.
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "x" * 40,            # >=32 required for https
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "https://app.example.org",
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    assert isinstance(app.state.email_sender, ConsoleEmailSender)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_app_starts_without_email_sender.py -v`
Expected: FAIL — `create_app()` raises `RuntimeError` on https without a sender.

- [ ] **Step 3: Relax the guard**

In `src/call_manager/main.py`, replace the `elif not settings.cookie_secure: ... else: raise RuntimeError(...)` block (~252-259) with:

```python
    else:
        # No RESEND/SMTP configured. The email sender now drives only best-effort
        # ingestion alerts (magic-link login was removed), so fall back to the
        # console sender instead of refusing to start. Alerts simply log instead
        # of being delivered.
        email_sender = ConsoleEmailSender()
```

(Update the explanatory comment block above it to drop the magic-link rationale.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_app_starts_without_email_sender.py -v`
Expected: PASS.

- [ ] **Step 5: Remove dead magic-link tests + whole-suite green**

```bash
git rm tests/test_auth_tokens.py
```
Then update/remove magic-link assertions in `tests/test_web_auth.py`,
`tests/test_auth_redirects.py`, `tests/test_auth_email.py` (keep sender tests that
still apply — the sender class is retained), and `tests/test_web_auth_magic_link_gate.py`
(delete — the gate is gone). Run:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q
```
Expected: lint clean; suite green. The full run may flake on the testcontainers
Postgres under load — re-run any flagged file in isolation to confirm it's infra,
not a regression (known issue: whole-suite `pytest` drops the container). Confirm
ingestion-alert tests still pass (the retained sender).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(auth): relax email-sender guard (alerts best-effort); drop magic-link tests"
```

---

## Self-Review (completed during planning)

**Spec coverage** — every spec section maps to a task:
- Two methods (password + OAuth2), email identifier → Tasks 2/3/7 (OAuth2 retained, untouched)
- Registration = request, no password, race-safe, enumeration-resistant → Task 6
- Admin issues credential on approval → Task 9
- Login (argon2, dummy-hash all no-cred paths, status branch) → Task 7 (+ Task 2 dummy-verify)
- Bootstrap admin (`INITIAL_ADMIN_PASSWORD`, ≥12/empty, re-apply + epoch bump) → Tasks 4/5
- session_epoch on every credential change; own-change re-mints cookie → Tasks 5/9/10
- OAuth-only cannot self-set first password → Task 10
- Magic-link removal (routes/tokens/templates/table) + retain `auth/email.py` → Tasks 7/8/11
- `password_hash` column + migrations → Tasks 3/8
- Config `INITIAL_ADMIN_PASSWORD`; relax prod email guard → Tasks 4/11
- Login page + register page → Tasks 6/7

**Type/name consistency:** `hash_password`/`verify_password`/`validate_password`/
`WeakPassword`/`MIN_PASSWORD_LENGTH` (Task 2) used verbatim in Tasks 5/6/7/9/10.
`bootstrap_admins(..., password=...)` (Task 5) matches the `main.py` call. Migration
revisions chain `0048 → 0049 → 0050`. `initial_password` (approve) and
`new_password` (reset/change) form fields match their tests.

**Follow-ups to confirm during execution (not blockers):**
- Exact insertion points in `admin_users.html` / `admin_users_pending.html` (match
  the existing row/form markup).
- Confirm `tests/test_web_admin_users.py` local-approve cases create the pending
  user **without** `oidc_sub` and now pass `initial_password`.
- `base.html` block names already verified (`content`/`title`).
