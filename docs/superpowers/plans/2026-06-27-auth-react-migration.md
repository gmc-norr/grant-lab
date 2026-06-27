# Auth + final Jinja teardown — React migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the 8 server-rendered Jinja auth pages to React under a new pre-auth layout, replace login/register/logout form-POSTs with typed `POST /api/auth/*` JSON endpoints, relocate the sign-out control into the SPA shell, and delete the entire web Jinja stack — completing the full-React migration.

**Architecture:** Standard strangler-fig slice, adapted for pre-auth pages: additive typed `/api/auth` endpoints (old Jinja still live) → React routes under a NEW pre-auth layout (sibling to `AppShell`, no IDE chrome) → flip the route manifest → delete the web Jinja stack (templates + Pico/htmx/app static + the web Jinja env + the two now-dead browser-redirect exception branches). Done as ONE atomic slice; the assets orphan the moment the React pages go live; a local-only `--no-ff` merge is revertible.

**Tech Stack:** FastAPI + Pydantic + SQLAlchemy (backend `/api`); React 19 + react-router + TanStack Query + openapi-fetch (frontend); pytest + testcontainers Postgres (backend tests); vitest + RTL (frontend tests).

**Spec:** `docs/superpowers/specs/2026-06-27-auth-react-migration-design.md`.

## Global Constraints

These bind every task; exact values copied from the spec and the codebase.

- **No DB schema change. No new dependency. Strict parity port.** The only intended behavior changes: (1) form-POST/redirect/banner UX → JSON `{redirect}` + inline errors; (2) login-failure HTTP status `200` → `400` (generic message + cross-mode indistinguishability preserved). Everything else preserved verbatim: the generic `"Invalid email or password."` message, the argon2 dummy-hash timing path, IP+email rate-limit keying AND its non-disclosure (rate-limit folded into the same `400`), status routing incl. the `/auth/no-access` catch-all, cookie attributes (`httponly`/`secure`/`samesite=lax`/`max_age`), `session_epoch` revocation, `safe_next` sanitization, register anti-enumeration, and `oauth_txn` left untouched on logout.
- **Login failure returns `400`, NEVER `401` or `429`.** `unwrap` (client.ts) redirects to `/login` on ANY `401`, so a `401` from `/api/auth/login` would loop. `400` throws a non-redirecting `ApiError`. Rate-limited folds into the same generic `400` (non-disclosure — the endpoint must never reveal credentials-vs-limiter). A test pins that wrong-password, unknown-email, and no-credential (oauth-only) failures return byte-identical status + body.
- **Errors via `raise HTTPException(code, detail)`** → the global `/api` envelope (`{error:{code,message}}`). Never hand-build error JSON. STATUS_CODES maps 400→`bad_request`.
- **`OriginCSRFMiddleware` allows an ABSENT Origin** — TestClient POSTs pass with no Origin header; do NOT add one. Same-origin browser fetch sends Origin → passes.
- **CSS Modules + `--app-*` tokens; NO bare element selectors** in `.module.css` (scope every rule under a hashed class). Pre-auth pages match the dark-teal theme: a centered card on `--app-editor`. Tokens available: `--app-editor/-sidebar/-titlebar/-accent/-accent-bright/-link/-text/-muted/-muted-chrome/-dim/-border/-border-strong/-hover/-selected/-good/-warn/-bad/-heading`.
- **`tsconfig` `noUnusedLocals` + `include:["src"]`** — no unused imports/locals under `src/` (tsc checks test files too). Frontend has NO eslint; `tsc --noEmit` is the gate.
- **Manifest is prefix-not-`:param`.** The new React routes + the `routes.json` manifest change + the regenerated artifacts land in ONE commit (the `router.test.tsx` set-equality guard enforces it).
- **The OAuth flow stays server-side redirects;** only the *pages* become React. `POST /auth/google/start`, `GET /auth/google/callback`, the `oauth_txn` PKCE cookie, and `/auth/google/*` routing are UNTOUCHED. The React login's "Continue with Google" is a NATIVE `<form method="post" action="/auth/google/start">`, never a fetch.
- **CI runs ONLY `ruff check .`** (whole repo incl. `tests/`), NOT `ruff format --check`. The repo carries pre-existing `ruff format` drift on master (out of scope). Run `ruff format` ONLY on this slice's own new/edited files.
- **Two OpenAPI regens** (`EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh` — the `bge-m3` override avoids the mxbai stdout WARNING that corrupts codegen): once after `/api/auth` exists (Task 6), once AFTER teardown deletes routes (Task 11). One routes regen at the manifest flip (`uv run python scripts/gen_routes.py`, Task 9), re-run at teardown if route deletion changes nothing in `routes.json` it's a no-op but `--check` must stay green.
- **Whole-suite testcontainers Postgres flakes under load** — run targeted files for verification. A test file run FIRST against a fresh container can hit a `UndefinedTable: relation "references" does not exist` lifespan-startup flake; re-run grouped with a neighbor before treating it as real. Capture exit codes, don't pipe through `tail`.

### Design notes (research-driven refinements to the spec — flagged for review)

1. **Manifest uses three explicit `/auth/*` leaves, NOT bare `/auth`.** `dev-proxy.ts` decides `shouldServeViaVite = isViteAsset(url) || isSpaRoute(url)` with no backend-precedence check. A bare `/auth` in `spa_routes` would make `isSpaRoute("/auth/google/start") === true` → in `SPA_MODE=dev` Vite would shadow the real OAuth endpoints. Listing `/auth/pending`, `/auth/no-access`, `/auth/error` exactly fixes dev and adds prod defense-in-depth. `/auth` stays in `backend_prefixes` (doc-only — OAuth lives there).
2. **New public `GET /api/auth/config`** → `{google_oidc_enabled, allowed_domains}`. The React login needs the flag to show/hide "Continue with Google" (parity with the deleted Jinja `{% if google_oidc_enabled %}`) and the domain hint. The PreAuthLayout invariant forbids an *authenticated* call; this endpoint is unauthenticated (always `200`), so it does not trip the no-`/api/me`-on-mount invariant.
3. **Request helpers relocate to a new `web/client_ip.py`.** `oauth_google.py` (which survives) imports `_client_ip` from the doomed `web/routes/auth.py`; the helper moves to a neutral module imported by `oauth_google.py` AND the new `web/api/auth.py`.
4. **The dead-redirect cleanup is FOLDED IN (user decision).** Collapse BOTH `_redirect_html_401`'s and `_inactive_account`'s non-`/api` browser-redirect branches (both unreachable: no protected non-`/api` HTML route exists after the migration). Migrate/delete the 4 already-red tests in `test_auth_inactive_enforcement.py` (the account/settings slice deleted the server-side `GET /me` page route they assert on, breaking them on master — verified: 4 failed). Keep the dependency unit test + `/api` 403 coverage.

---

## File Structure

**Backend (new):**
- `src/call_manager/web/client_ip.py` — `client_ip(request)`, `email_fingerprint(email)`, `MAX_EMAIL_LEN`. Relocated verbatim from `web/routes/auth.py`.
- `src/call_manager/web/api/auth.py` — `router` with `GET /auth/config`, `POST /auth/login`, `POST /auth/register`, `POST /auth/logout` (prefixed `/api` by the api router).

**Backend (modified):**
- `src/call_manager/web/api/__init__.py` — import + include `auth.router`.
- `src/call_manager/web/routes/oauth_google.py` — repoint `_client_ip` import (Task 1); delete the 3 holding-page GETs (Task 11).
- `src/call_manager/main.py` — Task 11: remove Jinja env + filter registrations + `auth`/`register` includes + `TEMPLATES_DIR` + orphaned imports; collapse both exception-handler non-`/api` branches.

**Backend (deleted, Task 11):** `web/routes/auth.py`, `web/routes/register.py`; 8 templates (`login/register/register_received/account_pending/account_no_access/auth_error/error/base.html`); 6 static files (`pico.min.css`, `app.css`, `app.js`, `htmx.min.js`, `htmx-sse.js`, `dedup.js` — keep `favicon.svg`).

**Frontend (new):**
- `src/api/auth.ts` — `useAuthConfig`, `useLogin`, `useRegister`, `useLogout`.
- `src/routes/PreAuthLayout.tsx` + `.module.css` — pre-auth layout (centered card, no shell).
- `src/routes/Login.tsx`, `src/routes/Register.tsx`, `src/routes/AccountPending.tsx`, `src/routes/AccountNoAccess.tsx`, `src/routes/AuthError.tsx`.

**Frontend (modified):**
- `src/router.tsx` — add `PreAuthLayout` sibling + 5 routes (Task 9).
- `frontend/routes.json` — manifest (Task 9).
- `src/shell/TitleBar.tsx` + `.module.css` — sign-out + email chip (Task 10).
- `src/routes/DashboardHeader.tsx` + `.module.css` — remove sign-out form (Task 10).
- `src/api/client.ts`, `src/api/session.ts` — stale-comment cleanup (Task 11).

**Tests + CI:** new `tests/test_api_auth.py` (Tasks 2–5) + `tests/test_auth_teardown_guard.py`; new frontend `*.test.tsx` per component; updated `tests/test_web_auth.py`, `tests/test_auth_inactive_enforcement.py`, `tests/test_security_headers.py`, `tests/test_spa_serving.py`, `tests/test_gen_routes.py`, `src/dev-proxy.test.ts`, `src/router.test.tsx`, `src/shell/TitleBar.test.tsx`, `src/routes/DashboardHeader.test.tsx`; deleted `tests/test_web_login_password.py`, `tests/test_web_register.py`, `tests/test_web_login_google_button.py`, `tests/test_web_me.py`, `tests/test_web_me_subscriptions.py`, and the whole `tests/js/` directory; `.github/workflows/ci.yml` loses its `node --test 'tests/js/**'` step (keeps "Set up Node" for the pnpm steps).

---

## Task 1: Relocate request helpers to `web/client_ip.py`

**Files:**
- Create: `src/call_manager/web/client_ip.py`
- Modify: `src/call_manager/web/routes/auth.py` (cut helper defs, import from new module), `src/call_manager/web/routes/register.py:12` (repoint import), `src/call_manager/web/routes/oauth_google.py:16` (repoint import)
- Test: `tests/test_client_ip.py` (create)

**Interfaces:**
- Produces: `client_ip(request: Request) -> str` (raises `HTTPException(400)` on malformed XFF / absent client), `email_fingerprint(email: str) -> str`, `MAX_EMAIL_LEN: int = 320`.

- [ ] **Step 1: Write the failing test** — `tests/test_client_ip.py`

