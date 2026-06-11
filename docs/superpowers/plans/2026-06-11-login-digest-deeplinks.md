# Login & Digest Deep-Link Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a weekly-digest link survive a hospital mail-scanner pre-fetch and, after sign-in, land the researcher on the *actual call* — not the generic dashboard or a dead-end.

**Architecture:** Three changes layered on the existing magic-link auth. (A) Split `GET /login/verify` into a side-effect-free **interstitial** (renders a "Confirm sign-in" form) and a token-consuming **`POST /login/verify`**, so a scanner's GET pre-fetch can't burn the single-use token. (B) Thread the originally-requested path as a validated `next` query param end to end — 401 handler → `/login` → emailed magic-link → interstitial → final redirect. (C) Replace silent dead-ends (expired link, no-guidance "check your email", hardcoded domain) with recovery pages and configured-domain hints. A single `safe_next()` validator guards every consumption point against open-redirect and oversize.

**Tech Stack:** Python 3.12, FastAPI + Jinja2, Starlette `TestClient`, pytest (+ testcontainers Postgres via the `pg_url` / `pg_session_factory` fixtures). No new dependencies. No migration (the `next` path is a validated query param, never stored).

**Spec:** [docs/superpowers/specs/2026-06-11-login-digest-deeplinks-design.md](../specs/2026-06-11-login-digest-deeplinks-design.md)

---

## File Structure

**Create:**
- `src/call_manager/auth/redirects.py` — the `safe_next()` open-redirect validator (one responsibility, unit-testable in isolation, imported by both `auth.py` and `main.py`).
- `src/call_manager/web/templates/login_confirm.html` — the scanner-proof "Confirm sign-in" interstitial.
- `src/call_manager/web/templates/login_link_expired.html` — the expired/used-link recovery page (inline new-link form).
- `tests/test_auth_redirects.py` — `safe_next()` unit tests.

**Modify:**
- `src/call_manager/web/routes/auth.py` — split verify into GET interstitial + POST consume; `next` on login form/submit; strict-origin override; recovery page wiring.
- `src/call_manager/main.py` — 401 handler encodes `next` for safe methods only.
- `src/call_manager/web/templates/login.html` — hidden `next` field + configured-domain hint (replace hardcoded `you@ki.se`).
- `src/call_manager/web/templates/login_sent.html` — spam/latency guidance + `next`-preserving "Back to sign in".
- `tests/test_web_auth.py` — migrate the five GET-verify tests to POST; add the new behavior tests.

**Untouched (deliberately):** `digest.html.jinja` — tokenized digest links are a non-goal for this slice; the digest journey is repaired purely by the `next`-threading + interstitial path. `auth/csrf.py`, `web/middleware.py` — the CSRF relaxation and the app-wide `same-origin` default are unchanged; only the interstitial *response* overrides `Referrer-Policy`.

---

## Test infrastructure (read before Task 1)

`tests/test_web_auth.py` already defines the helper every web test here reuses:

```python
def _mk_app(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()
    sender = InMemoryEmailSender()
    app.state.email_sender = sender
    app.state.email_limiter = SlidingWindowLimiter(max_requests=100, window_seconds=60)
    app.state.ip_limiter = SlidingWindowLimiter(max_requests=100, window_seconds=60)
    return app, sender
```

It also imports `issue_token`, `User`, `SlidingWindowLimiter`, `InMemoryEmailSender`, `TestClient`. New tests in that file reuse `_mk_app`, the `pg_url` / `pg_session_factory` fixtures, and `issue_token` to mint a real token. The TestClient sends no `Origin`/`Referer` by default, so `OriginCSRFMiddleware` allows its POSTs (Django-style relaxation) — to exercise the CSRF path you set the headers explicitly.

---

## Phase 1 — The `safe_next` validator

### Task 1: `safe_next()` open-redirect + oversize validator

**Files:**
- Create: `src/call_manager/auth/redirects.py`
- Test: `tests/test_auth_redirects.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_auth_redirects.py`:

