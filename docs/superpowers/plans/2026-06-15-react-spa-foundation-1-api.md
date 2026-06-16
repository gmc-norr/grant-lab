# React SPA Foundation — Plan 1: Backend `/api` + Error Envelope + Dashboard Read-Model

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a JSON `/api` surface to the existing FastAPI app — a uniform error envelope, `GET /api/me`, a side-effect-free shared dashboard read-model behind `GET /api/calls`, and a discrete `POST /api/me/default-profile` — all testable with no frontend.

**Architecture:** Reuse the existing FastAPI app, session-cookie auth, and DB. New endpoints live under a dedicated `/api` router. The dashboard's read logic is extracted into a pure (read-only) service shared by the existing Jinja route and the new JSON endpoint; the sticky-default **write** stays in the Jinja path and is also exposed as an explicit POST for the SPA. CSRF/body-size rejections are emitted by ASGI middleware *above* FastAPI's exception layer, so the envelope is applied in two places: FastAPI exception handlers (path-scoped to `/api`) **and** the middleware itself (path-aware).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Pydantic v2, pytest + `fastapi.testclient.TestClient`, testcontainers Postgres.

**Scope:** This is Plan **1 of 3** for the foundation. Plan 2 = SPA serving + Vite scaffold + route manifest + `SPA_MODE`. Plan 3 = React app (client, shell, dashboard, `useEventStream` + `/events/ping`, e2e, OpenAPI→TS). `/events/ping` and OpenAPI→TS generation are intentionally **not** here (they need the frontend dir / the SSE consumer).

**Conventions used below:** tests follow the existing pattern — `_app(monkeypatch, pg_url)` sets env (`DATABASE_URL`, `SESSION_SECRET`, `ALLOWED_EMAIL_DOMAINS`, `APP_BASE_URL`) and calls `create_app()`; auth via `TestClient(app)` + `c.cookies.set("session", app.state.session_signer.make(user_id=uid))`; data via `pg_session_factory` (same testcontainer DB as the app). Run a single test with `uv run pytest <path>::<name> -v`. (Whole-suite runs are flaky on the testcontainer under load — verify flagged tests in isolation.)

---

## Rev 2 — review fixes (apply these; from the multi-agent plan review)

- **[HIGH] Task 1 must catch unmatched-route 404s.** A 404 for an unknown `/api/...` path is a `starlette.exceptions.HTTPException`, not `fastapi.HTTPException`, so a handler registered on the subclass won't catch it — the envelope test would fail. FIX: register the existing `_redirect_html_401` handler on the Starlette base class so it catches both raised `fastapi.HTTPException`s and unmatched-route 404s:
  ```python
  from starlette.exceptions import HTTPException as StarletteHTTPException
  @app.exception_handler(StarletteHTTPException)   # was: HTTPException (fastapi)
  ```
  Keep the `if api_request(request): return api_error(...)` branch at the top. (`test_non_api_404_is_unchanged` still passes: a JSON-Accept 404 falls through to `{"detail": ...}`.)
- **[HIGH] Task 1 sanitized-500 must not alter non-`/api` 500s.** The `@app.exception_handler(Exception)` handler must NOT `raise exc` for non-`/api` (that yields an EMPTY body under `raise_server_exceptions=False`, and is the "global catch-all" the spec warned against). FIX — return Starlette's normal plain 500 for non-`/api`:
  ```python
  from starlette.responses import PlainTextResponse
  @app.exception_handler(Exception)
  async def _api_500(request: Request, exc: Exception):
      if api_request(request):
          return api_error(500, "internal_error", "internal server error")
      return PlainTextResponse("Internal Server Error", status_code=500)
  ```
  Add tests: an `/api` route that raises → envelope 500; a non-`/api` raise → 500 body `Internal Server Error`.
- **[MED] Task 4 must not break the live `/calls/ranking-status`.** `ranking_status` (calls.py:301) uses `_ranking_counts` from `calls.py`; moving it to `dashboard.py` breaks that endpoint. FIX: keep `_ranking_counts` public in `dashboard.py` and update `ranking_status` to `from call_manager.web.dashboard import _ranking_counts` (or leave a one-line re-export in `calls.py`); confirm the ranking-strip tests pass.
- **[MED] Task 4 parity tests must cover ordering + hide**, not just the row set: assert the closed-first tie-break ordering and the dismissed / `show_closed` hide logic match the Jinja route.
- **[LOW] Task 1/2 add the remaining envelope cases:** `AccountInactive` → 403 envelope on `/api`, and `RequestValidationError` → 422 envelope (e.g. `POST /api/me/default-profile` with a non-int `profile_id`).

## Rev 3 — supply-chain hardening: BACKEND (per the 2026-06-16 supply-chain spec)