```python
import pytest
from fastapi import FastAPI, HTTPException, Request

from call_manager.web.client_ip import MAX_EMAIL_LEN, client_ip, email_fingerprint


def _req(*, headers=None, client=("203.0.113.5", 1234), trusted_proxy=False, hops=1):
    app = FastAPI()
    app.state.settings = type(
        "S", (), {"trusted_proxy": trusted_proxy, "trusted_proxy_hops": hops}
    )()
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "headers": raw, "app": app, "client": client}
    return Request(scope)


def test_max_email_len_is_320():
    assert MAX_EMAIL_LEN == 320


def test_email_fingerprint_is_stable_12_hex():
    fp = email_fingerprint("a@ki.se")
    assert len(fp) == 12 and fp == email_fingerprint("a@ki.se")


def test_direct_client_host():
    assert client_ip(_req()) == "203.0.113.5"


def test_absent_client_400():
    with pytest.raises(HTTPException) as e:
        client_ip(_req(client=None))
    assert e.value.status_code == 400


def test_trusted_proxy_takes_nth_from_right():
    r = _req(headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9"}, trusted_proxy=True, hops=1)
    assert client_ip(r) == "9.9.9.9"


def test_trusted_proxy_too_few_hops_400():
    r = _req(headers={"X-Forwarded-For": "1.1.1.1"}, trusted_proxy=True, hops=2)
    with pytest.raises(HTTPException) as e:
        client_ip(r)
    assert e.value.status_code == 400
```

- [ ] **Step 2: Run it — fails** `uv run pytest tests/test_client_ip.py -q` → ImportError (`web.client_ip` missing).

- [ ] **Step 3: Create `src/call_manager/web/client_ip.py`** — move the three helpers verbatim from `web/routes/auth.py` (lines 20–71), renamed public:

```python
from __future__ import annotations

import hashlib

from fastapi import HTTPException, Request

# Hard cap matches users.email VARCHAR(320). Reject oversized inputs *before*
# they reach the rate limiter so an attacker can't grow the limiter dict with
# junk keys (memory-amplification DoS).
MAX_EMAIL_LEN = 320


def email_fingerprint(email: str) -> str:
    """Short hex of sha256(email). Safe to log — doesn't expose the address but
    lets an operator correlate events across log lines."""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:12]


def client_ip(request: Request) -> str:
    """Resolve the caller's IP. With settings.trusted_proxy, read the Nth-from-
    the-right X-Forwarded-For entry (trusted_proxy_hops); reject (400) an XFF with
    fewer entries than the hop count. Otherwise use request.client and fail loudly
    (400) if absent — silently bucketing unknown clients breaks the rate limiter."""
    settings = request.app.state.settings
    if getattr(settings, "trusted_proxy", False):
        xff = request.headers.get("x-forwarded-for")
        if xff:
            hops = getattr(settings, "trusted_proxy_hops", 1)
            entries = [e.strip() for e in xff.split(",") if e.strip()]
            if len(entries) >= hops:
                return entries[-hops]
            raise HTTPException(status_code=400, detail="malformed X-Forwarded-For")
    if request.client is None:
        raise HTTPException(status_code=400, detail="cannot determine client")
    return request.client.host
```

(Preserve the full docstrings from the original; condensed here for the plan.)

- [ ] **Step 4: Repoint the three importers.** In `web/routes/auth.py`: delete the `import hashlib` (now unused there) and the `MAX_EMAIL_LEN`/`_email_fingerprint`/`_client_ip` defs (lines 20–71), and replace the local uses (`MAX_EMAIL_LEN`, `_email_fingerprint(...)`, `_client_ip(...)`) by importing from the new module:

```python
from call_manager.web.client_ip import MAX_EMAIL_LEN, client_ip, email_fingerprint
```

Then update auth.py's call sites: `_email_fingerprint(` → `email_fingerprint(`, `_client_ip(` → `client_ip(`. In `web/routes/register.py:12` replace `from call_manager.web.routes.auth import MAX_EMAIL_LEN, _client_ip, _email_fingerprint` with `from call_manager.web.client_ip import MAX_EMAIL_LEN, client_ip, email_fingerprint` and update its call sites (`_client_ip(` → `client_ip(`, `_email_fingerprint(` → `email_fingerprint(`). In `web/routes/oauth_google.py:16` replace `from call_manager.web.routes.auth import _client_ip` with `from call_manager.web.client_ip import client_ip` and update line 50 `_client_ip(request)` → `client_ip(request)`.

- [ ] **Step 5: Run** `uv run pytest tests/test_client_ip.py tests/test_web_oauth_google.py tests/test_web_login_password.py tests/test_web_register.py -q` → all PASS (existing auth/oauth/register behavior unchanged; helpers relocated).

- [ ] **Step 6: ruff + commit**

```bash
uv run ruff format src/call_manager/web/client_ip.py tests/test_client_ip.py
uv run ruff check src/call_manager/web/client_ip.py src/call_manager/web/routes/auth.py src/call_manager/web/routes/register.py src/call_manager/web/routes/oauth_google.py tests/test_client_ip.py
git add -A && git commit -m "refactor(auth): relocate client_ip/email_fingerprint to web/client_ip (auth slice)"
```

---

## Task 2: `GET /api/auth/config` (public)

**Files:**
- Create: `src/call_manager/web/api/auth.py`
- Modify: `src/call_manager/web/api/__init__.py`
- Test: `tests/test_api_auth.py` (create)

**Interfaces:**
- Produces: `router` (APIRouter, no prefix — `/api` added by the parent). `AuthConfigOut(BaseModel){google_oidc_enabled: bool, allowed_domains: list[str]}`.

- [ ] **Step 1: Write the failing test** — `tests/test_api_auth.py`

```python
from fastapi.testclient import TestClient

from call_manager.main import create_app


def _app(monkeypatch, pg_url, **extra):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
        **extra,
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def test_config_reports_domains_and_google_disabled(monkeypatch, pg_url):
    app = _app(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.get("/api/auth/config")
    assert r.status_code == 200
    body = r.json()
    assert body["google_oidc_enabled"] is False
    assert body["allowed_domains"] == ["ki.se"]


def test_config_reports_google_enabled(monkeypatch, pg_url):
    app = _app(
        monkeypatch, pg_url,
        GOOGLE_OIDC_ENABLED="true", GOOGLE_CLIENT_ID="cid", GOOGLE_CLIENT_SECRET="sec",
    )
    with TestClient(app) as c:
        r = c.get("/api/auth/config")
    assert r.json()["google_oidc_enabled"] is True


def test_config_is_public_no_cookie(monkeypatch, pg_url):
    app = _app(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.get("/api/auth/config")
    assert "set-cookie" not in {k.lower() for k in r.headers}
```

- [ ] **Step 2: Run — fails** `uv run pytest tests/test_api_auth.py -q` → 404 (route missing).

- [ ] **Step 3: Create `src/call_manager/web/api/auth.py`**

```python
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter()


class AuthConfigOut(BaseModel):
    google_oidc_enabled: bool
    allowed_domains: list[str]


@router.get("/auth/config", response_model=AuthConfigOut)
def auth_config(request: Request) -> AuthConfigOut:
    """Public, unauthenticated. The pre-auth React pages read this to decide
    whether to show 'Continue with Google' and to derive the email-domain hint —
    they CANNOT call an authenticated /api endpoint (would 401-loop)."""
    s = request.app.state.settings
    return AuthConfigOut(
        google_oidc_enabled=s.google_oidc_enabled,
        allowed_domains=list(s.allowed_email_domains),
    )
```

- [ ] **Step 4: Register the router** — in `src/call_manager/web/api/__init__.py`, add `auth,` to the `from call_manager.web.api import (...)` tuple (alphabetical: before `calls`) and add `router.include_router(auth.router)` (place it first, before `me`, so it reads as the entry surface).

- [ ] **Step 5: Run** `uv run pytest tests/test_api_auth.py -q` → PASS.

- [ ] **Step 6: ruff + commit**

```bash
uv run ruff format src/call_manager/web/api/auth.py tests/test_api_auth.py
uv run ruff check src/call_manager/web/api/auth.py src/call_manager/web/api/__init__.py tests/test_api_auth.py
git add -A && git commit -m "feat(api): public GET /api/auth/config (google flag + domains) (auth slice)"
```

---

## Task 3: `POST /api/auth/login`

**Files:**
- Modify: `src/call_manager/web/api/auth.py`
- Test: `tests/test_api_auth.py`

**Interfaces:**
- Consumes: `client_ip`, `email_fingerprint`, `MAX_EMAIL_LEN` (Task 1); `verify_password`, `safe_next`, `SessionSigner` (existing). `app.state.{email_limiter,ip_limiter,session_factory,session_signer,settings}`.
- Produces: `LoginIn(BaseModel){email: str, password: str, next: str | None = None}`, `LoginOut(BaseModel){redirect: str}`. `POST /api/auth/login` → `200 LoginOut` (active/pending/no-access) or `400` (generic failure).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_api_auth.py`

```python
import call_manager.web.api.auth as apiauth
from call_manager.auth.passwords import hash_password
from call_manager.db.models import User


def _user(sf, *, email, status, password=None):
    with sf() as s:
        u = User(
            email=email, status=status,
            password_hash=hash_password(password) if password else None,
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def test_login_active_sets_cookie_redirect_root(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _user(app.state.session_factory, email="a@ki.se", status="active", password="correct-horse-1")
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"email": "a@ki.se", "password": "correct-horse-1"})
    assert r.status_code == 200 and r.json() == {"redirect": "/"}
    assert "session=" in "".join(r.headers.get_list("set-cookie"))