```python
import pytest

from call_manager.auth.redirects import MAX_NEXT_BYTES, safe_next


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "relative/no/leading/slash",
        "//evil.com",
        "https://evil.com",
        "http://evil.com",
        "/\\evil.com",
        "/back\\slash",
        "/has\ttab",
        "/has\nnewline",
        "/has\rcarriage",
        "/has\x7fdel",
        "/" + "a" * (MAX_NEXT_BYTES + 1),
    ],
)
def test_safe_next_rejects(value):
    assert safe_next(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "/",
        "/calls/5",
        "/calls/5?profile=3",
        "/calls/5?profile=3&source=x",
    ],
)
def test_safe_next_accepts_local_paths(value):
    # Returns the path unchanged (decoded form); callers must urlencode it.
    assert safe_next(value) == value


def test_safe_next_accepts_at_length_boundary():
    value = "/" + "a" * (MAX_NEXT_BYTES - 1)
    assert len(value.encode("utf-8")) == MAX_NEXT_BYTES
    assert safe_next(value) == value
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_auth_redirects.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'call_manager.auth.redirects'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/call_manager/auth/redirects.py`:

```python
from __future__ import annotations

from urllib.parse import urlparse

# Cap the relative path we round-trip through the magic-link URL. An
# allowed user could otherwise submit a giant `next` and inflate the
# emailed link (and every redirect that carries it).
MAX_NEXT_BYTES = 2048


def safe_next(path: str | None) -> str | None:
    """Validate an open-redirect-safe local relative path.

    Returns the path unchanged if it is a safe local target, else None
    (callers fall back to "/"). Accepts ONLY a local relative path:

    * must start with exactly one "/" (rejects "//host", "/\\host")
    * no backslashes anywhere, no control characters
    * urlparse must yield neither a scheme nor a netloc
    * at most MAX_NEXT_BYTES bytes

    The returned value is the DECODED path; it may itself contain a query
    string (e.g. "/calls/5?profile=3"), so callers MUST urlencode it when
    embedding it into a URL — never string-interpolate.
    """
    if not path:
        return None
    if len(path.encode("utf-8")) > MAX_NEXT_BYTES:
        return None
    if not path.startswith("/"):
        return None
    if path.startswith("//") or path.startswith("/\\"):
        return None
    if "\\" in path:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        return None
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc:
        return None
    return path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_auth_redirects.py -q`