The [supply-chain spec](../specs/2026-06-16-supply-chain-security-design.md) hardens BOTH ecosystems; the **Python/backend** half folds in here (Plan 1 is the first backend plan, so it sets the baseline before more deps arrive). Authoritative detail: that spec §3.2 / §5 / §7. **These are now executable steps in [Task 0](#task-0-backend-supply-chain-baseline-frozen-install--cooldown--detection) — apply that task first.** Summary:

- **Frozen, hashed Python installs:** CI uses **`uv sync --frozen`** (not a resolving sync); `uv.lock` hashes stay authoritative + committed.
- **Cooldown + wheels-preferred:** add a new **`[tool.uv]`** block to `pyproject.toml` — `exclude-newer = "<now − ~1–2 weeks, ISO-8601>"` (bump deliberately on intentional updates) and wheels-preferred resolution to avoid sdist build-backend code execution at install (confirm the exact `only-binary`/no-build key for the pinned uv version; keep build isolation on).
- **Python detection (backstop to the cooldown):** `.github/dependabot.yml` with the Python ecosystem entry (confirm the uv identifier) + the **Dependency Review** action on PRs; add **`pip-audit`** and **OSV-Scanner** (over `uv.lock`) as CI steps.
- **Dependency-add review rule:** any PR moving `uv.lock` documents why + download/age/maintainer sanity + whether it ships a build backend (spec §2).
- Plan 1 adds no new Python deps, so this purely establishes the backend supply-chain baseline.

## Rev 4 — task-body reconciliation (Codex)

Folded the Rev 2 / Rev 3 header decisions **into the canonical task bodies** (an implementation agent follows the bodies, not the header notes): (1) **Task 1 Step 4** now re-registers the handler on the **Starlette base `HTTPException`** (catches unmatched-route 404s) and the generic-500 handler **returns `PlainTextResponse("Internal Server Error", 500)` for non-`/api`** instead of `raise exc` (which yields an empty body and is the global catch-all the spec warns against); Task 1 Step 1 gains the two 500 tests. (2) The Rev 3 backend supply-chain notes became executable **[Task 0](#task-0-backend-supply-chain-baseline-frozen-install--cooldown--detection)** — `uv sync --frozen`, a `[tool.uv]` `exclude-newer` + wheels-preferred block in `pyproject.toml`, and `pip-audit`/OSV-Scanner + `.github/dependabot.yml` in CI. The bodies are authoritative; the Rev sections remain as history.

## File structure

- Create `src/call_manager/web/api/__init__.py` — assembles the `/api` `APIRouter` (includes the sub-routers).
- Create `src/call_manager/web/api/errors.py` — `ApiError` model + `api_error()` JSONResponse helper + `is_api_path()`.
- Create `src/call_manager/web/api/me.py` — `GET /api/me`, `POST /api/me/default-profile`.
- Create `src/call_manager/web/api/calls.py` — `GET /api/calls`.
- Create `src/call_manager/web/dashboard.py` — shared read-only read-model service + selection resolution + default-profile write helper.
- Modify `src/call_manager/main.py` — mount the `/api` router; re-register the existing handler on the **Starlette base `HTTPException`** (so unmatched-route 404s get the envelope) and add `/api`-scoped branches (Starlette HTTPException, RequestValidationError, AccountInactive, generic 500 returning a plain non-`/api` 500 — never `raise exc`).
- Modify `src/call_manager/auth/csrf.py` — `_forbidden` emits the JSON envelope for `/api` paths.
- Modify `src/call_manager/web/middleware.py` — `MaxBodySizeMiddleware` emits the JSON envelope for `/api` paths.
- Modify `src/call_manager/web/routes/calls.py` — `calls_index` keeps its sticky-default **write**, then delegates the read to `dashboard.build_dashboard_view`.
- Tests: `tests/test_api_errors.py`, `tests/test_api_me.py`, `tests/test_dashboard_readmodel.py`, `tests/test_api_calls.py`.

---

## Task 0: Backend supply-chain baseline (frozen install + cooldown + detection)

Plan 1 is the first backend plan, so it sets the Python supply-chain baseline *before* more deps arrive (per the [supply-chain spec](../specs/2026-06-16-supply-chain-security-design.md) §3.2 / §5 / §7 / §8). These are **executable changes to real files**, not notes. Plan 1 adds no new Python deps; this purely establishes the baseline. **Apply this task first.**

**Files:**
- Modify: `pyproject.toml` (add a new `[tool.uv]` block — none exists today)
- Modify: `.github/workflows/ci.yml` (`uv sync --all-extras` → frozen; add detection steps)
- Create: `.github/dependabot.yml`

- [ ] **Step 1: Make the backend install frozen + add cooldown/wheels-preferred to `pyproject.toml`**

Append a new `[tool.uv]` block to `pyproject.toml` (after `[tool.hatch.build.targets.wheel]`). Set `exclude-newer` to "now minus ~1–2 weeks" as an ISO-8601 UTC timestamp (bump it **deliberately** when intentionally updating deps). Prefer wheels so an sdist build backend never runs arbitrary code at install (Surface A); keep build isolation on (uv default).

```toml
[tool.uv]
# Cooldown: only consider artifacts published before this instant. Bump
# deliberately on intentional dependency updates (supply-chain spec §3.2).
exclude-newer = "2026-06-02T00:00:00Z"
# Wheels-preferred: avoid executing an sdist build backend at install time.
# Confirm the exact key for the pinned uv version (0.4.x): use
# `no-build = true` if supported, otherwise the equivalent only-binary
# resolution setting. Keep a small allowlist if a dep is legitimately
# source-only.
no-build = true
```

Verify the lockfile is still satisfiable under these constraints and stays authoritative (do NOT regenerate it loosely — this is a check, not a resolving sync):

Run: `uv sync --frozen` (and `uv lock --check` if available for the pinned uv version)
Expected: the environment installs from the committed `uv.lock` with hashes verified, honoring `exclude-newer`. If `no-build`/only-binary rejects a source-only dep, add that one dep to an explicit allowlist (do not drop the global wheels-preferred default).

- [ ] **Step 2: Switch CI to a frozen install + add detection (`.github/workflows/ci.yml`)**

Replace the resolving `uv sync --all-extras` step with a **frozen** sync, and add `pip-audit` and **OSV-Scanner** (over `uv.lock`) as CI steps. Current CI (ci.yml:22-23) runs `uv sync --all-extras`; change it to:

```yaml
      - name: Sync deps (frozen)
        run: uv sync --all-extras --frozen
```

Add detection steps after `Pytest` (and before the Node steps):

```yaml
      - name: pip-audit (Python deps)
        run: uv run --with pip-audit pip-audit

      - name: OSV-Scanner (uv.lock)
        uses: google/osv-scanner-action@v1
        with:
          scan-args: "--lockfile=uv.lock"
```

(OSV-Scanner over `pnpm-lock.yaml` and the Socket CLI are added by the supply-chain plan once `frontend/` exists; Plan 1 covers only the Python lockfile.)

- [ ] **Step 3: Add Dependabot for the Python ecosystem (`.github/dependabot.yml`)**

Create `.github/dependabot.yml` with the Python ecosystem entry (Tier-1 backstop to the cooldown). Confirm the exact uv ecosystem identifier for the Dependabot version in use; if uv is not yet a first-class ecosystem, use the `pip` ecosystem pointed at the project so advisories still surface:

```yaml
version: 2
updates:
  - package-ecosystem: "uv"   # confirm identifier; fall back to "pip" if uv unsupported
    directory: "/"
    schedule:
      interval: "weekly"
```

Also enable the **Dependency Review** action on PRs (a separate workflow or a job step) so a PR introducing a known-vulnerable dependency fails. (The `npm`/`pnpm` Dependabot entry is added later by the supply-chain plan once `frontend/` exists.)

- [ ] **Step 4: Document the dependency-add review rule**

Add a one-line note to the PR template / contributing docs (or this plan's commit message) that any PR moving `uv.lock` must document **why + download/age/maintainer sanity + whether it ships a build backend** (supply-chain spec §2). No new Python deps are added in Plan 1, so this is purely documentary here.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .github/workflows/ci.yml .github/dependabot.yml
git commit -m "chore(supply-chain): frozen uv sync + exclude-newer/wheels-preferred + pip-audit/OSV/Dependabot"
```

---

## Task 1: `/api` router + error envelope (FastAPI exception handlers)

**Files:**
- Create: `src/call_manager/web/api/errors.py`, `src/call_manager/web/api/__init__.py`
- Modify: `src/call_manager/main.py` (mount router ~after other routers; **re-register** the existing handler at main.py:317 on the Starlette base `HTTPException` and add the `/api` branches — see Step 4)
- Test: `tests/test_api_errors.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_errors.py
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


def test_api_unknown_path_returns_envelope_404(monkeypatch, pg_url, pg_session_factory):
    c = TestClient(_app(monkeypatch, pg_url))
    r = c.get("/api/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert set(body["error"]) >= {"code", "message"}
    assert body["error"]["code"] == "not_found"


def test_non_api_404_is_unchanged(monkeypatch, pg_url, pg_session_factory):
    c = TestClient(_app(monkeypatch, pg_url))
    r = c.get("/totally-unknown-html-path", headers={"accept": "application/json"})
    # Non-/api path must NOT get the new envelope shape.
    assert r.status_code == 404
    assert "error" not in r.json()


def test_api_500_is_sanitized_envelope(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)

    @app.get("/api/_boom")
    def _boom():
        raise RuntimeError("secret internals")

    # raise_server_exceptions=False so the generic Exception handler runs.
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/api/_boom")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal_error"
    # Must NOT leak the raised message.
    assert "secret internals" not in r.text


def test_non_api_500_is_plain_text(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)

    @app.get("/_boom")
    def _boom():
        raise RuntimeError("secret internals")

    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/_boom")
    # Non-/api: Starlette's normal plain 500 with a NON-empty body — not the
    # envelope, and not the empty body that `raise exc` would produce here.
    assert r.status_code == 500
    assert r.text == "Internal Server Error"
    assert "error" not in r.headers.get("content-type", "")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_api_errors.py -v`
Expected: FAIL — `/api/does-not-exist` currently returns `{"detail": ...}`, so `body["error"]` raises `KeyError`.

- [ ] **Step 3: Create the envelope helper**

```python
# src/call_manager/web/api/errors.py
from __future__ import annotations

from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request


class ApiErrorBody(BaseModel):
    code: str
    message: str
    detail: object | None = None


class ApiError(BaseModel):
    error: ApiErrorBody


def is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def api_error(
    status_code: int, code: str, message: str, detail: object | None = None
) -> JSONResponse:
    body = ApiError(error=ApiErrorBody(code=code, message=message, detail=detail))
    return JSONResponse(status_code=status_code, content=body.model_dump(exclude_none=True))


# Map common HTTP statuses to stable string codes for the envelope.
STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    500: "internal_error",
}


def code_for(status_code: int) -> str:
    return STATUS_CODES.get(status_code, "error")


def api_request(request: Request) -> bool:
    return is_api_path(request.url.path)
```

```python
# src/call_manager/web/api/__init__.py
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api")
```

- [ ] **Step 4: Branch the exception handlers for `/api`**

In `src/call_manager/main.py`, add the imports near the other imports. Note the **Starlette base** `HTTPException` import — it is required (see below):

```python
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import PlainTextResponse

from call_manager.web.api.errors import api_error, api_request, code_for
```

**Re-register the existing `_redirect_html_401` handler on the Starlette base `HTTPException`** (not the `fastapi.HTTPException` subclass). main.py:317 currently does `@app.exception_handler(HTTPException)` where `HTTPException` is imported from `fastapi` (main.py:5) — a handler bound to the FastAPI subclass does **not** catch unmatched-route 404s, because FastAPI raises those as `starlette.exceptions.HTTPException` (the base class). Registering on the base class catches **both** raised `fastapi.HTTPException`s *and* unmatched-route 404s, so `GET /api/does-not-exist` reaches this handler and gets the envelope. Change the decorator:

```python
    @app.exception_handler(StarletteHTTPException)   # was: HTTPException (fastapi)
    async def _redirect_html_401(request: Request, exc: StarletteHTTPException):
        # /api paths get the uniform JSON envelope BEFORE any wants_html logic.
        if api_request(request):
            return api_error(exc.status_code, code_for(exc.status_code), exc.detail)
        # ... existing wants_html / hx_partial / styled-error-page logic unchanged ...
```

The `if api_request(request): return api_error(...)` branch sits at the **top** of the body, before the existing `wants_html` logic; everything below it is unchanged. (`test_non_api_404_is_unchanged` still passes: a JSON-Accept 404 on a non-`/api` path falls through to `{"detail": ...}`.)

Add the `RequestValidationError` and generic-`Exception` handlers right after the `AccountInactive` handler (main.py:384):

```python
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def _api_validation(request: Request, exc: RequestValidationError):
        if api_request(request):
            return api_error(422, "validation_error", "request validation failed", exc.errors())
        # Non-/api: preserve FastAPI's default 422 shape.
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    @app.exception_handler(Exception)
    async def _api_500(request: Request, exc: Exception):
        if api_request(request):
            # Sanitized — never leak internals to the client.
            return api_error(500, "internal_error", "internal server error")
        # Non-/api: return Starlette's normal plain-text 500. Do NOT `raise exc`
        # here — under TestClient(raise_server_exceptions=False) re-raising yields
        # an EMPTY 500 body, and a global re-raise is the "global catch-all" the
        # spec warns against (it would also reshape HTML routes' 500s).
        return PlainTextResponse("Internal Server Error", status_code=500)
```

Also branch the `AccountInactive` handler (main.py:366) at the top of its body:

```python
        if api_request(request):
            return api_error(403, "account_inactive", "account not active")
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_api_errors.py -v`
Expected: PASS — the unmatched-route 404 envelope, the unchanged non-`/api` 404, the sanitized `/api` 500 envelope, and the plain non-`/api` 500 (`Internal Server Error`).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/api/errors.py src/call_manager/web/api/__init__.py src/call_manager/main.py tests/test_api_errors.py
git commit -m "feat(api): /api error envelope via Starlette-base + path-scoped handlers"
```

---

## Task 2: CSRF + body-size rejections emit the envelope for `/api`

**Files:**
- Modify: `src/call_manager/auth/csrf.py` (`_forbidden` at csrf.py:118; call sites at 102/106/111)
- Modify: `src/call_manager/web/middleware.py` (`MaxBodySizeMiddleware.__call__` at :107; `_send_status` at :140)
- Test: `tests/test_api_errors.py` (extend)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_api_errors.py  (append)
def test_api_csrf_rejection_is_envelope(monkeypatch, pg_url, pg_session_factory):
    c = TestClient(_app(monkeypatch, pg_url))
    # A cross-origin POST to an /api path: bad Origin → CSRF 403.
    r = c.post("/api/me/default-profile", headers={"origin": "http://evil.example"}, json={})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_api_body_too_large_is_envelope(monkeypatch, pg_url, pg_session_factory):
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "16")
    c = TestClient(_app(monkeypatch, pg_url))
    r = c.post(
        "/api/me/default-profile",
        headers={"origin": "http://testserver", "content-type": "application/json"},
        content=b"x" * 64,
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_api_errors.py -k "csrf or body_too_large" -v`
Expected: FAIL — current responses are `text/plain` (`b"origin mismatch"` / `b"request body too large"`), so `.json()` raises.

- [ ] **Step 3: Make CSRF path-aware**

In `src/call_manager/auth/csrf.py`, replace the three `return _forbidden(...)` call sites in `dispatch` (lines ~102/106/111) with path-aware variants and update `_forbidden`:

```python
import json
# ... in dispatch, each site becomes e.g.:
            if _hostport(origin) != self._expected_host:
                return _forbidden("origin", origin, path=request.url.path)
# (and the origin=="null" and referer sites likewise pass path=request.url.path)
```

```python
def _forbidden(header: str, value: str, *, path: str = "") -> Response:
    _logger.warning(
        "csrf.origin_mismatch",
        extra={"event": "csrf_origin_mismatch", "header": header, "value": value},
    )
    if path == "/api" or path.startswith("/api/"):
        body = json.dumps({"error": {"code": "forbidden", "message": "origin mismatch"}})
        return Response(status_code=403, content=body, media_type="application/json")
    return Response(status_code=403, content=b"origin mismatch")
```

- [ ] **Step 4: Make body-size path-aware**

In `src/call_manager/web/middleware.py`, in `MaxBodySizeMiddleware.__call__`, compute the path once and pass an `as_json` flag through to a JSON-capable sender:

```python
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw_path = scope.get("path", "")
        as_json = raw_path == "/api" or raw_path.startswith("/api/")

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    await _send_error(send, 400, "bad_request", "malformed content-length", as_json)
                    return
                if declared > self.max_bytes:
                    await _send_error(send, 413, "payload_too_large", "request body too large", as_json)
                    return
                break
        # ... (limited_receive unchanged) ...
```

Add `_send_error` (keep the existing `_send_status` for the non-JSON path):

```python
async def _send_error(send: Send, status: int, code: str, message: str, as_json: bool) -> None:
    if as_json:
        import json as _json
        body = _json.dumps({"error": {"code": code, "message": message}}).encode()
        ctype = b"application/json"
    else:
        body = message.encode()
        ctype = b"text/plain; charset=utf-8"
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", ctype), (b"content-length", str(len(body)).encode("ascii"))],
    })
    await send({"type": "http.response.body", "body": body})
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_api_errors.py -v`
Expected: PASS (all four tests). Body-size test depends on `MaxBodySizeMiddleware` being active (it is when `MAX_REQUEST_BODY_BYTES > 0`).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/auth/csrf.py src/call_manager/web/middleware.py tests/test_api_errors.py
git commit -m "feat(api): CSRF + body-size rejections use the JSON envelope on /api"
```

---

## Task 3: `GET /api/me`

**Files:**
- Create: `src/call_manager/web/api/me.py`
- Modify: `src/call_manager/web/api/__init__.py` (include the sub-router); `src/call_manager/main.py` (mount `/api` router if not yet)
- Test: `tests/test_api_me.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_me.py
from fastapi.testclient import TestClient
from call_manager.db.models import User
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def _login(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="u@ki.se")
        s.add(u); s.commit(); uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid


def test_me_unauthenticated_is_envelope_401(monkeypatch, pg_url, pg_session_factory):
    c = TestClient(_app(monkeypatch, pg_url))
    r = c.get("/api/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_me_returns_user(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid = _login(app, pg_session_factory)
    r = c.get("/api/me")
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["id"] == uid
    assert body["user"]["email"] == "u@ki.se"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_api_me.py -v`
Expected: FAIL — `/api/me` not mounted (404).

- [ ] **Step 3: Implement `/api/me`**

```python
# src/call_manager/web/api/me.py
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import User

router = APIRouter()


class UserOut(BaseModel):
    id: int
    email: str
    status: str
    default_profile_id: int | None


class MeOut(BaseModel):
    user: UserOut
    capabilities: dict[str, bool]


@router.get("/me", response_model=MeOut)
def get_me(request: Request, user: User = Depends(get_current_user)):  # noqa: B008
    return MeOut(
        user=UserOut(
            id=user.id, email=user.email, status=user.status,
            default_profile_id=user.default_profile_id,
        ),
        capabilities={"admin": getattr(user, "is_admin", False)},
    )
```

Wire it up in `src/call_manager/web/api/__init__.py`:

```python
from call_manager.web.api import me as _me

router.include_router(_me.router)
```

In `src/call_manager/main.py`, mount the api router alongside the existing `app.include_router(...)` calls:

```python
from call_manager.web.api import router as api_router
app.include_router(api_router)
```

(If `User` has no `is_admin`, replace the capability with a real attribute or drop it — confirm against `db/models.py:35`.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_api_me.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/api/me.py src/call_manager/web/api/__init__.py src/call_manager/main.py tests/test_api_me.py
git commit -m "feat(api): GET /api/me (envelope 401 when unauthenticated)"
```

---

## Task 4: Shared dashboard read-model service (read-only) + Jinja refactor

**Files:**
- Create: `src/call_manager/web/dashboard.py`
- Modify: `src/call_manager/web/routes/calls.py` (`calls_index` at :60-265; keep the sticky-default write at :96-122, delegate the read)
- Test: `tests/test_dashboard_readmodel.py`

The read logic in `calls.py:124-247` (query branches, summary, sources/profiles, pagination) plus `_ranking_counts` (`calls.py:268-296`) and the selection resolution (`calls.py:71-134`) move into the service. The **write** (`calls.py:96-122`, persisting `default_profile_id`) stays in `calls_index` so the live Jinja UI is unchanged.

> **Don't break the live `/calls/ranking-status` endpoint.** `ranking_status` (`calls.py:301`) calls the local `_ranking_counts` (`calls.py:268`); moving `_ranking_counts` into `dashboard.py` would leave `ranking_status` referencing a now-missing name. After the move, keep `_ranking_counts` **public** in `dashboard.py` and update `ranking_status` to `from call_manager.web.dashboard import _ranking_counts` (or leave a one-line re-export in `calls.py`). Step 6 below regression-tests this endpoint.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dashboard_readmodel.py
from call_manager.db.models import User
from call_manager.web.dashboard import build_dashboard_view, resolve_profile_selection


def _seed(pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="u@ki.se")
        s.add(u); s.commit()
        return u.id


def test_resolve_readonly_does_not_persist(pg_session_factory):
    uid = _seed(pg_session_factory)
    with pg_session_factory() as s:
        # An explicit id, but persist=False (the /api read path) must NOT write.
        resolve_profile_selection(s, user_id=uid, raw_profile="999999", persist=False)
        s.commit()
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id is None


def test_build_view_shape(pg_session_factory):
    uid = _seed(pg_session_factory)
    with pg_session_factory() as s:
        view = build_dashboard_view(
            s, user_id=uid, source=None, sort="newest",
            profile_id=None, limit=50, page=1, show_closed=False,
        )
    assert view.ranking == {"pending": 0, "failed": 0}
    assert view.pagination["page"] == 1
    assert isinstance(view.calls, list)
    assert "open_count" in view.summary
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_dashboard_readmodel.py -v`
Expected: FAIL — `call_manager.web.dashboard` does not exist.

- [ ] **Step 3: Implement the service**

```python
# src/call_manager/web/dashboard.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, joinedload

from call_manager.db.models import (
    Call, CallRanking, Profile, RankingJob, RankingJobStatus, Source, User, UserCallDismissal,
)

Sort = Literal["newest", "deadline"]


@dataclass
class DashboardView:
    calls: list[tuple[Call, CallRanking | None]]
    sources: list[Source]
    profiles: list[Profile]
    summary: dict[str, int]
    pagination: dict[str, object]
    ranking: dict[str, int]               # {"pending": int, "failed": int}
    effective: dict[str, object]          # resolved source/sort/profile_id/show_closed
    now: datetime


def resolve_profile_selection(
    session: Session, *, user_id: int, raw_profile: str | None, persist: bool
) -> int | None:
    """Resolve ?profile into a valid active profile id.

    persist=True (Jinja path): mirrors the legacy three-way sticky-default WRITE
    (absent→stored default; ""→clear; valid id→store). persist=False (/api path):
    READ-ONLY — absent→stored default (no write); ""→None; id→validate only.
    """
    if raw_profile is None:
        stored = session.scalar(select(User.default_profile_id).where(User.id == user_id))
        if stored is None:
            return None
        return session.scalar(
            select(Profile.id).where(Profile.id == stored, Profile.is_active.is_(True))
        )
    raw = raw_profile.strip()
    if raw == "":
        if persist:
            u = session.get(User, user_id)
            if u is not None and u.default_profile_id is not None:
                u.default_profile_id = None
        return None
    try:
        requested = int(raw)
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"invalid profile id: {raw!r}") from e
    valid = session.scalar(
        select(Profile.id).where(Profile.id == requested, Profile.is_active.is_(True))
    )
    if valid is not None and persist:
        u = session.get(User, user_id)
        if u is not None and u.default_profile_id != valid:
            u.default_profile_id = valid
    return valid


def set_default_profile(session: Session, *, user_id: int, profile_id: int | None) -> None:
    """Explicit preference write for POST /api/me/default-profile. None clears."""
    u = session.get(User, user_id)
    if u is None:
        return
    if profile_id is None:
        u.default_profile_id = None
        return
    valid = session.scalar(
        select(Profile.id).where(Profile.id == profile_id, Profile.is_active.is_(True))
    )
    if valid is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="profile not found or inactive")
    u.default_profile_id = valid


def _order_for(sort: Sort):
    if sort == "deadline":
        return (Call.deadline.asc().nullslast(), Call.id.desc())
    return (Call.published_at.desc().nullslast(), Call.id.desc())


def _ranking_counts(session: Session, profile_id: int) -> tuple[int, int]:
    pending = session.scalar(
        select(func.count()).select_from(RankingJob).where(
            RankingJob.profile_id == profile_id,
            RankingJob.status.in_((RankingJobStatus.PENDING, RankingJobStatus.RUNNING)),
        )
    ) or 0
    scored = select(CallRanking.call_id).where(
        CallRanking.profile_id == profile_id, CallRanking.call_id == RankingJob.call_id
    )
    failed = session.scalar(
        select(func.count()).select_from(RankingJob).where(
            RankingJob.profile_id == profile_id,
            RankingJob.status == RankingJobStatus.FAILED,
            ~scored.exists(),
        )
    ) or 0
    return pending, failed


def build_dashboard_view(
    session: Session, *, user_id: int, source: str | None, sort: Sort,
    profile_id: int | None, limit: int, page: int, show_closed: bool,
) -> DashboardView:
    now = datetime.now(UTC)
    soon_cutoff = now + timedelta(days=30)
    closed_expr = and_(Call.deadline.isnot(None), Call.deadline <= now)
    open_expr = or_(Call.deadline.is_(None), Call.deadline > now)
    offset = (page - 1) * limit

    source_id: int | None = None
    selected_source = source
    if source:
        source_id = session.scalar(select(Source.id).where(Source.slug == source))
        if source_id is None:
            selected_source = None

    profile_obj = None
    if profile_id is not None:
        profile_obj = session.execute(
            select(Profile).where(Profile.id == profile_id, Profile.is_active.is_(True))
        ).scalar_one_or_none()
        if profile_obj is None:
            profile_id = None

    if profile_id is not None:
        stmt = (
            select(Call, CallRanking).options(joinedload(Call.source)).outerjoin(
                CallRanking,
                (CallRanking.call_id == Call.id) & (CallRanking.profile_id == profile_id),
            ).order_by(
                closed_expr.asc(),
                CallRanking.aggregate_score.desc().nullslast(),
                CallRanking.embed_similarity.desc().nullslast(),
                Call.published_at.desc().nullslast(), Call.id.desc(),
            ).limit(limit).offset(offset)
        )
    else:
        stmt = (
            select(Call, None).options(joinedload(Call.source))
            .order_by(closed_expr.asc(), *_order_for(sort)).limit(limit).offset(offset)
        )

    dismissed_subq = (
        select(UserCallDismissal.call_id)
        .where(UserCallDismissal.user_id == user_id).scalar_subquery()
    )
    base_filters = [Call.id.notin_(dismissed_subq)]
    if source_id is not None:
        base_filters.append(Call.source_id == source_id)
    list_filters = list(base_filters)
    if not show_closed:
        list_filters.append(open_expr)
    rows = session.execute(stmt.where(*list_filters)).unique().all()
    calls = [(row[0], row[1] if profile_id is not None else None) for row in rows]

    list_total = session.scalar(
        select(func.count()).select_from(Call).where(*list_filters)
    ) or 0
    open_count = session.scalar(
        select(func.count()).select_from(Call).where(*base_filters, open_expr)
    ) or 0
    soon_deadline = session.scalar(
        select(func.count()).select_from(Call).where(
            *base_filters, Call.deadline.isnot(None),
            Call.deadline > now, Call.deadline <= soon_cutoff,
        )
    ) or 0
    high_score = 0
    if profile_id is not None:
        high_score = session.scalar(
            select(func.count()).select_from(CallRanking)
            .join(Call, Call.id == CallRanking.call_id)
            .where(*base_filters, open_expr, CallRanking.profile_id == profile_id,
                   CallRanking.aggregate_score >= 70)
        ) or 0

    sources = session.execute(select(Source).order_by(Source.name)).scalars().all()
    profiles = session.execute(
        select(Profile).where(Profile.is_active.is_(True)).order_by(Profile.name)
    ).scalars().all()

    pending, failed = (_ranking_counts(session, profile_id) if profile_id is not None else (0, 0))

    return DashboardView(
        calls=calls, sources=sources, profiles=profiles,
        summary={"open_count": open_count, "high_score": high_score, "soon_deadline": soon_deadline},
        pagination={"page": page, "limit": limit, "has_prev": page > 1,
                    "has_next": offset + len(calls) < list_total, "total": list_total},
        ranking={"pending": pending, "failed": failed},
        effective={"source": selected_source, "sort": sort,
                   "profile_id": profile_id, "show_closed": show_closed},
        now=now,
    )
```

- [ ] **Step 4: Run the service tests to verify they pass**

Run: `uv run pytest tests/test_dashboard_readmodel.py -v`
Expected: PASS.

- [ ] **Step 5: Refactor `calls_index` to use the service (keep its write)**

In `src/call_manager/web/routes/calls.py`, replace the body of `calls_index` (lines ~71-265) so it (a) resolves selection **with `persist=True`** inside one session (preserving the sticky-default write), (b) calls `build_dashboard_view`, (c) renders the template from the view. Keep the same template context keys the template expects:

```python
    from call_manager.web.dashboard import build_dashboard_view, resolve_profile_selection
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        profile_id = resolve_profile_selection(
            session, user_id=user.id, raw_profile=profile, persist=True
        )
        session.commit()  # persist any sticky-default change
        view = build_dashboard_view(
            session, user_id=user.id, source=source, sort=sort,
            profile_id=profile_id, limit=limit, page=page, show_closed=show_closed,
        )
        session.expunge_all()
    return request.app.state.templates.TemplateResponse(
        request=request, name="calls_index.html",
        context={
            "calls_with_rankings": view.calls, "sources": view.sources,
            "profiles": view.profiles, "selected_source": view.effective["source"],
            "selected_sort": view.effective["sort"],
            "selected_profile_id": view.effective["profile_id"],
            "show_closed": view.effective["show_closed"], "summary": view.summary,
            "pagination": view.pagination, "now": view.now, "user": user,
        },
    )
```

- [ ] **Step 6: Verify the existing dashboard tests still pass (parity)**

Run: `uv run pytest tests/test_web_calls.py tests/test_web_calls_sticky_default.py tests/test_web_calls_with_score.py -v`
Expected: PASS — sticky-default behavior and rendering unchanged. **Parity must cover, not just the row set:** (a) the closed-first tie-break **ordering** and the dismissed / `show_closed` **hide** logic match the Jinja route; (b) the live **`GET /calls/ranking-status`** endpoint still returns the `{pending, failed}` strip after `_ranking_counts` moved to `dashboard.py` (this is the regression guard for the import/re-export above — if its test isn't already in `tests/test_web_calls*.py`, add one hitting `/calls/ranking-status`).

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/web/dashboard.py src/call_manager/web/routes/calls.py tests/test_dashboard_readmodel.py
git commit -m "refactor(dashboard): extract read-only read-model service; Jinja keeps the sticky-default write"
```

---

## Task 5: `GET /api/calls` (read-only) + `POST /api/me/default-profile`

**Files:**
- Create: `src/call_manager/web/api/calls.py`
- Modify: `src/call_manager/web/api/me.py` (add the POST), `src/call_manager/web/api/__init__.py` (include calls router)
- Test: `tests/test_api_calls.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_api_calls.py
from fastapi.testclient import TestClient
from call_manager.db.models import Profile, ProfileKind, User
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def _login(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="u@ki.se"); s.add(u); s.flush()
        p = Profile(name="Grp", kind=ProfileKind.ORGANIZATIONAL_UNIT.value, level="group",
                    description="d", structured={}, created_by=u.id)
        s.add(p); s.commit(); uid, pid = u.id, p.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid, pid


def test_api_calls_is_read_only(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _login(app, pg_session_factory)
    r = c.get(f"/api/calls?profile={pid}")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and "summary" in body and body["ranking"] == {"pending": 0, "failed": 0}
    # Deep-link GET must NOT persist the default.
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id is None


def test_post_default_profile_persists(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _login(app, pg_session_factory)
    r = c.post("/api/me/default-profile",
               headers={"origin": "http://testserver"}, json={"profile_id": pid})
    assert r.status_code == 200
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id == pid
    # Clearing
    c.post("/api/me/default-profile", headers={"origin": "http://testserver"},
           json={"profile_id": None})
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_api_calls.py -v`
Expected: FAIL — endpoints not mounted (404).

- [ ] **Step 3: Implement `GET /api/calls`**

```python
# src/call_manager/web/api/calls.py
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import User
from call_manager.web.dashboard import build_dashboard_view, resolve_profile_selection

router = APIRouter()


class CallOut(BaseModel):
    id: int
    title: str
    deadline: str | None
    published_at: str | None
    source_name: str | None
    source_slug: str | None
    aggregate_score: float | None


class DashboardOut(BaseModel):
    items: list[CallOut]
    summary: dict[str, int]
    pagination: dict[str, object]
    ranking: dict[str, int]
    effective: dict[str, object]
    facets: dict[str, list[dict]]


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None


@router.get("/calls", response_model=DashboardOut)
def get_calls(
    request: Request,
    source: str | None = Query(default=None),
    sort: Literal["newest", "deadline"] = Query(default="newest"),
    profile: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    page: int = Query(default=1, ge=1),
    show_closed: bool = Query(default=False),
    user: User = Depends(get_current_user),  # noqa: B008
):
    sf = request.app.state.session_factory
    with sf() as session:
        # READ-ONLY: persist=False — a deep link never changes the stored default.
        profile_id = resolve_profile_selection(
            session, user_id=user.id, raw_profile=profile, persist=False
        )
        view = build_dashboard_view(
            session, user_id=user.id, source=source, sort=sort,
            profile_id=profile_id, limit=limit, page=page, show_closed=show_closed,
        )
        items = [
            CallOut(
                id=call.id, title=call.title, deadline=_iso(call.deadline),
                published_at=_iso(call.published_at),
                source_name=(call.source.name if call.source else None),
                source_slug=(call.source.slug if call.source else None),
                aggregate_score=(ranking.aggregate_score if ranking else None),
            )
            for call, ranking in view.calls
        ]
        facets = {
            "sources": [{"slug": s.slug, "name": s.name} for s in view.sources],
            "profiles": [{"id": p.id, "name": p.name} for p in view.profiles],
        }
    return DashboardOut(
        items=items, summary=view.summary, pagination=view.pagination,
        ranking=view.ranking, effective=view.effective, facets=facets,
    )
```

(`CallOut` fields are the ones the dashboard rows render — confirm/extend against `templates/calls_index.html` and the `Call` model if the React row needs more, e.g. summary/url.)

- [ ] **Step 4: Implement `POST /api/me/default-profile`**

Append to `src/call_manager/web/api/me.py`:

```python
class DefaultProfileIn(BaseModel):
    profile_id: int | None


@router.post("/me/default-profile", status_code=200)
def set_default(request: Request, body: DefaultProfileIn,
                user: User = Depends(get_current_user)):  # noqa: B008
    from call_manager.web.dashboard import set_default_profile
    sf = request.app.state.session_factory
    with sf() as session:
        set_default_profile(session, user_id=user.id, profile_id=body.profile_id)
        session.commit()
    return {"ok": True}
```

Wire the calls router in `src/call_manager/web/api/__init__.py`:

```python
from call_manager.web.api import calls as _calls
router.include_router(_calls.router)
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_api_calls.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/api/calls.py src/call_manager/web/api/me.py src/call_manager/web/api/__init__.py tests/test_api_calls.py
git commit -m "feat(api): read-only GET /api/calls + POST /api/me/default-profile"
```

---

## Self-review checklist (completed)

- **Spec coverage:** error envelope (two mechanisms) ✓; `/api/me` ✓; read-only `/api/calls` ✓; `POST /api/me/default-profile` ✓; shared read-model service used by Jinja + JSON ✓; ranking `{pending, failed}` ✓; effective-selection resolution incl. absent-`?profile`→stored default read-only ✓. Deferred (correctly) to Plan 2/3: `SPA_MODE`, serving/manifest, `/events/ping`, OpenAPI→TS drift check, CSP work.
- **Placeholder scan:** one flagged soft spot — `CallOut` fields + the `User.is_admin` capability must be confirmed against `db/models.py` / `calls_index.html` (noted inline, with the file to check). No "TODO/implement later".
- **Type consistency:** `resolve_profile_selection`, `build_dashboard_view`, `set_default_profile`, `DashboardView` names/signatures are consistent across Tasks 4–5.

## Notes for Plans 2 & 3

- Plan 2 adds `SPA_MODE`, the route manifest + generator, Vite scaffold, dev proxy (default-to-backend carve-out + dev-CSP relaxation), prod mount + hatch `force-include` + wheel-contents CI check.
- Plan 3 adds the React app (TanStack Query + openapi-fetch + React Router Data Mode), the design-token split, the dashboard page (conditional polling on `ranking.pending`, drain refetch, deep-link read-only, explicit-selection POST), the `useEventStream` hook, the dev-gated `/events/ping` SSE endpoint with anti-buffering headers, native-form logout, and the dual-mode Playwright + dev-proxy + a11y tests.