def test_login_active_honors_safe_next(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _user(app.state.session_factory, email="a@ki.se", status="active", password="correct-horse-1")
    with TestClient(app) as c:
        ok = c.post(
            "/api/auth/login",
            json={"email": "a@ki.se", "password": "correct-horse-1", "next": "/calls/5"},
        )
        bad = c.post(
            "/api/auth/login",
            json={"email": "a@ki.se", "password": "correct-horse-1", "next": "//evil.example"},
        )
    assert ok.json()["redirect"] == "/calls/5"
    assert bad.json()["redirect"] == "/"  # open-redirect rejected -> "/"


def test_login_pending_and_no_access_no_cookie(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _user(app.state.session_factory, email="p@ki.se", status="pending", password="correct-horse-1")
    _user(app.state.session_factory, email="d@ki.se", status="disabled", password="correct-horse-1")
    with TestClient(app) as c:
        rp = c.post("/api/auth/login", json={"email": "p@ki.se", "password": "correct-horse-1"})
        rd = c.post("/api/auth/login", json={"email": "d@ki.se", "password": "correct-horse-1"})
    assert rp.json() == {"redirect": "/auth/pending"}
    assert rd.json() == {"redirect": "/auth/no-access"}
    for r in (rp, rd):
        assert "set-cookie" not in {k.lower() for k in r.headers}


def test_login_failures_identical_400_no_cookie(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _user(app.state.session_factory, email="a@ki.se", status="active", password="correct-horse-1")
    _user(app.state.session_factory, email="g@ki.se", status="active", password=None)  # oauth-only
    with TestClient(app) as c:
        wrong = c.post("/api/auth/login", json={"email": "a@ki.se", "password": "nope-long-enough"})
        unknown = c.post("/api/auth/login", json={"email": "ghost@ki.se", "password": "nope-long-enough"})
        oauth = c.post("/api/auth/login", json={"email": "g@ki.se", "password": "nope-long-enough"})
    assert wrong.status_code == unknown.status_code == oauth.status_code == 400
    assert wrong.json() == unknown.json() == oauth.json()  # byte-identical -> no enumeration
    assert wrong.json()["error"]["message"] == "Invalid email or password."
    for r in (wrong, unknown, oauth):
        assert "set-cookie" not in {k.lower() for k in r.headers}


def test_login_dummy_hash_invoked_for_unknown_email(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    seen = []
    real = apiauth.verify_password
    monkeypatch.setattr(apiauth, "verify_password", lambda h, p: (seen.append(h), real(h, p))[1])
    with TestClient(app) as c:
        c.post("/api/auth/login", json={"email": "ghost@ki.se", "password": "whatever-long"})
    assert seen == [None]  # verified against dummy (stored hash None)


def test_login_rate_limited_folds_into_400(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _user(app.state.session_factory, email="a@ki.se", status="active", password="correct-horse-1")
    from call_manager.auth.rate_limit import SlidingWindowLimiter

    app.state.email_limiter = SlidingWindowLimiter(max_requests=2, window_seconds=60)
    with TestClient(app) as c:
        for _ in range(2):
            c.post("/api/auth/login", json={"email": "a@ki.se", "password": "bad-but-long"})
        # now rate-limited even WITH the correct password
        r = c.post("/api/auth/login", json={"email": "a@ki.se", "password": "correct-horse-1"})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "Invalid email or password."  # non-disclosure
    assert "set-cookie" not in {k.lower() for k in r.headers}


def test_login_oversized_email_400_no_limiter_growth(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    huge = "a" * 400 + "@ki.se"
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"email": huge, "password": "x" * 16})
    assert r.status_code == 400
    assert huge not in app.state.email_limiter._events
```

- [ ] **Step 2: Run — fails** `uv run pytest tests/test_api_auth.py -k login -q` → 404 / 405.

- [ ] **Step 3: Implement `POST /api/auth/login`** — add to `web/api/auth.py` (imports + endpoint). The cookie is set on the FastAPI-provided `Response` param; the pending/no-access returns set NO cookie. Cookie attrs match `web/routes/auth.py:148-155` and `web/api/me.py:165-172` verbatim:

```python
import logging
from datetime import UTC, datetime

from fastapi import HTTPException, Response
from sqlalchemy import select

from call_manager.auth.passwords import verify_password
from call_manager.auth.redirects import safe_next
from call_manager.db.models import User
from call_manager.web.client_ip import MAX_EMAIL_LEN, client_ip, email_fingerprint

_logger = logging.getLogger(__name__)

# The ONLY login-failure response. Generic + status 400 (NOT 401: unwrap loops to
# /login on 401; NOT 429 on rate-limit: non-disclosure). Identical across
# unknown-email / wrong-password / no-credential / rate-limited.
_INVALID = "Invalid email or password."


class LoginIn(BaseModel):
    email: str
    password: str
    next: str | None = None


class LoginOut(BaseModel):
    redirect: str


@router.post("/auth/login", response_model=LoginOut)
def login(request: Request, body: LoginIn, response: Response) -> LoginOut:
    settings = request.app.state.settings
    email_limiter = request.app.state.email_limiter
    ip_limiter = request.app.state.ip_limiter
    session_factory = request.app.state.session_factory
    session_signer = request.app.state.session_signer
    safe = safe_next(body.next)

    def _denied() -> HTTPException:
        return HTTPException(status_code=400, detail=_INVALID)

    if len(body.email) > MAX_EMAIL_LEN:
        raise _denied()
    email_normalized = body.email.strip().lower()
    email_fp = email_fingerprint(email_normalized)

    if not ip_limiter.allow(client_ip(request)) or not email_limiter.allow(email_normalized):
        _logger.info("login rate-limited", extra={"event": "login_rate_limited", "email_fp": email_fp})
        raise _denied()

    with session_factory() as session:
        user = session.execute(
            select(User).where(User.email == email_normalized)
        ).scalar_one_or_none()
        stored = user.password_hash if user is not None else None
        if not verify_password(stored, body.password):  # dummy-hash path when stored is None
            _logger.info("login failed", extra={"event": "login_failed", "email_fp": email_fp})
            raise _denied()
        if user.status != "active":
            # Mirrors web/routes/auth.py:140 — pending vs the no-access CATCH-ALL.
            # Do NOT hard-code status == "disabled" (silently drops a future status).
            target = "/auth/pending" if user.status == "pending" else "/auth/no-access"
            return LoginOut(redirect=target)  # no cookie
        user.last_login_at = datetime.now(UTC)
        user_id = user.id
        session_epoch = user.session_epoch
        session.commit()

    response.set_cookie(
        "session",
        session_signer.make(user_id=user_id, session_epoch=session_epoch),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    _logger.info("login success", extra={"event": "login_success", "email_fp": email_fp})
    return LoginOut(redirect=safe or "/")
```

- [ ] **Step 4: Run** `uv run pytest tests/test_api_auth.py -q` → all PASS.

- [ ] **Step 5: ruff + commit**

```bash
uv run ruff format src/call_manager/web/api/auth.py tests/test_api_auth.py
uv run ruff check src/call_manager/web/api/auth.py tests/test_api_auth.py
git add -A && git commit -m "feat(api): POST /api/auth/login (generic-400 parity, status routing, cookie) (auth slice)"
```

---

## Task 4: `POST /api/auth/register`

**Files:**
- Modify: `src/call_manager/web/api/auth.py`
- Test: `tests/test_api_auth.py`

**Interfaces:**
- Consumes: `domain_allowed` (existing), `verify_password`, `client_ip`, `email_fingerprint`, `MAX_EMAIL_LEN`.
- Produces: `RegisterIn(BaseModel){email: str, name: str | None = None}`, `RegisterOut(BaseModel){ok: bool}`. `POST /api/auth/register` → ALWAYS `200 {ok: true}`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_api_auth.py`

```python
from sqlalchemy import select as _select


def test_register_creates_pending_truncates_name(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.post("/api/auth/register", json={"email": "new@ki.se", "name": "z" * 300})
    assert r.status_code == 200 and r.json() == {"ok": True}
    with pg_session_factory() as s:
        u = s.execute(_select(User).where(User.email == "new@ki.se")).scalar_one()
        assert u.status == "pending" and u.password_hash is None and len(u.name) == 200


def test_register_duplicate_silent_identical_no_second_row(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        s.add(User(email="dupe@ki.se", status="active"))
        s.commit()
    with TestClient(app) as c:
        fresh = c.post("/api/auth/register", json={"email": "fresh@ki.se"})
        dup = c.post("/api/auth/register", json={"email": "dupe@ki.se"})
    assert fresh.status_code == dup.status_code == 200
    assert fresh.json() == dup.json() == {"ok": True}
    with pg_session_factory() as s:
        assert len(s.execute(_select(User).where(User.email == "dupe@ki.se")).scalars().all()) == 1


def test_register_dummy_hash_on_every_eligible_attempt(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        s.add(User(email="dupe@ki.se", status="active"))
        s.commit()
    calls = []
    real = apiauth.verify_password
    monkeypatch.setattr(apiauth, "verify_password", lambda h, p: (calls.append(h), real(h, p))[1])
    with TestClient(app) as c:
        c.post("/api/auth/register", json={"email": "new2@ki.se"})  # fresh
        c.post("/api/auth/register", json={"email": "dupe@ki.se"})  # duplicate
    assert calls == [None, None]  # dummy-hash on BOTH paths (timing parity)


def test_register_domain_disallowed_no_row(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.post("/api/auth/register", json={"email": "x@evil.com"})
    assert r.status_code == 200 and r.json() == {"ok": True}  # anti-enumeration
    with pg_session_factory() as s:
        assert s.execute(_select(User).where(User.email == "x@evil.com")).scalar_one_or_none() is None


def test_register_rate_limited_silent_no_row(monkeypatch, pg_url, pg_session_factory):
    """Rate-limited register stays anti-enumeration: always 200 {ok: true}, NO
    row created. (Closes the Codex-flagged register rate-limit gap.)"""
    app = _app(monkeypatch, pg_url)
    from call_manager.auth.rate_limit import SlidingWindowLimiter

    app.state.ip_limiter = SlidingWindowLimiter(max_requests=0, window_seconds=60)
    with TestClient(app) as c:
        r = c.post("/api/auth/register", json={"email": "limited@ki.se"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    with pg_session_factory() as s:
        assert s.execute(_select(User).where(User.email == "limited@ki.se")).scalar_one_or_none() is None
```

- [ ] **Step 2: Run — fails** `uv run pytest tests/test_api_auth.py -k register -q` → 404.

- [ ] **Step 3: Implement** — add to `web/api/auth.py` (extra imports + endpoint), mirroring `web/routes/register.py:31-80` verbatim except the response shape:

```python
from sqlalchemy.exc import IntegrityError

from call_manager.auth.domain import domain_allowed

_NAME_MAX = 200  # users.name is VARCHAR(200)


class RegisterIn(BaseModel):
    email: str
    name: str | None = None


class RegisterOut(BaseModel):
    ok: bool


@router.post("/auth/register", response_model=RegisterOut)
def register(request: Request, body: RegisterIn) -> RegisterOut:
    """Anti-enumeration: ALWAYS 200 {ok: true}. Silent on duplicate / disallowed
    domain / rate-limit; one dummy argon2 for timing parity on every eligible
    attempt. Verbatim port of web/routes/register.py."""
    settings = request.app.state.settings
    ip_limiter = request.app.state.ip_limiter
    email_limiter = request.app.state.email_limiter
    session_factory = request.app.state.session_factory
    ok = RegisterOut(ok=True)

    if len(body.email) > MAX_EMAIL_LEN:
        return ok
    email_normalized = body.email.strip().lower()
    if not ip_limiter.allow(client_ip(request)):
        return ok
    if not domain_allowed(email_normalized, settings.allowed_email_domains):
        return ok
    if not email_limiter.allow(email_normalized):
        return ok

    verify_password(None, "timing-parity")  # dummy-hash on BOTH fresh & dup paths
    name = (body.name or "").strip()[:_NAME_MAX] or None
    with session_factory() as session:
        session.add(User(email=email_normalized, name=name, status="pending"))
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            _logger.info(
                "register: email already exists",
                extra={"event": "register_duplicate", "email_fp": email_fingerprint(email_normalized)},
            )
            return ok
        session.commit()
    _logger.info(
        "register: request created",
        extra={"event": "register_created", "email_fp": email_fingerprint(email_normalized)},
    )
    return ok
```

- [ ] **Step 4: Run** `uv run pytest tests/test_api_auth.py -q` → all PASS.

- [ ] **Step 5: ruff + commit**

```bash
uv run ruff format src/call_manager/web/api/auth.py tests/test_api_auth.py
uv run ruff check src/call_manager/web/api/auth.py tests/test_api_auth.py
git add -A && git commit -m "feat(api): POST /api/auth/register (anti-enumeration, always-200) (auth slice)"
```

---

## Task 5: `POST /api/auth/logout`

**Files:**
- Modify: `src/call_manager/web/api/auth.py`
- Test: `tests/test_api_auth.py`

**Interfaces:**
- Consumes: `SessionSigner` (`app.state.session_signer`), `InvalidSession`, `User`.
- Produces: `LogoutOut(BaseModel){ok: bool}`. `POST /api/auth/logout` → `200 {ok: true}`; bumps `session_epoch`, clears the `session` cookie. Leaves `oauth_txn` UNTOUCHED.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_api_auth.py`

```python
def test_logout_bumps_epoch_and_clears_cookie(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    uid = _user(app.state.session_factory, email="a@ki.se", status="active", password="correct-horse-1")
    with TestClient(app) as c:
        c.cookies.set("session", app.state.session_signer.make(user_id=uid, session_epoch=0))
        r = c.post("/api/auth/logout")
    assert r.status_code == 200 and r.json() == {"ok": True}
    set_cookie = " ".join(r.headers.get_list("set-cookie"))
    assert "session=" in set_cookie and ("Max-Age=0" in set_cookie or "expires=" in set_cookie.lower())
    with pg_session_factory() as s:
        assert s.get(User, uid).session_epoch == 1  # revokes all outstanding cookies


def test_logout_without_cookie_is_noop_200(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.post("/api/auth/logout")
    assert r.status_code == 200 and r.json() == {"ok": True}
```

- [ ] **Step 2: Run — fails** `uv run pytest tests/test_api_auth.py -k logout -q` → 404.

- [ ] **Step 3: Implement** — add to `web/api/auth.py` (mirror `web/routes/auth.py:160-196` exactly, minus the redirect):

```python
from fastapi import Cookie

from call_manager.auth.session_cookie import InvalidSession


class LogoutOut(BaseModel):
    ok: bool


@router.post("/auth/logout", response_model=LogoutOut)
def logout(
    request: Request,
    response: Response,
    session: str | None = Cookie(default=None),
) -> LogoutOut:
    """Bump session_epoch (revokes EVERY outstanding cookie for the user) and
    clear the session cookie. Does NOT touch oauth_txn — strict parity with the
    deleted logout_submit. React then full-page-navs to /login."""
    settings = request.app.state.settings
    if session is not None:
        signer = request.app.state.session_signer
        session_factory = request.app.state.session_factory
        try:
            data = signer.verify(session)
        except InvalidSession:
            data = None
        if data is not None:
            with session_factory() as db:
                user = db.get(User, data.user_id)
                if user is not None:
                    user.session_epoch = (user.session_epoch or 0) + 1
                    db.commit()
    response.delete_cookie(
        "session", path="/", httponly=True, secure=settings.cookie_secure, samesite="lax"
    )
    return LogoutOut(ok=True)
```

- [ ] **Step 4: Run** `uv run pytest tests/test_api_auth.py -q` → all PASS.

- [ ] **Step 5: ruff + commit**

```bash
uv run ruff format src/call_manager/web/api/auth.py tests/test_api_auth.py
uv run ruff check src/call_manager/web/api/auth.py tests/test_api_auth.py
git add -A && git commit -m "feat(api): POST /api/auth/logout (epoch bump + clear cookie) (auth slice)"
```

---

## Task 6: OpenAPI regen #1 + `api/auth.ts` hooks

**Files:**
- Modify: `frontend/src/api/schema.gen.ts` (regenerated — do not hand-edit)
- Create: `frontend/src/api/auth.ts`, `frontend/src/api/auth.test.tsx`

**Interfaces:**
- Produces: `useAuthConfig()` (query `["auth-config"]`), `useLogin()` (mutation → `{redirect}`), `useRegister()` (mutation → `{ok}`), `useLogout()` (mutation → `{ok}`). Types from `schema.gen`.

- [ ] **Step 1: Regenerate the OpenAPI types** (so `/api/auth/*` paths exist in `schema.gen.ts`):

```bash
EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh
```

Expected: `frontend/src/api/schema.gen.ts` now contains `"/api/auth/config"`, `"/api/auth/login"`, `"/api/auth/register"`, `"/api/auth/logout"`.

- [ ] **Step 2: Write the failing hook tests** — `frontend/src/api/auth.test.tsx` (mirror `account.test.tsx` shape)

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import { useAuthConfig, useLogin, useLogout, useRegister } from "./auth";

afterEach(() => vi.restoreAllMocks());

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

it("useAuthConfig GETs /api/auth/config", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { google_oidc_enabled: true, allowed_domains: ["ki.se"] }, error: undefined,
  } as never);
  const { result } = renderHook(() => useAuthConfig(), { wrapper: wrapper(new QueryClient()) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith("/api/auth/config", {});
});

it("useLogin POSTs credentials + returns the redirect", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { redirect: "/" }, error: undefined, response: new Response(),
  } as never);
  const { result } = renderHook(() => useLogin(), { wrapper: wrapper(new QueryClient()) });
  const out = await result.current.mutateAsync({ email: "a@ki.se", password: "pw-long-enough" });
  expect(out).toEqual({ redirect: "/" });
  expect(post).toHaveBeenCalledWith("/api/auth/login", {
    body: { email: "a@ki.se", password: "pw-long-enough", next: null },
  });
});

it("useLogin throws ApiError on a 400 (unwrap does NOT redirect — 400 != 401)", async () => {
  // A 401 would trigger unwrap's redirectToLogin (window.location) -> loop; a 400
  // must just throw. Mocking a 400 here exercises that path without touching location.
  const redirect = vi.spyOn(clientMod, "redirectToLogin");
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    error: { error: { code: "bad_request", message: "Invalid email or password." } },
    response: new Response(null, { status: 400 }),
  } as never);
  const { result } = renderHook(() => useLogin(), { wrapper: wrapper(new QueryClient()) });
  await expect(
    result.current.mutateAsync({ email: "a@ki.se", password: "bad" }),
  ).rejects.toThrow(/Invalid email or password/);
  expect(redirect).not.toHaveBeenCalled(); // 400 != 401, no redirect loop
});

it("useRegister POSTs email+name", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { ok: true }, error: undefined,
  } as never);
  const { result } = renderHook(() => useRegister(), { wrapper: wrapper(new QueryClient()) });
  await result.current.mutateAsync({ email: "n@ki.se", name: "N" });
  expect(post).toHaveBeenCalledWith("/api/auth/register", { body: { email: "n@ki.se", name: "N" } });
});

it("useLogout POSTs logout", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { ok: true }, error: undefined,
  } as never);
  const { result } = renderHook(() => useLogout(), { wrapper: wrapper(new QueryClient()) });
  await result.current.mutateAsync();
  expect(post).toHaveBeenCalledWith("/api/auth/logout", {});
});
```

- [ ] **Step 3: Run — fails** `cd frontend && pnpm vitest run src/api/auth.test.tsx` → module missing.

- [ ] **Step 4: Implement `frontend/src/api/auth.ts`**

```ts
import { useMutation, useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";

export function useAuthConfig() {
  // PUBLIC endpoint — returns 200 logged-out, so it never 401-loops. This is the
  // ONLY /api call the pre-auth pages make; it is NOT an authenticated call.
  return useQuery({
    queryKey: ["auth-config"],
    queryFn: async () => unwrap(await api.GET("/api/auth/config", {})),
    retry: false,
    staleTime: Infinity,
  });
}

export function useLogin() {
  return useMutation({
    mutationFn: async (vars: { email: string; password: string; next?: string | null }) =>
      unwrap(
        await api.POST("/api/auth/login", {
          body: { email: vars.email, password: vars.password, next: vars.next ?? null },
        }),
      ),
  });
}

export function useRegister() {
  return useMutation({
    mutationFn: async (vars: { email: string; name?: string | null }) =>
      unwrap(await api.POST("/api/auth/register", { body: { email: vars.email, name: vars.name ?? null } })),
  });
}

export function useLogout() {
  return useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/auth/logout", {})),
  });
}
```

- [ ] **Step 5: Run + tsc** `cd frontend && pnpm vitest run src/api/auth.test.tsx && pnpm tsc --noEmit` → PASS / 0 errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/schema.gen.ts frontend/src/api/auth.ts frontend/src/api/auth.test.tsx
git commit -m "feat(spa): api/auth hooks + OpenAPI regen (auth slice)"
```

---

## Task 7: PreAuthLayout + holding pages + Register page

**Files:**
- Create: `frontend/src/routes/PreAuthLayout.tsx`, `frontend/src/routes/PreAuth.module.css`, `frontend/src/routes/AccountPending.tsx`, `frontend/src/routes/AccountNoAccess.tsx`, `frontend/src/routes/AuthError.tsx`, `frontend/src/routes/Register.tsx`
- Test: `frontend/src/routes/PreAuthLayout.test.tsx`, `frontend/src/routes/Register.test.tsx`

**Interfaces:**
- Consumes: `useRegister`, `useAuthConfig` (Task 6). `renderWithProviders` (test-utils).
- Produces: `PreAuthLayout` (default-less named export), `Register`, `AccountPending`, `AccountNoAccess`, `AuthError`. A shared `PreAuth.module.css` (`.wrap`, `.card`, `.brand`, `.field`, `.actions`, `.err`, `.muted`, `.link`).

- [ ] **Step 1: Write the failing tests** — `PreAuthLayout.test.tsx` (the anti-loop invariant) + `Register.test.tsx`

```tsx
// PreAuthLayout.test.tsx
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MantineProvider } from "@mantine/core";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { PreAuthLayout } from "./PreAuthLayout";

afterEach(() => vi.restoreAllMocks());

it("renders children with NO shell chrome and fires NO /api/me on mount (anti-loop invariant)", () => {
  const get = vi.spyOn(clientMod.api, "GET");
  render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/x"]}>
          <Routes>
            <Route element={<PreAuthLayout />}>
              <Route path="/x" element={<p>child</p>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
  expect(screen.getByText("child")).toBeInTheDocument();
  expect(screen.queryByLabelText("Primary")).not.toBeInTheDocument(); // no ActivityBar
  // The layout itself makes no authenticated call (no /api/me).
  for (const call of get.mock.calls) expect(call[0]).not.toBe("/api/me");
});
```

```tsx
// Register.test.tsx
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { Register } from "./Register";

afterEach(() => vi.restoreAllMocks());

it("submits email+name and shows the 'request received' state", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { google_oidc_enabled: false, allowed_domains: ["ki.se"] }, error: undefined,
  } as never);
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { ok: true }, error: undefined } as never);
  renderWithProviders(<Register />, { route: "/register" });
  await userEvent.type(screen.getByLabelText(/email/i), "new@ki.se");
  await userEvent.click(screen.getByRole("button", { name: /request access/i }));
  await waitFor(() => expect(screen.getByText(/request received/i)).toBeInTheDocument());
  expect(post).toHaveBeenCalledWith("/api/auth/register", { body: { email: "new@ki.se", name: null } });
});
```

- [ ] **Step 2: Run — fails** `cd frontend && pnpm vitest run src/routes/PreAuthLayout.test.tsx src/routes/Register.test.tsx`.

- [ ] **Step 3: Implement `PreAuthLayout.tsx`** (bare layout — NO `useSession`, NO shell)

```tsx
import { Outlet } from "react-router-dom";

import { BrandMark } from "../shell/BrandMark";
import styles from "./PreAuth.module.css";

// SECOND top-level pathless layout (sibling to AppShell). Centered card on the
// app background, NO ActivityBar / Sidebar / StatusBar. INVARIANT: this layout
// and its pages make NO authenticated /api call on mount — a useSession here
// would 401 -> unwrap -> redirectToLogin -> infinite loop. The public
// /api/auth/config call lives in the pages (Login/Register), not here.
export function PreAuthLayout() {
  return (
    <div className={styles.wrap}>
      <main className={styles.card}>
        <span className={styles.brand}>
          <BrandMark size={36} />
        </span>
        <Outlet />
      </main>
    </div>
  );
}
```

- [ ] **Step 4: Implement `PreAuth.module.css`** (scoped under hashed classes; dark-teal centered card)

```css
.wrap {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--app-editor);
  padding: 1rem;
}
.card {
  width: 100%;
  max-width: 22rem;
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
  padding: 1.75rem 1.5rem 2rem;
  border: 1px solid var(--app-border);
  border-radius: 10px;
  background: var(--app-sidebar);
  box-shadow: var(--app-shadow-sm);
}
.brand {
  display: flex;
  justify-content: center;
  color: var(--app-accent-bright);
}
.card h1 {
  margin: 0;
  text-align: center;
  font-size: 1.2rem;
  color: var(--app-heading);
}
.card p {
  margin: 0;
  color: var(--app-muted);
  font-size: 0.9rem;
  text-align: center;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  font-size: 0.85rem;
  color: var(--app-text);
}
.field input {
  background: var(--app-editor);
  border: 1px solid var(--app-border-strong);
  border-radius: 5px;
  padding: 0.45rem 0.55rem;
  color: var(--app-text);
  font: inherit;
}
.field input:focus {
  outline: 2px solid var(--app-accent);
  outline-offset: 1px;
}
.actions {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.actions button {
  background: var(--app-accent);
  color: #fff;
  border: none;
  border-radius: 5px;
  padding: 0.5rem 0.9rem;
  font: inherit;
  font-weight: 500;
  cursor: pointer;
}
.actions button:disabled {
  opacity: 0.6;
  cursor: default;
}
.secondary {
  background: transparent !important;
  color: var(--app-text) !important;
  border: 1px solid var(--app-border-strong) !important;
}
.err {
  color: var(--app-bad);
  font-size: 0.85rem;
  text-align: center;
  margin: 0;
}
.muted {
  color: var(--app-muted);
  font-size: 0.8rem;
}
.link {
  text-align: center;
  font-size: 0.85rem;
}
.link a {
  color: var(--app-link);
}
```

- [ ] **Step 5: Implement the 3 holding pages** — copy verbatim from the deleted templates.

`AccountPending.tsx`:

```tsx
import { Link } from "react-router-dom";

import styles from "./PreAuth.module.css";

export function AccountPending() {
  return (
    <>
      <h1>Account awaiting approval</h1>
      <p>
        Your sign-in worked. An administrator needs to approve your account before
        you can use Call Manager. You'll be able to sign in once approved.
      </p>
      <p className={styles.link}>
        <Link to="/login">Back to sign in</Link>
      </p>
    </>
  );
}
```

`AccountNoAccess.tsx`:

```tsx
import { Link } from "react-router-dom";

import styles from "./PreAuth.module.css";

export function AccountNoAccess() {
  return (
    <>
      <h1>No access</h1>
      <p>
        This account does not have access. If you think this is a mistake, contact
        an administrator.
      </p>
      <p className={styles.link}>
        <Link to="/login">Back to sign in</Link>
      </p>
    </>
  );
}
```

`AuthError.tsx`:

```tsx
import { Link } from "react-router-dom";

import styles from "./PreAuth.module.css";

export function AuthError() {
  return (
    <>
      <h1>Sign-in could not be completed</h1>
      <p>We couldn't sign you in. Please try again.</p>
      <p>
        If you already have an account with this email, sign in your usual way, then
        connect Google from your account settings.
      </p>
      <p className={styles.link}>
        <Link to="/login">Back to sign in</Link>
      </p>
    </>
  );
}
```

- [ ] **Step 6: Implement `Register.tsx`** (email + optional name → `useRegister` → inline "request received"; reads `useAuthConfig` for the domain hint)

```tsx
import { useState } from "react";
import { Link } from "react-router-dom";

import { useAuthConfig, useRegister } from "../api/auth";
import styles from "./PreAuth.module.css";

export function Register() {
  const cfg = useAuthConfig().data;
  const domains = cfg?.allowed_domains ?? [];
  const register = useRegister();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");

  if (register.isSuccess) {
    return (
      <>
        <h1>Request received</h1>
        <p>
          Thanks — your access request has been recorded. An administrator will
          review it and set up your account. You'll be able to sign in once it's
          approved.
        </p>
        <p className={styles.link}>
          <Link to="/login">Back to sign in</Link>
        </p>
      </>
    );
  }

  return (
    <>
      <h1>Request access</h1>
      <p>
        Enter your hospital email. An administrator will review your request and set
        up your account.
      </p>
      <form
        className={styles.actions}
        onSubmit={(e) => {
          e.preventDefault();
          register.mutate({ email, name: name.trim() || null });
        }}
      >
        <label className={styles.field}>
          Email
          <input
            type="email"
            required
            autoFocus
            autoComplete="email"
            value={email}
            placeholder={`you@${domains[0] ?? "example.org"}`}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label className={styles.field}>
          Name (optional)
          <input
            type="text"
            maxLength={200}
            autoComplete="name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        {domains.length > 0 && (
          <span className={styles.muted}>Use your hospital email ({domains.join(" or ")}).</span>
        )}
        <button type="submit" disabled={register.isPending}>
          Request access
        </button>
      </form>
      <p className={styles.link}>
        <Link to="/login">Back to sign in</Link>
      </p>
    </>
  );
}
```

- [ ] **Step 7: Run + tsc** `cd frontend && pnpm vitest run src/routes/PreAuthLayout.test.tsx src/routes/Register.test.tsx && pnpm tsc --noEmit` → PASS / 0.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/routes/PreAuthLayout.tsx frontend/src/routes/PreAuth.module.css frontend/src/routes/AccountPending.tsx frontend/src/routes/AccountNoAccess.tsx frontend/src/routes/AuthError.tsx frontend/src/routes/Register.tsx frontend/src/routes/PreAuthLayout.test.tsx frontend/src/routes/Register.test.tsx
git commit -m "feat(spa): PreAuthLayout + holding pages + Register (auth slice)"
```

---

## Task 8: Login page

**Files:**
- Create: `frontend/src/routes/Login.tsx`, `frontend/src/routes/Login.test.tsx`

**Interfaces:**
- Consumes: `useAuthConfig`, `useLogin` (Task 6); `ApiError` (client.ts); `useNavigate`, `useSearchParams` (react-router); `useQueryClient`.
- Produces: `Login` (named export).

- [ ] **Step 1: Write the failing tests** — `Login.test.tsx`

```tsx
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { ApiError } from "../api/client";
import { renderWithProviders } from "../test-utils";
import { Login } from "./Login";

afterEach(() => vi.restoreAllMocks());

function mockConfig(enabled: boolean) {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { google_oidc_enabled: enabled, allowed_domains: ["ki.se"] }, error: undefined,
  } as never);
}

it("renders email/password and the 'Request access' link; no Google button when disabled", async () => {
  mockConfig(false);
  renderWithProviders(<Login />, { route: "/login" });
  expect(screen.getByLabelText(/email/i)).toBeInTheDocument();
  expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /request access/i })).toHaveAttribute("href", "/register");
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: /continue with google/i })).not.toBeInTheDocument(),
  );
});

it("shows a native Google POST form when config enables it", async () => {
  mockConfig(true);
  renderWithProviders(<Login />, { route: "/login" });
  const btn = await screen.findByRole("button", { name: /continue with google/i });
  expect(btn.closest("form")).toHaveAttribute("action", "/auth/google/start");
  expect(btn.closest("form")).toHaveAttribute("method", "post");
});

it("on success invalidates ['me'] and navigates to the server redirect", async () => {
  mockConfig(false);
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { redirect: "/calls/5" }, error: undefined, response: new Response(),
  } as never);
  renderWithProviders(<Login />, { route: "/login?next=/calls/5" });
  await userEvent.type(screen.getByLabelText(/email/i), "a@ki.se");
  await userEvent.type(screen.getByLabelText(/password/i), "correct-horse-1");
  await userEvent.click(screen.getByRole("button", { name: /^sign in$/i }));
  // threads ?next into the POST
  await waitFor(() =>
    expect(clientMod.api.POST).toHaveBeenCalledWith("/api/auth/login", {
      body: { email: "a@ki.se", password: "correct-horse-1", next: "/calls/5" },
    }),
  );
});

it("shows ONE generic message for every failure mode (anti-enumeration)", async () => {
  mockConfig(false);
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    error: { error: { code: "bad_request", message: "Invalid email or password." } },
    response: new Response(null, { status: 400 }),
  } as never);
  renderWithProviders(<Login />, { route: "/login" });
  await userEvent.type(screen.getByLabelText(/email/i), "ghost@ki.se");
  await userEvent.type(screen.getByLabelText(/password/i), "whatever-long");
  await userEvent.click(screen.getByRole("button", { name: /^sign in$/i }));
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Invalid email or password.");
  expect(alert).not.toHaveTextContent("bad_request"); // code prefix stripped
});

it("ApiError message strips to the bare server message", () => {
  // Pins the strip helper contract used by the component.
  const e = new ApiError("bad_request", "Invalid email or password.");
  expect(e.message.slice(e.code.length + 2)).toBe("Invalid email or password.");
});
```

- [ ] **Step 2: Run — fails** `cd frontend && pnpm vitest run src/routes/Login.test.tsx`.

- [ ] **Step 3: Implement `Login.tsx`**

```tsx
import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import { useAuthConfig, useLogin } from "../api/auth";
import { ApiError } from "../api/client";
import styles from "./PreAuth.module.css";

function errorMessage(err: unknown): string {
  // unwrap throws ApiError(code, "code: message"); strip the code prefix so the
  // form shows the bare server message (always the generic one for a 400).
  if (err instanceof ApiError) return err.message.slice(err.code.length + 2);
  return "Something went wrong. Please try again.";
}

export function Login() {
  const cfg = useAuthConfig().data;
  const [sp] = useSearchParams();
  const next = sp.get("next");
  const navigate = useNavigate();
  const qc = useQueryClient();
  const login = useLogin();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  return (
    <>
      <h1>Sign in</h1>
      <p>Enter your email and password.</p>
      {login.isError && (
        <p className={styles.err} role="alert">
          {errorMessage(login.error)}
        </p>
      )}
      <form
        className={styles.actions}
        onSubmit={(e) => {
          e.preventDefault();
          login.mutate(
            { email, password, next },
            {
              onSuccess: (data) => {
                qc.invalidateQueries({ queryKey: ["me"] });
                navigate(data.redirect);
              },
            },
          );
        }}
      >
        <label className={styles.field}>
          Email
          <input
            type="email"
            required
            autoFocus
            autoComplete="email"
            value={email}
            placeholder={`you@${cfg?.allowed_domains[0] ?? "example.org"}`}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label className={styles.field}>
          Password
          <input
            type="password"
            required
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <button type="submit" disabled={login.isPending}>
          Sign in
        </button>
      </form>
      {cfg?.google_oidc_enabled && (
        // Native server-side OAuth start: a full-page POST carries Origin/CSRF and
        // 303s to Google (cannot be a fetch). safe_next is applied server-side.
        <form method="post" action="/auth/google/start" className={styles.actions}>
          {next && <input type="hidden" name="next" value={next} />}
          <button type="submit" className={styles.secondary}>
            Continue with Google
          </button>
        </form>
      )}
      <p className={styles.link}>
        <Link to="/register">Request access</Link>
      </p>
    </>
  );
}
```

- [ ] **Step 4: Run + tsc** `cd frontend && pnpm vitest run src/routes/Login.test.tsx && pnpm tsc --noEmit` → PASS / 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/routes/Login.tsx frontend/src/routes/Login.test.tsx
git commit -m "feat(spa): Login page (config-driven Google, generic-400 inline error, next threading) (auth slice)"
```

---

## Task 9: Router wiring + manifest flip + routes regen

**Files:**
- Modify: `frontend/src/router.tsx`, `frontend/routes.json`, `frontend/src/routes.gen.ts` (regen), `src/call_manager/web/spa_routes.py` (regen), `frontend/src/dev-proxy.test.ts`, `tests/test_gen_routes.py`
- Test: `frontend/src/router.test.tsx` (existing set-equality guard — auto), add a render assertion

**Interfaces:**
- Consumes: `PreAuthLayout`, `Login`, `Register`, `AccountPending`, `AccountNoAccess`, `AuthError` (Tasks 7–8).
- Produces: manifest `spa_routes` now includes `/login`, `/register`, `/auth/pending`, `/auth/no-access`, `/auth/error`.

- [ ] **Step 1: Edit `frontend/routes.json`** — add the pre-auth leaves to `spa_routes`; remove `/login`, `/logout`, `/register` from `backend_prefixes` (keep `/auth` — OAuth lives there, doc-only):

```json
{
  "spa_routes": ["/", "/cockpit", "/calls", "/me", "/profiles", "/admin",
    "/login", "/register", "/auth/pending", "/auth/no-access", "/auth/error"],
  "backend_prefixes": ["/api", "/healthz", "/auth", "/workspaces", "/static"],
  "_comment": "spa_routes = SPA-owned page routes, matched EXACTLY (not by prefix). /login, /register and the three /auth/* holding pages are React (auth slice). Bare /auth is NOT an spa_route: it would make is_spa_route('/auth/google/start') True and let the dev Vite proxy shadow the real OAuth endpoints (dev-proxy.ts keys off isSpaRoute with no backend-precedence check); the three explicit leaves avoid that. /auth stays in backend_prefixes (doc-only) because POST /auth/google/start + GET /auth/google/callback live there. backend_prefixes / is_backend_route are NOT consulted by the serving or proxy path."
}
```

- [ ] **Step 2: Regenerate route artifacts**

```bash
uv run python scripts/gen_routes.py
```

Expected: `frontend/src/routes.gen.ts` `SPA_ROUTES` now lists the 5 new entries; `BACKEND_PREFIXES` drops `/login`/`/register`/`/logout`. `src/call_manager/web/spa_routes.py` regenerated to match.

- [ ] **Step 3: Wire the router** — in `frontend/src/router.tsx`, add the imports and a SECOND top-level pathless layout (sibling to the `AppShell` object) in the `createBrowserRouter([...])` array:

```tsx
import { AccountNoAccess } from "./routes/AccountNoAccess";
import { AccountPending } from "./routes/AccountPending";
import { AuthError } from "./routes/AuthError";
import { Login } from "./routes/Login";
import { PreAuthLayout } from "./routes/PreAuthLayout";
import { Register } from "./routes/Register";
```

Append this object as the second element of the top-level array (after the `AppShell` object):

```tsx
  {
    // Pre-auth pages: a SECOND pathless layout, sibling to AppShell. No IDE shell,
    // no useSession (the anti-401-loop invariant). An exact path like "login"
    // out-ranks AppShell's "*" splat, so /login matches HERE, not the soft-404.
    element: <PreAuthLayout />,
    children: [
      { path: "login", element: <Login /> },
      { path: "register", element: <Register /> },
      { path: "auth/pending", element: <AccountPending /> },
      { path: "auth/no-access", element: <AccountNoAccess /> },
      { path: "auth/error", element: <AuthError /> },
    ],
  },
```

- [ ] **Step 4: Add a router render assertion** — append to `frontend/src/router.test.tsx`:

```tsx
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { theme } from "./theme/theme";

it("/login renders the pre-auth Login, not the AppShell soft-404", () => {
  const r = createMemoryRouter(router.routes, { initialEntries: ["/login"] });
  render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={new QueryClient()}>
        <RouterProvider router={r} />
      </QueryClientProvider>
    </MantineProvider>,
  );
  expect(screen.getByRole("heading", { name: /sign in/i })).toBeInTheDocument();
  expect(screen.queryByLabelText("Primary")).not.toBeInTheDocument(); // no ActivityBar
});
```

(The existing set-equality test auto-validates `SPA_ROUTES` vs the router's leaves: the new leaves `/login`, `/register`, `/auth/pending|no-access|error` each map to themselves via `toOwningPrefix`, matching the manifest.)

- [ ] **Step 5: Update `frontend/src/dev-proxy.test.ts`** — the manifest flip changes `/login`/`/register` from backend to SPA. Update the assertions:
  - Line ~15 (`isSpaRoute` "does NOT own" block): change `expect(isSpaRoute("/login")).toBe(false)` → `toBe(true)`, and add `expect(isSpaRoute("/auth/pending")).toBe(true)` and `expect(isSpaRoute("/auth/google/start")).toBe(false)` (NOT owned — the leaves are exact, not a bare `/auth` prefix).
  - Line ~64 (`shouldServeViaVite`): change `expect(shouldServeViaVite("/login")).toBe(false)` → `toBe(true)`; add `expect(shouldServeViaVite("/auth/google/start")).toBe(false)` (forwards to uvicorn — OAuth not shadowed).

- [ ] **Step 6: Update `tests/test_gen_routes.py`** — `test_is_spa_route_is_exact_not_prefix` (line 35): change `assert is_spa_route("/login") is False` → `is True`, and add `assert is_spa_route("/auth/pending") is True` and `assert is_spa_route("/auth/google/start") is False`.

- [ ] **Step 7: Run gates**

```bash
cd frontend && pnpm vitest run src/router.test.tsx src/dev-proxy.test.ts && pnpm tsc --noEmit && cd ..
uv run python scripts/gen_routes.py --check
uv run pytest tests/test_gen_routes.py -q
```

Expected: all PASS; `--check` reports in sync.

- [ ] **Step 8: Commit (routes + manifest + regens in ONE commit)**

```bash
git add frontend/routes.json frontend/src/routes.gen.ts src/call_manager/web/spa_routes.py frontend/src/router.tsx frontend/src/router.test.tsx frontend/src/dev-proxy.test.ts tests/test_gen_routes.py
git commit -m "feat(spa): wire pre-auth routes + flip manifest (/login,/register,/auth/* -> SPA) (auth slice)"
```

---

## Task 10: Relocate sign-out into the shell `TitleBar`

**Files:**
- Modify: `frontend/src/shell/TitleBar.tsx`, `frontend/src/shell/TitleBar.module.css`, `frontend/src/shell/TitleBar.test.tsx`, `frontend/src/routes/DashboardHeader.tsx`, `frontend/src/routes/DashboardHeader.module.css`, `frontend/src/routes/DashboardHeader.test.tsx`

**Interfaces:**
- Consumes: `useSession` (session.ts — `data.user.email`), `useLogout` (Task 6).

- [ ] **Step 1: Update the failing tests first.** `TitleBar.test.tsx` — adding `useSession` means TitleBar now fires `api.GET("/api/me")` in isolation, so the three `expect(get).not.toHaveBeenCalled()` assertions must become "did not fetch the workspace meta." Replace `renderAt` and those assertions:

```tsx
// renderAt: provide a default /api/me mock so useSession resolves; tests that
// assert "no meta fetch" check the cockpit-meta path specifically.
function renderAt(path: string) {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <TitleBar />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const ME = { data: { user: { id: 1, email: "p@ki.se", status: "active", default_profile_id: null }, capabilities: { admin: false } }, error: undefined };
const META = (title: string) => ({ data: { workspace_title: title, workspace_deadline: null, current_phase: "active", role: "owner", spec_locked: false, member_count: 1 }, error: undefined });

it("shows 'Funding calls' off the cockpit and does not fetch workspace meta", () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue(ME as never);
  renderAt("/");
  expect(screen.getByText("Funding calls")).toBeInTheDocument();
  // /api/me is allowed (the session chip); the cockpit-meta endpoint is NOT hit.
  for (const call of get.mock.calls)
    expect(call[0]).not.toBe("/api/workspaces/{workspace_id}/cockpit-meta");
});
```

Apply the same change to the `/cockpit` and `/cockpit/new` "does not fetch" tests (rename to "does not fetch workspace meta", same per-call assertion). The cockpit-title test mocks BOTH endpoints by path:

```tsx
it("shows the workspace title on the cockpit", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation((path: string) =>
    Promise.resolve((path === "/api/me" ? ME : META("AMR surveillance grant")) as never),
  );
  renderAt("/cockpit/ws-1");
  expect(await screen.findByText("AMR surveillance grant")).toBeInTheDocument();
});
```

Add the new sign-out/chip tests:

```tsx
it("shows the signed-in email chip and a working sign-out", async () => {
  const assign = vi.fn();
  vi.stubGlobal("location", { assign } as never);
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(ME as never);
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { ok: true }, error: undefined } as never);
  renderAt("/");
  expect(await screen.findByText("p@ki.se")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /sign out/i }));
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/auth/logout", {}));
  await waitFor(() => expect(assign).toHaveBeenCalledWith("/login"));
  vi.unstubAllGlobals();
});
```

(Add the imports `userEvent` from `@testing-library/user-event` and `waitFor` from `@testing-library/react` at the top of the test file.)

- [ ] **Step 2: Run — fails** `cd frontend && pnpm vitest run src/shell/TitleBar.test.tsx`.

- [ ] **Step 3: Implement the TitleBar change** — add `useSession` + `useLogout`, render an email chip + sign-out button at the top-right. In `TitleBar.tsx`, add imports and the right-side controls:

```tsx
import { useSession } from "../api/session";
import { useLogout } from "../api/auth";
```

Inside the component, after the existing `isOwner` line:

```tsx
  const email = useSession().data?.user.email;
  const logout = useLogout();
  const signOut = () =>
    logout.mutate(undefined, { onSuccess: () => window.location.assign("/login") });
```

Then in the returned `<header>`, after the settings-gear block, add (the gear keeps `margin-left:auto`; the new group sits to its right — see CSS step):

```tsx
      <span className={styles.account}>
        {email && <span className={styles.chip} title={email}>{email}</span>}
        <button type="button" className={styles.signout} onClick={signOut} disabled={logout.isPending}>
          Sign out
        </button>
      </span>
```

Because the settings gear currently uses `margin-left:auto` to push itself right, move that auto-margin onto the `.account` group instead (so both the gear and the account group sit on the right in order). Update the gear's wrapper: remove `margin-left:auto` from `.settingsGear` (Step 4) and add it to `.account`.

- [ ] **Step 4: TitleBar CSS** — in `TitleBar.module.css`, change `.settingsGear { margin-left: auto; ... }` to drop `margin-left: auto`, and add:

```css
.account {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}
.chip {
  color: var(--app-muted-chrome);
  max-width: 16rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.signout {
  background: none;
  border: 1px solid var(--app-border-strong);
  border-radius: 4px;
  color: var(--app-text);
  font: inherit;
  font-size: 11px;
  padding: 2px 8px;
  cursor: pointer;
}
.signout:disabled {
  opacity: 0.6;
  cursor: default;
}
```

(When the owner gear is present it renders before `.account`; both sit right. If you prefer the gear immediately left of the chip, keep the gear before `.account` in the JSX — it already is.)

- [ ] **Step 5: Remove the sign-out from `DashboardHeader.tsx`** — delete the comment + `<form>` block (lines 29–33) so the header ends after the `.counts` div. Remove the now-unused `.signout` rule from `DashboardHeader.module.css` (lines 19–21).

- [ ] **Step 6: Update `DashboardHeader.test.tsx`** — drop the logout-form assertions (the form moved to the shell). Rename the test and remove its last two lines:

```tsx
it("shows the breadcrumb and the summary counts verbatim", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData() as never);
  renderWithProviders(<DashboardHeader />, { route: "/" });
  expect(screen.getByText("Calls")).toBeInTheDocument();
  await waitFor(() => expect(screen.getByText("12")).toBeInTheDocument());
  expect(screen.getByText("5")).toBeInTheDocument();
  expect(screen.getByText("3")).toBeInTheDocument();
});
```

- [ ] **Step 7: Run + tsc** `cd frontend && pnpm vitest run src/shell/TitleBar.test.tsx src/routes/DashboardHeader.test.tsx && pnpm tsc --noEmit` → PASS / 0.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/shell/TitleBar.tsx frontend/src/shell/TitleBar.module.css frontend/src/shell/TitleBar.test.tsx frontend/src/routes/DashboardHeader.tsx frontend/src/routes/DashboardHeader.module.css frontend/src/routes/DashboardHeader.test.tsx
git commit -m "feat(spa): relocate sign-out + email chip into TitleBar; drop DashboardHeader form (auth slice)"
```

---

## Task 11: Teardown — delete the web Jinja stack + migrate tests

This is the atomic teardown. After it, FastAPI serves pure JSON `/api` + OAuth redirects + the static React bundle; no web Jinja/HTMX/Pico. The `digest/` Jinja env is untouched.

**Files:**
- Delete: `src/call_manager/web/routes/auth.py`, `src/call_manager/web/routes/register.py`; templates `login.html`, `register.html`, `register_received.html`, `account_pending.html`, `account_no_access.html`, `auth_error.html`, `error.html`, `base.html`; static `pico.min.css`, `app.css`, `app.js`, `htmx.min.js`, `htmx-sse.js`, `dedup.js`; tests `tests/test_web_login_password.py`, `tests/test_web_register.py`, `tests/test_web_login_google_button.py`
- Modify: `src/call_manager/main.py`, `src/call_manager/web/routes/oauth_google.py`, `frontend/src/api/client.ts`, `frontend/src/api/session.ts`, `tests/test_web_auth.py`, `tests/test_auth_inactive_enforcement.py`, `tests/test_security_headers.py`, `tests/test_spa_serving.py`
- Create: `tests/test_auth_teardown_guard.py`

- [ ] **Step 1: Trim `oauth_google.py`** — delete the three holding-page GET routes (`auth_pending`, `auth_no_access`, `auth_error`, lines 182–200) and the now-unused `HTMLResponse` import (line 7 — keep `RedirectResponse`). The OAuth start/callback routes are unchanged.

- [ ] **Step 2: Delete the doomed route modules** `git rm src/call_manager/web/routes/auth.py src/call_manager/web/routes/register.py`.

- [ ] **Step 3: Edit `main.py`.** Make these removals/changes:
  - Remove `from urllib.parse import urlencode` (line 3) and `from fastapi.templating import Jinja2Templates` (line 8) and `from call_manager.auth.redirects import safe_next` (line 23) — all become unused.
  - **Change `from fastapi.responses import JSONResponse, RedirectResponse` (line 6) to `from fastapi.responses import JSONResponse`** — `RedirectResponse`'s only uses (lines 374, 378, 407) live inside the two branches collapsed below, so it orphans and trips `ruff check .` (F401). [VERIFIED empirically via Codex probe — this was a plan miss.] `JSONResponse` stays (used by both collapsed handlers + `_api_validation` + `_api_500`).
  - Update the now-stale `/static` mount comment (lines 516–520: "Vendored static assets (htmx.min.js lives here)…") — htmx is deleted; the mount now serves only `favicon.svg`. Keep the no-CDN rationale, drop the htmx reference.
  - Remove the filter imports (lines 54–61: `criterion_label, display_date, display_dt, model_label, note_category_heading, phase_label`) — `web/filters.py` STAYS (it's imported by `web/api/calls.py` and `digest/render.py`), but main.py no longer uses these.
  - In `from call_manager.web.routes import (auth, health, oauth_google, register)` (lines 67–72) remove `auth,` and `register,` (keep `health, oauth_google`).
  - Remove `TEMPLATES_DIR = ...` (line 75). Keep `STATIC_DIR` (used by the `/static` mount).
  - **Collapse `_redirect_html_401`** (lines 328–394): keep ONLY the `api_request(request)` branch (lines 330–343 unchanged) and replace the entire non-`/api` tail (the `wants_html`/`hx_partial`/401-redirect/`error.html` `TemplateResponse`/final JSONResponse, lines 344–394) with a single minimal fallback. Also delete the `_HTML_ERROR_STATUSES` set (line 326). Final handler body:

```python
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException):
        if api_request(request):
            hdrs = getattr(exc, "headers", None)
            if exc.status_code >= 500:
                return api_error(exc.status_code, "internal_error", "internal server error", headers=hdrs)
            return api_error(exc.status_code, code_for(exc.status_code), exc.detail, headers=hdrs)
        # Non-/api: every surviving non-/api route is public (oauth/health) or an
        # SPA path served at 200; none can 401 a browser any more, so the backend
        # never redirects browsers. A rare non-/api exception gets a minimal JSON body.
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None) or {},
        )
```

  - **Collapse `_inactive_account`** (lines 396–416): the `wants_html`/`hx_partial` browser-redirect branch is now dead (no protected non-`/api` HTML route reaches `AccountInactive` — only `/api/*` deps do). Keep ONLY the `/api` 403 envelope + a JSON fallback:

```python
    @app.exception_handler(AccountInactive)
    async def _inactive_account(request: Request, exc: AccountInactive):
        if api_request(request):
            return api_error(403, "account_inactive", "account not active")
        return JSONResponse(status_code=403, content={"detail": "account not active"})
```

  - Remove the Jinja env block (lines 470–481: `Jinja2Templates(...)`, the six `templates.env.filters[...] = ...` lines, and `app.state.templates = templates`).
  - Remove `app.include_router(auth.router)` and `app.include_router(register.router)` (lines 523–524). Keep `health` and `oauth_google` includes.

- [ ] **Step 4: Delete templates + static**

```bash
git rm src/call_manager/web/templates/login.html src/call_manager/web/templates/register.html \
  src/call_manager/web/templates/register_received.html src/call_manager/web/templates/account_pending.html \
  src/call_manager/web/templates/account_no_access.html src/call_manager/web/templates/auth_error.html \
  src/call_manager/web/templates/error.html src/call_manager/web/templates/base.html
git rm src/call_manager/web/static/pico.min.css src/call_manager/web/static/app.css \
  src/call_manager/web/static/app.js src/call_manager/web/static/htmx.min.js \
  src/call_manager/web/static/htmx-sse.js src/call_manager/web/static/dedup.js
```

(Keeps `favicon.svg`. The `/static` mount stays — now serving only the favicon.)

- [ ] **Step 4b: Delete the legacy JS test suite + its CI step (Codex blocker 2).** All four `tests/js/*.test.js` files test the deleted `static/app.js` / `static/dedup.js` (the Jinja-era inline-handler JS) — they hard-fail CI once those files are gone:

```bash
git rm -r tests/js/
```

Then in `.github/workflows/ci.yml`, DELETE the entire `- name: JS unit tests (legacy static app.js)` step (the `run: node --test 'tests/js/**/*.test.js'` step, ~lines 66–72). **Keep the `- name: Set up Node` step** — the frontend pnpm/vitest/OpenAPI-drift steps below it depend on Node.

- [ ] **Step 5: Migrate/delete the broken backend tests.**
  - `git rm tests/test_web_login_password.py tests/test_web_register.py tests/test_web_login_google_button.py` — their behavior is fully covered by `tests/test_api_auth.py` (Tasks 2–5).
  - `git rm tests/test_web_me.py tests/test_web_me_subscriptions.py` — **obsolete Jinja-`/me`-page tests (Codex blocker 3 + verified sibling).** Both target the server-rendered `/me` page (`GET /me` → HTML, `POST /me/name`, `POST /me/subscriptions`) that the account/settings slice already deleted: 5 + 8 of their tests are PRE-EXISTING RED on master (`assert 404 == 200`), and `test_web_me.py::test_static_htmx_is_served_locally` (the only green one) dies when `htmx.min.js` + the Jinja `/login` are deleted. `/me` is now React + `/api/me/*` (covered by `web/api/me.py` + the React `Settings`/`account` tests); the Jinja autoescape guard is moot (React escapes JSX). Deleting already-red obsolete tests regresses nothing.
  - `tests/test_web_auth.py`: DELETE `test_login_form_renders` and `test_login_form_shows_configured_domain` (server no longer renders `/login`; the domain hint is covered by `test_config_reports_domains_and_google_disabled`). REPOINT the infra tests' POST target from `/login` to `/api/auth/login` and switch the form `data=` to `json=`: `test_login_rejects_oversized_email_without_consuming_limiter` (now asserts `r.status_code == 400`), `test_trusted_proxy_reads_x_forwarded_for`, `test_trusted_proxy_picks_rightmost_xff_to_resist_spoofing`, `test_untrusted_proxy_ignores_x_forwarded_for` (these assert limiter `_events` keys, unchanged). Keep `test_api_admin_requires_auth` and `test_api_admin_forbidden_for_non_admin` as-is. Example repoint:

```python
    with TestClient(app) as client:
        r = client.post("/api/auth/login", json={"email": huge, "password": _PW})
    assert r.status_code == 400
    assert huge not in app.state.email_limiter._events
```

  - `tests/test_auth_inactive_enforcement.py`: the four `/me`-as-HTML-route tests are already red (the account/settings slice deleted the server `GET /me` page). DELETE `test_active_user_reaches_me`, `test_pending_user_html_redirects_and_clears_cookie`, `test_disabled_mid_session_reaches_no_access_despite_bumped_epoch`. REPLACE `test_disabled_user_api_gets_403` with an `/api/me`-targeted equivalent (the inactive enforcement now lives on the `/api` path):

```python
def test_inactive_api_caller_gets_403(monkeypatch, pg_url, pg_session_factory):
    """A pending/disabled user hitting an /api route gets the 403 envelope
    (AccountInactive -> api branch). Browser redirect handling is gone — the SPA
    drives it client-side off this 403."""
    app = _app(monkeypatch, pg_url)
    uid = _user(app.state.session_factory, "disabled")
    with TestClient(app) as c:
        c.cookies.set("session", app.state.session_signer.make(user_id=uid))
        r = c.get("/api/me")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "account_inactive"
```

  Keep `test_get_current_user_raises_account_inactive` (pure dependency unit — unaffected). (Adjust `_app`/`_user` helper names to whatever the file already defines; the file's `_app` sets the four env vars and `_user(sf, status)` creates a user.)

  - `tests/test_security_headers.py`: REPOINT both `/login` POSTs to `/api/auth/login`. `test_oversized_body_rejected_with_413` → `client.post("/api/auth/login", content=b"x" * 2000)` still 413 (the body-size middleware runs before routing). `test_normal_body_passes_under_cap` → `client.post("/api/auth/login", json={"email": "bodycap-probe@ki.se", "password": "x" * 16})` and assert `r.status_code == 400` (was 200 — login failure is now a generic 400; the point is it was NOT rejected by the size cap).
  - `tests/test_spa_serving.py`: the two CSP probes `c.get("/login")` (in `test_dev_mode_relaxes_csp` and `test_disabled_mode_keeps_strict_script_src`) only read the CSP header (added by middleware on any response, incl. a 404). Repoint both to `c.get("/healthz")` for clarity now that `/login` 404s server-side.

- [ ] **Step 6: Stale-comment cleanup (frontend).**
  - `frontend/src/api/client.ts` `redirectToLogin()` (lines 19–24): the comment says "the server-rendered /login". Update to reflect `/login` is now React (mechanism unchanged — still a full-page nav):

```ts
// Full-page nav to /login (now a React pre-auth page), preserving where we were
// so the user lands back after signing in (mirrors the backend's 401 next=).
```

  - `frontend/src/api/session.ts` (lines 5–8): drop "server-rendered" from the `/login` comment — "...the full-page nav to /login (React pre-auth) and throws...".

- [ ] **Step 7: Add the teardown guard** — `tests/test_auth_teardown_guard.py`

```python
from fastapi.testclient import TestClient

from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def test_jinja_auth_routes_gone_and_api_auth_present(monkeypatch, pg_url):
    app = _app(monkeypatch, pg_url)
    paths = {getattr(r, "path", None) for r in app.routes}
    # The Jinja page GET routes are gone (SPA_MODE=disabled -> no catch-all here).
    for gone in ("/login", "/register", "/auth/pending", "/auth/no-access", "/auth/error"):
        assert gone not in paths
    # The typed /api surface is present.
    for present in ("/api/auth/config", "/api/auth/login", "/api/auth/register", "/api/auth/logout"):
        assert present in paths
    # The OAuth routes survive.
    assert "/auth/google/start" in paths and "/auth/google/callback" in paths


def test_no_web_jinja_env_or_templates(monkeypatch, pg_url):
    app = _app(monkeypatch, pg_url)
    # The web Jinja env is gone (digest has its own env elsewhere).
    assert not hasattr(app.state, "templates")


def test_deleted_static_assets_absent():
    from pathlib import Path

    import call_manager.main as m

    static = m.STATIC_DIR
    for gone in ("pico.min.css", "app.css", "app.js", "htmx.min.js", "htmx-sse.js", "dedup.js"):
        assert not (static / gone).exists()
    assert (static / "favicon.svg").exists()  # kept
```

- [ ] **Step 8: OpenAPI regen #2 + routes `--check`** (route deletions change the schema):

```bash
EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh
uv run python scripts/gen_routes.py --check   # already regenerated in Task 9; must stay in sync
```

- [ ] **Step 9: Dangling-reference guard, then run the full affected backend + frontend suites**

First, prove nothing still references the deleted assets/modules/tests (this is the gate the plan's earlier grouped run would miss — CI runs everything):

```bash
# Must print NOTHING (grep exits 1 = clean). References to deleted static/templates/tests:
grep -rn "pico.min.css\|htmx.min.js\|htmx-sse.js\|dedup.js\|static/app.js\|static/app.css\|base.html\|tests/js\|web.routes.auth\|web.routes.register\|test_web_me\|test_web_login_password\|test_web_register\|test_web_login_google_button" \
  src tests scripts .github --include=*.py --include=*.js --include=*.yml --include=*.html 2>/dev/null \
  | grep -v "src/call_manager/web/client_ip.py" || echo "CLEAN"
```

Then the suites:

```bash
# Backend — affected files grouped (avoid the whole-suite flake; group to dodge the first-file UndefinedTable flake)
uv run pytest tests/test_api_auth.py tests/test_client_ip.py tests/test_web_auth.py \
  tests/test_web_oauth_google.py tests/test_auth_inactive_enforcement.py \
  tests/test_security_headers.py tests/test_spa_serving.py tests/test_gen_routes.py \
  tests/test_web_me.py tests/test_web_me_subscriptions.py tests/test_auth_teardown_guard.py -q
# (test_web_me*.py are now DELETED -> pytest reports them as "no tests ran" / file-not-found,
#  which confirms the deletion; remove them from the command once confirmed gone.)
# Frontend
cd frontend && pnpm vitest run && pnpm tsc --noEmit && pnpm build && cd ..
# JS legacy suite must be GONE (the CI step was deleted): this must error "no test files".
node --test 'tests/js/**/*.test.js' 2>&1 | tail -2 || echo "tests/js gone (expected)"
# Lint (whole repo, as CI does)
uv run ruff check .
```

Expected: dangling-ref grep prints `CLEAN`; pytest green; frontend green + tsc 0 + build ok; `ruff check .` clean (run `ruff format` on this slice's new/edited files only if `ruff check` flags anything — it checks lint, not format).

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "refactor(spa): delete web Jinja stack, collapse dead redirect branches, migrate auth tests (auth slice)"
```

---

## Self-Review (completed by plan author)

**Spec coverage:** login (Task 3) · register (Task 4) · logout (Task 5) · config-for-pre-auth-pages (Task 2, refinement) · PreAuthLayout + invariant (Task 7) · Login/Register/holding pages (Tasks 7–8) · sign-out relocation to TitleBar + DashboardHeader removal (Task 10) · manifest flip with three `/auth/*` leaves (Task 9, refinement) · `_client_ip` relocation so oauth_google survives (Task 1, refinement) · collapse `_redirect_html_401` + `_inactive_account` (Task 11, folded-in) · delete templates/static/routes + Jinja env (Task 11) · stale-comment cleanup (Task 11) · two OpenAPI regens (Tasks 6, 11) + routes regen (Task 9) · all test migrations coverage-mapped (Tasks 2–5 create `test_api_auth.py` before Task 11 deletes the Jinja test files). The `feat/google-oidc-pkce` stale-branch deletion is optional cleanup, called out in the spec, handled at merge time — not a code task.

**Anti-enumeration / timing parity pins:** `test_login_failures_identical_400_no_cookie` (byte-identical body), `test_login_dummy_hash_invoked_for_unknown_email`, `test_login_rate_limited_folds_into_400` (non-disclosure), `test_register_dummy_hash_on_every_eligible_attempt`, `test_register_duplicate_silent_identical_no_second_row`, and the frontend `Login.test.tsx` generic-message assertion.

**Type consistency:** `LoginIn/LoginOut/RegisterIn/RegisterOut/LogoutOut/AuthConfigOut` (backend) drive the generated TS body/response types consumed by `useLogin/useRegister/useLogout/useAuthConfig`. The `{redirect}` / `{ok}` / `{google_oidc_enabled, allowed_domains}` shapes match across the stack.

**Known transient state:** between Task 9 (manifest flip) and Task 11 (teardown), `/login` is both a Jinja route and manifest-owned; in prod the explicit Jinja route wins (registered before the catch-all) so the React login only goes live once Task 11 deletes the Jinja route — acceptable for incremental tasks; full integration is verified at Task 11 + the final whole-branch review.

**Codex plan review (applied):** (1) `RedirectResponse` orphan at main.py:6 → Task 11 Step 3 now drops it (was a ruff-fail miss); (2) `tests/js/` + the ci.yml `node --test` step depend on the deleted `app.js`/`dedup.js` → Task 11 Step 4b deletes both; (3) `test_web_me.py::test_static_htmx` (the lone green test) dies on htmx deletion → Task 11 Step 5 deletes `test_web_me.py` + `test_web_me_subscriptions.py` (obsolete Jinja-`/me`, otherwise pre-existing-red); (4) register rate-limit gap → Task 4 adds `test_register_rate_limited_silent_no_row`. A Step-9 dangling-reference grep was added to catch any straggler the grouped run would miss (Codex's "passes the grouped gate, fails full CI" concern).