Expected: PASS (all parametrized cases green).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/auth/redirects.py tests/test_auth_redirects.py
git commit -m "feat(auth): safe_next open-redirect + 2048-byte validator"
```

---

## Phase 2 — Scanner-proof verify (Component A)

### Task 2: Split `GET /login/verify` (interstitial) from `POST /login/verify` (consume)

This is the core scanner-proofing change. The GET handler renders a confirm form with **no side effects** and overrides `Referrer-Policy` to `strict-origin`; the POST handler consumes the token (the old GET logic), mints the cookie, redirects to a validated `next`, and carries the IP rate-limit. Five existing GET-verify tests migrate to POST.

**Files:**
- Modify: `src/call_manager/web/routes/auth.py` (imports; replace `login_verify`)
- Create: `src/call_manager/web/templates/login_confirm.html`
- Test: `tests/test_web_auth.py` (migrate 5 tests; add 6 new)

- [ ] **Step 1: Add the new-behavior failing tests**

In `tests/test_web_auth.py`, add these new tests (append near the other verify tests):

```python
def test_get_verify_renders_interstitial_without_consuming(
    monkeypatch, pg_url, pg_session_factory
):
    """GET /login/verify renders the confirm page and does NOT consume the
    token — a mail-scanner pre-fetch must leave it valid for the human."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.flush()
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()

    with TestClient(app) as client:
        r = client.get(f"/login/verify?token={raw}", follow_redirects=False)
        assert r.status_code == 200
        assert "confirm sign-in" in r.text.lower()
        # Token must NOT leak to the same-origin /static assets the page loads.
        assert r.headers["referrer-policy"] == "strict-origin"
        # Token still valid: the human's subsequent POST succeeds.
        p = client.post("/login/verify", data={"token": raw}, follow_redirects=False)
    assert p.status_code == 303
    assert "session" in p.cookies


def test_post_verify_consumes_and_redirects_to_next(
    monkeypatch, pg_url, pg_session_factory
):
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.flush()
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()

    with TestClient(app) as client:
        r = client.post(
            "/login/verify",
            data={"token": raw, "next": "/calls/5?profile=3"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/calls/5?profile=3"
    assert "session" in r.cookies


def test_post_verify_unsafe_next_falls_back_to_root(
    monkeypatch, pg_url, pg_session_factory
):
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.flush()
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()

    with TestClient(app) as client:
        r = client.post(
            "/login/verify",
            data={"token": raw, "next": "https://evil.com"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_post_verify_succeeds_under_origin_csrf_check(
    monkeypatch, pg_url, pg_session_factory
):
    """The real interstitial POST carries same-origin Origin/Referer; the
    CSRF middleware must allow it. Regression guard against reintroducing
    no-referrer / Origin: null on the verify response."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.flush()
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()

    with TestClient(app) as client:
        r = client.post(
            "/login/verify",
            data={"token": raw},
            headers={
                "Origin": "http://testserver",
                "Referer": "http://testserver/login/verify",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
```

- [ ] **Step 2: Migrate the five existing GET-verify tests to POST**

In `tests/test_web_auth.py`, replace these existing tests in place:

Replace `test_verify_sets_session_cookie_and_redirects`:

```python
def test_verify_sets_session_cookie_and_redirects(monkeypatch, pg_url, pg_session_factory):
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.flush()
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()

    with TestClient(app) as client:
        r = client.post("/login/verify", data={"token": raw}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "session" in r.cookies
```

Replace `test_verify_rejects_unknown_token`:

```python
def test_verify_rejects_unknown_token(monkeypatch, pg_url):
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post("/login/verify", data={"token": "nope"}, follow_redirects=False)
    assert r.status_code == 400
```

Replace `test_verify_rejects_replay`:

```python
def test_verify_rejects_replay(monkeypatch, pg_url, pg_session_factory):
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.flush()
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()

    with TestClient(app) as client:
        assert (
            client.post("/login/verify", data={"token": raw}, follow_redirects=False).status_code
            == 303
        )
        r = client.post("/login/verify", data={"token": raw}, follow_redirects=False)
    assert r.status_code == 400
```

Replace the action line in `test_verify_survives_post_consumption_failure` (keep the rest of the test body — the monkeypatch and the `MagicLinkToken.consumed_at` assertion are unchanged):

```python
    with TestClient(app) as client, contextlib.suppress(RuntimeError):
        client.post("/login/verify", data={"token": raw}, follow_redirects=False)
```

Replace `test_login_verify_is_rate_limited_by_ip`:

```python
def test_post_verify_is_rate_limited_by_ip(monkeypatch, pg_url, pg_session_factory):
    """The IP limiter moved to the verb that consumes — POST /login/verify."""
    app, _ = _mk_app(monkeypatch, pg_url)
    app.state.ip_limiter = SlidingWindowLimiter(max_requests=3, window_seconds=60)

    with TestClient(app) as client:
        for _ in range(3):
            r = client.post("/login/verify", data={"token": "nope"}, follow_redirects=False)
            assert r.status_code == 400
        r = client.post("/login/verify", data={"token": "nope"}, follow_redirects=False)
    assert r.status_code == 429
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_auth.py -q`
Expected: FAIL — the new GET test gets a 303 (old consuming GET) not a 200 interstitial; the POST tests 405 (no POST route yet).

- [ ] **Step 4: Add imports and the interstitial template**

In `src/call_manager/web/routes/auth.py`, add to the imports near the top (after the existing `from datetime import ...`):

```python
from urllib.parse import urlencode

from call_manager.auth.redirects import safe_next
```

Create `src/call_manager/web/templates/login_confirm.html`:

```html
{% extends "base.html" %}
{% block title %}Confirm sign-in — Call Manager{% endblock %}
{% block content %}
<div class="narrow">
  <div class="auth-card" style="text-align:center">
    <div class="brand-mark">
      <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
        <path d="M5 12.5 10 17.5 19 7"/>
      </svg>
    </div>
    <h1>Confirm sign-in</h1>
    <p>Click below to finish signing in to Call Manager.</p>
    <form method="post" action="/login/verify">
      <input type="hidden" name="token" value="{{ token }}">
      {% if next_path %}<input type="hidden" name="next" value="{{ next_path }}">{% endif %}
      <button type="submit">Confirm sign-in</button>
    </form>
  </div>
</div>
{% endblock %}
```

- [ ] **Step 5: Replace `login_verify` with the GET interstitial + POST consume handlers**

In `src/call_manager/web/routes/auth.py`, replace the entire `@router.get("/login/verify")` function (the `login_verify` def, lines ~179-231) with these two handlers:

```python
@router.get("/login/verify", response_class=HTMLResponse)
def login_verify_confirm(request: Request, token: str, next: str | None = None):
    """Render the scanner-proof 'Confirm sign-in' interstitial.

    NO side effects: this does not consume the token. Hospital mail
    gateways (Safe Links / Proofpoint / Mimecast) pre-fetch URLs with a
    GET to scan them, so leaving the token untouched here means the human's
    later POST still works.

    The response OVERRIDES the app-wide `Referrer-Policy: same-origin`
    (set by SecurityHeadersMiddleware, which leaves a handler's own header
    alone). The page extends base.html, which loads same-origin /static
    assets (pico/app CSS+JS, favicon, htmx); under `same-origin` those
    sub-requests would carry the full verify URL — INCLUDING ?token= — in
    their Referer. `strict-origin` sends only scheme+host (no path/query),
    so the token never leaks, while still giving the CSRF check the
    non-null Origin/Referer it needs for the confirm POST. (`no-referrer`
    is forbidden: it nulls Origin on form POSTs → 403.)
    """
    response = request.app.state.templates.TemplateResponse(
        request=request,
        name="login_confirm.html",
        context={"token": token, "next_path": safe_next(next)},
    )
    response.headers["Referrer-Policy"] = "strict-origin"
    return response


@router.post("/login/verify")
def login_verify_consume(
    request: Request,
    token: str = Form(...),
    next: str | None = Form(default=None),
):
    session_factory = request.app.state.session_factory
    session_signer = request.app.state.session_signer
    ip_limiter = request.app.state.ip_limiter
    settings = request.app.state.settings

    # The IP rate-limit lives on the verb that actually consumes the token.
    client_ip = _client_ip(request)
    if not ip_limiter.allow(client_ip):
        _logger.info(
            "verify rate-limited by IP",
            extra={"event": "verify_rate_limited_ip", "ip": client_ip},
        )
        raise HTTPException(status_code=429, detail="too many requests")

    # Commit consumption in its own transaction so any later failure
    # (user missing, DB hiccup, code change that raises) cannot roll the
    # consumption back. Single-use must not depend on what happens after.
    with session_factory() as session:
        try:
            user_id = consume_token(session, token)
        except InvalidToken as e:
            # A human clicked a stale/used interstitial. Surfaced as a plain
            # 400 here; Task 5 swaps it for a dedicated recovery page.
            raise HTTPException(status_code=400, detail="invalid or expired link") from e
        session.commit()

    with session_factory() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=400, detail="invalid link")
        user.last_login_at = datetime.now(UTC)
        session_epoch = user.session_epoch
        session.commit()

    cookie_value = session_signer.make(user_id=user_id, session_epoch=session_epoch)
    target = safe_next(next) or "/"
    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(
        "session",
        cookie_value,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.cookie_secure,  # True when APP_BASE_URL is https://
        samesite="lax",
    )
    # The POST body carried the single-use token; suppress Referer so the
    # post-login page can't leak anything to third parties. Safe on this
    # redirect (the next request is a GET — no CSRF check — so no Origin:
    # null regression).
    response.headers["Referrer-Policy"] = "no-referrer"
    _logger.info("verify success", extra={"event": "verify_success"})
    return response
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_auth.py -q`
Expected: PASS (migrated + new tests green).

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check src/call_manager/web/routes/auth.py
git add src/call_manager/web/routes/auth.py \
        src/call_manager/web/templates/login_confirm.html \
        tests/test_web_auth.py
git commit -m "feat(auth): scanner-proof verify — GET interstitial + POST consume, strict-origin"
```

---

## Phase 3 — Next-URL threading (Component B)

### Task 3: 401 handler encodes `next` for safe methods only

When a browser (Accept: text/html) hits a 401 on a **GET/HEAD**, redirect to `/login?next=<urlencoded relative path>`. For any other method (an expired-session POST that 401s) redirect to bare `/login` — threading a POST path would produce a final `303 GET` to a POST-only route and a `405`.

**Files:**
- Modify: `src/call_manager/main.py` (imports; `_redirect_html_401`)
- Test: `tests/test_web_auth.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web_auth.py`:

```python
def test_html_401_get_threads_next(monkeypatch, pg_url):
    """An unauthenticated browser GET on a protected path redirects to
    /login carrying the urlencoded relative path (path + query)."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get(
            "/calls/5?profile=3",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fcalls%2F5%3Fprofile%3D3"


def test_html_401_post_does_not_thread_next(monkeypatch, pg_url):
    """A POST that 401s must redirect to bare /login (no next) — threading
    a POST path would 303-GET into a POST-only route and 405."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post(
            "/calls/5/hide",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_json_401_unaffected_by_next(monkeypatch, pg_url):
    """A non-HTML caller still gets the raw JSON 401, no redirect."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/calls/5?profile=3", follow_redirects=False)
    assert r.status_code == 401
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_auth.py -k "401" -q`
Expected: FAIL — `test_html_401_get_threads_next` gets `/login` (next not yet threaded).

- [ ] **Step 3: Add imports and update the handler**

In `src/call_manager/main.py`, add near the top imports:

```python
from urllib.parse import urlencode

from call_manager.auth.redirects import safe_next
```

Replace the `if exc.status_code == 401 and wants_html:` block inside `_redirect_html_401` (currently lines ~296-301) with:

```python
        if exc.status_code == 401 and wants_html:
            # Preserve the originally-requested path so the user lands back
            # there after sign-in. Only for safe methods: threading a POST
            # path would produce a final `303 GET` to a POST-only route and
            # a 405. `safe_next` rejects anything non-local or oversized.
            if request.method in ("GET", "HEAD"):
                raw_next = request.url.path
                if request.url.query:
                    raw_next = f"{raw_next}?{request.url.query}"
                target = safe_next(raw_next)
                if target:
                    return RedirectResponse(
                        url=f"/login?{urlencode({'next': target})}",
                        status_code=303,
                    )
            return RedirectResponse(url="/login", status_code=303)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_auth.py -k "401" -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/call_manager/main.py
git add src/call_manager/main.py tests/test_web_auth.py
git commit -m "feat(auth): thread next through the HTML 401 redirect (safe methods only)"
```

---

### Task 4: Thread `next` through `/login` (form + submit) and into the magic-link

`GET /login` reads/validates `next` and renders it as a hidden field; `POST /login` accepts it as an optional form field, threads the validated value into the emailed magic-link URL (proper `urlencode`), and preserves it in the `login_sent` "Back to sign in" link. The login form also shows the configured domain(s) instead of the hardcoded `you@ki.se`. `next` never affects any allow/deny/rate-limit branch (enumeration-resistance intact).

**Files:**
- Modify: `src/call_manager/web/routes/auth.py` (`login_form`, `login_submit`)
- Modify: `src/call_manager/web/templates/login.html`
- Modify: `src/call_manager/web/templates/login_sent.html`
- Test: `tests/test_web_auth.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web_auth.py`:

```python
def test_next_threaded_end_to_end_with_nested_query(
    monkeypatch, pg_url, pg_session_factory
):
    """A nested-query next survives /login?next=… → emailed magic-link →
    POST verify → final redirect, with BOTH query params intact."""
    import re
    from urllib.parse import parse_qs, urlparse

    app, sender = _mk_app(monkeypatch, pg_url)
    nxt = "/calls/5?profile=3&source=x"
    with TestClient(app) as client:
        g = client.get("/login", params={"next": nxt})
        assert g.status_code == 200  # renders the hidden next field
        client.post("/login", data={"email": "alice@ki.se", "next": nxt})
        body = sender.sent[-1].text_body
        verify_path = re.search(r"(/login/verify\?\S+)", body).group(1)
        q = parse_qs(urlparse(verify_path).query)
        assert q["next"] == [nxt]  # encode→email→decode round-trip intact
        r = client.post(
            "/login/verify",
            data={"token": q["token"][0], "next": q["next"][0]},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/calls/5?profile=3&source=x"


def test_login_sent_preserves_next_in_back_link(monkeypatch, pg_url, pg_session_factory):
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post("/login", data={"email": "alice@ki.se", "next": "/calls/5?profile=3"})
    assert r.status_code == 200
    assert 'href="/login?next=%2Fcalls%2F5%3Fprofile%3D3"' in r.text


def test_login_post_drops_unsafe_next(monkeypatch, pg_url, pg_session_factory):
    """An open-redirect next must never reach the magic-link URL."""
    app, sender = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        client.post("/login", data={"email": "alice@ki.se", "next": "https://evil.com"})
    body = sender.sent[-1].text_body
    assert "evil.com" not in body
    assert "next=" not in body


def test_login_enumeration_resistant_with_next(monkeypatch, pg_url, pg_session_factory):
    """next must not change the response across allow/deny branches."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        allowed = client.post("/login", data={"email": "alice@ki.se", "next": "/calls/5"})
        denied = client.post("/login", data={"email": "x@evil.com", "next": "/calls/5"})
    assert allowed.status_code == denied.status_code == 200
    assert allowed.text == denied.text  # byte-identical


def test_login_form_shows_configured_domain(monkeypatch, pg_url):
    """The login page renders the allowlisted domain, not a hardcoded one."""
    app, _ = _mk_app(monkeypatch, pg_url)  # ALLOWED_EMAIL_DOMAINS=ki.se
    with TestClient(app) as client:
        r = client.get("/login")
    assert r.status_code == 200
    assert "you@ki.se" in r.text  # placeholder now derived from config
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_auth.py -k "next or configured_domain or enumeration" -q`
Expected: FAIL — `login_form` rejects the unexpected `next` kwarg / no hidden field / magic-link has no `next`.

- [ ] **Step 3: Update `login_form` and `login_submit`**

In `src/call_manager/web/routes/auth.py`, replace `login_form` (lines ~75-79) with:

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
        },
    )
```

In `login_submit`, change the signature to accept `next`:

```python
@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(...),
    next: str | None = Form(default=None),
):
```

Immediately after the function's local bindings (`session_factory = request.app.state.session_factory`), and BEFORE the shared `response = ... login_sent.html ...` is built, compute the validated next and the back link:

```python
    # Validate `next` once. It rides into the magic-link URL and the
    # "Back to sign in" link on the check-your-email page. It NEVER
    # influences any allow/deny/rate-limit branch below, so the response
    # stays byte-identical across branches (enumeration-resistance intact).
    safe = safe_next(next)
    back_href = "/login"
    if safe:
        back_href = "/login?" + urlencode({"next": safe})
```

Change the shared response to pass `back_href`:

```python
    response = request.app.state.templates.TemplateResponse(
        request=request, name="login_sent.html", context={"back_href": back_href}
    )
```

Replace the magic-link construction (currently `link = f"{settings.app_base_url.rstrip('/')}/login/verify?token={raw}"`) with a properly-encoded build:

```python
    params = {"token": raw}
    if safe:
        params["next"] = safe
    link = f"{settings.app_base_url.rstrip('/')}/login/verify?{urlencode(params)}"
```

- [ ] **Step 4: Update the two templates**

In `src/call_manager/web/templates/login.html`, replace the `<form method="post" action="/login">` block (lines ~20-27) with:

```html
    <form method="post" action="/login">
      {% if next_path %}<input type="hidden" name="next" value="{{ next_path }}">{% endif %}
      <label>
        Email
        <input type="email" name="email" required autofocus autocomplete="email"
               placeholder="you@{{ allowed_domains[0] if allowed_domains else 'example.org' }}">
      </label>
      {% if allowed_domains %}
      <small>Use your hospital email ({{ allowed_domains | join(' or ') }}).</small>
      {% endif %}
      <button type="submit">Send login link</button>
    </form>
```

In `src/call_manager/web/templates/login_sent.html`, replace the back-link line (line ~15, `<p><a href="/login">← Back to sign in</a></p>`) with:

```html
    <p><a href="{{ back_href | default('/login', true) }}">← Back to sign in</a></p>
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_auth.py -k "next or configured_domain or enumeration" -q`
Expected: PASS.

- [ ] **Step 6: Run the whole auth test file (catch regressions)**

Run: `uv run pytest tests/test_web_auth.py -q`
Expected: PASS (the existing enumeration/rate-limit/send tests still green — `next` defaulting to None preserves old behavior).

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check src/call_manager/web/routes/auth.py
git add src/call_manager/web/routes/auth.py \
        src/call_manager/web/templates/login.html \
        src/call_manager/web/templates/login_sent.html \
        tests/test_web_auth.py
git commit -m "feat(auth): thread next through /login into the magic-link; show configured domain"
```

---

## Phase 4 — Dead-ends & guidance (Component C)

### Task 5: Expired-link recovery page + spam/latency guidance

Replace the generic `error.html` for the used/expired link with a dedicated recovery page (HTTP 400) carrying an inline new-link form that forwards `next`; add spam/latency guidance to `login_sent.html`. Reached from the `InvalidToken` path of `POST /login/verify`.

**Files:**
- Create: `src/call_manager/web/templates/login_link_expired.html`
- Modify: `src/call_manager/web/routes/auth.py` (`login_verify_consume` InvalidToken branch)
- Modify: `src/call_manager/web/templates/login_sent.html`
- Test: `tests/test_web_auth.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web_auth.py`:

```python
def test_expired_link_post_renders_recovery_page(monkeypatch, pg_url, pg_session_factory):
    """A human clicking a stale interstitial (bad/used token) gets the
    dedicated recovery page at 400, with an inline new-link form that
    forwards the original next."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post(
            "/login/verify",
            data={"token": "already-used-or-bogus", "next": "/calls/5?profile=3"},
            follow_redirects=False,
        )
    assert r.status_code == 400
    text = r.text.lower()
    assert "expired" in text or "already used" in text
    assert 'action="/login"' in r.text  # inline new-link form
    assert 'value="/calls/5?profile=3"' in r.text  # next forwarded


def test_login_sent_shows_spam_guidance(monkeypatch, pg_url, pg_session_factory):
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post("/login", data={"email": "alice@ki.se"})
    assert r.status_code == 200
    assert "spam" in r.text.lower()


def test_replay_renders_recovery_page(monkeypatch, pg_url, pg_session_factory):
    """The single-use replay (Task 2 asserted status 400) now lands on the
    recovery page — still 400, but the dedicated body."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.flush()
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()
    with TestClient(app) as client:
        assert (
            client.post("/login/verify", data={"token": raw}, follow_redirects=False).status_code
            == 303
        )
        r = client.post("/login/verify", data={"token": raw}, follow_redirects=False)
    assert r.status_code == 400
    assert "expired" in r.text.lower() or "already used" in r.text.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_auth.py -k "recovery or spam_guidance" -q`
Expected: FAIL — the InvalidToken path still raises the generic 400 (`error.html`), `login_sent.html` has no spam guidance.

- [ ] **Step 3: Create the recovery template**

Create `src/call_manager/web/templates/login_link_expired.html`:

```html
{% extends "base.html" %}
{% block title %}Link expired — Call Manager{% endblock %}
{% block content %}
<div class="narrow">
  <div class="auth-card" style="text-align:center">
    <div class="brand-mark">
      <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
        <circle cx="12" cy="12" r="9"/>
        <path d="M12 7.5V12l3 2"/>
      </svg>
    </div>
    <h1>This link expired or was already used</h1>
    <p>Login links are valid for 15 minutes and can be used once. Enter your email to get a new one.</p>
    <form method="post" action="/login">
      {% if next_path %}<input type="hidden" name="next" value="{{ next_path }}">{% endif %}
      <label>
        Email
        <input type="email" name="email" required autofocus autocomplete="email"
               placeholder="you@{{ allowed_domains[0] if allowed_domains else 'example.org' }}">
      </label>
      <button type="submit">Send a new link</button>
    </form>
  </div>
</div>
{% endblock %}
```

- [ ] **Step 4: Wire the InvalidToken branch to render the recovery page**

In `src/call_manager/web/routes/auth.py`, inside `login_verify_consume`, replace the InvalidToken handler:

```python
        try:
            user_id = consume_token(session, token)
        except InvalidToken as e:
            raise HTTPException(status_code=400, detail="invalid or expired link") from e
```

with a dedicated 400 recovery page that forwards `next` (note: returning from inside the `with` cleanly closes the session — nothing was committed):

```python
        try:
            user_id = consume_token(session, token)
        except InvalidToken:
            # A human clicked a stale/used interstitial. Show a dedicated
            # recovery page (HTTP 400, preserving the status semantics)
            # with an inline new-link form that carries `next` forward.
            return request.app.state.templates.TemplateResponse(
                request=request,
                name="login_link_expired.html",
                context={
                    "next_path": safe_next(next),
                    "allowed_domains": settings.allowed_email_domains,
                },
                status_code=400,
            )
```

- [ ] **Step 5: Add the spam/latency guidance to `login_sent.html`**

In `src/call_manager/web/templates/login_sent.html`, add a line immediately after the existing `<p>If the address is recognized…</p>` (line ~14):

```html
    <p>This can take a minute — check your spam folder if it doesn't arrive.</p>
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_auth.py -k "recovery or spam_guidance or replay" -q`
Expected: PASS.

- [ ] **Step 7: Run the whole auth file**

Run: `uv run pytest tests/test_web_auth.py -q`
Expected: PASS — note the earlier `test_verify_rejects_replay` and `test_verify_rejects_unknown_token` (Task 2) still pass: they assert only `status_code == 400`, which the recovery page preserves.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check src/call_manager/web/routes/auth.py
git add src/call_manager/web/routes/auth.py \
        src/call_manager/web/templates/login_link_expired.html \
        src/call_manager/web/templates/login_sent.html \
        tests/test_web_auth.py
git commit -m "feat(auth): expired-link recovery page + spam/latency guidance"
```

---

## Final verification

- [ ] **Run the full auth + redirect suite**

Run: `uv run pytest tests/test_auth_redirects.py tests/test_web_auth.py tests/test_security_headers.py tests/test_auth_csrf.py -q`
Expected: PASS. (`test_security_headers.py` confirms non-verify routes still emit `referrer-policy: same-origin`; the interstitial's `strict-origin` is a per-response override and does not change the middleware default.)

- [ ] **Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS (allowing for the documented pre-existing APScheduler/testcontainer flakiness in unrelated tests — rerun any such failure to confirm it is not introduced here).

- [ ] **Lint the full changeset**

Run: `uv run ruff check src tests`
Expected: clean.

---

## Spec coverage map

| Spec requirement | Task |
|---|---|
| A. GET interstitial (no consume) + POST consume | Task 2 |
| A. `strict-origin` override on the interstitial | Task 2 |
| A. IP rate-limit moves to POST | Task 2 |
| B. 401 handler threads `next`, GET/HEAD only | Task 3 |
| B. `GET /login` reads/validates `next` → hidden field | Task 4 |
| B. `POST /login` threads `next` into magic-link (encoded) | Task 4 |
| B. `login_sent` preserves `next` in "Back to sign in" | Task 4 |
| B. `POST /login/verify` redirects to validated `next` else `/` | Task 2 (redirect) |
| B. `urlencode` at every embed point | Tasks 2–5 |
| C. Expired/used-link recovery page (HTTP 400) + inline form + `next` | Task 5 |
| C. `login_sent` spam/latency guidance | Task 5 |
| C. `login.html` configured-domain hint | Task 4 |
| Security: `_safe_next` (relative-only, 2048-byte cap, control/backslash/scheme/netloc) | Task 1 |
| Security: single-use + TTL unchanged (verb-only change) | Task 2 |
| Security: enumeration-resistance unchanged | Task 4 |
| Testing: nested-query round-trip; safe-method-only; strict-origin no-leak; CSRF POST; dead-ends | Tasks 2–5 |

All spec sections map to a task. No DB migration (validated query param, per the spec's approach decision).
