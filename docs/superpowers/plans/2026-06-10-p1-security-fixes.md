# P1 — Security Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four pre-internet-exposure security/robustness gaps that need no schema change, so this plan is shippable independently of the user-system redesign (P2).

**Architecture:** Four independent changes — (1) enforce the cloud-LLM disclosure acknowledgement server-side in `post_turn`; (2) bound untrusted file parsing with a hard wall-clock kill; (3) add a catch-all exception handler so unhandled errors render the styled page instead of a bare 500; (4) ship a production `.env` template + hardening checklist. Each is its own task with its own commit.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Jinja2, pytest, `multiprocessing` (stdlib).

**Source spec:** `docs/superpowers/specs/2026-06-10-user-system-and-security-hardening-design.md` §9.

**Conventions:** Tests live under `tests/` mirroring the package path. Commit style: `fix(area): …` / `feat(area): …` / `docs(area): …`. Run the suite with `uv run pytest`. The DB-backed tests use the `pg_session_factory` / `client_authed_owner` fixtures and the `make_user` / `make_workspace` helpers in `tests/proposal_assist/conftest.py`.

---

## File structure

| File | Responsibility | Task |
|---|---|---|
| `src/call_manager/proposal_assist/web/workspaces.py` | add disclosure gate to `post_turn` | 1 |
| `tests/proposal_assist/test_web_workspaces_disclosure_gate.py` (new) | gate tests | 1 |
| `src/call_manager/proposal_assist/references/extract_sandbox.py` (new) | run `extract_text` in a killable subprocess with a wall-clock timeout | 2 |
| `src/call_manager/proposal_assist/references/ingest.py` | call the bounded extractor in Phase 2 | 2 |
| `src/call_manager/config.py` | add `reference_extract_timeout_seconds` setting | 2 |
| `tests/proposal_assist/references/test_extract_sandbox.py` (new) | timeout tests | 2 |
| `src/call_manager/main.py` | add catch-all `Exception` handler | 3 |
| `tests/test_exception_handler.py` (new) | catch-all tests | 3 |
| `.env.production.example` (new) + `docs/deployment-hardening.md` (new) | deployment template + checklist | 4 |

---

## Task 1: Enforce the disclosure gate in `post_turn`

Today `post_turn` ([workspaces.py:693](../../../src/call_manager/proposal_assist/web/workspaces.py#L693)) checks `_require_owner` and the kill-switch, then calls `submit_user_turn` at line 844 — but never checks whether the acting user acknowledged the data disclosure. The `needs_disclosure` flag is computed only for the GET render ([workspaces.py:566-576](../../../src/call_manager/proposal_assist/web/workspaces.py#L566)). A direct POST (curl / replayed HTMX) sends workspace text to the cloud LLM with the warning never shown.

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (in `post_turn`, after the kill-switch block at line 708, before `storage = AttachmentStorage(...)` at line 709)
- Test: `tests/proposal_assist/test_web_workspaces_disclosure_gate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/test_web_workspaces_disclosure_gate.py
"""POST /workspaces/<id>/turns must 403 until the acting user has a
disclosure_acknowledged WorkspaceEvent (server-side enforcement of the
UI-only modal). See spec 2026-06-10 §9."""
from call_manager.db.models import WorkspaceEvent


def test_post_turn_blocked_without_disclosure_ack(client_authed_owner, owner_user, workspace):
    # No disclosure_acknowledged event exists for this user yet.
    resp = client_authed_owner.post(
        f"/workspaces/{workspace.id}/turns",
        data={"content": "draft the aims section"},
    )
    assert resp.status_code == 403
    assert "disclosure" in resp.text.lower()


def test_post_turn_allowed_after_disclosure_ack(client_authed_owner, workspace):
    # Acknowledge first, then the turn is accepted (not 403).
    ack = client_authed_owner.post(f"/workspaces/{workspace.id}/disclosure")
    assert ack.status_code == 200
    resp = client_authed_owner.post(
        f"/workspaces/{workspace.id}/turns",
        data={"content": "draft the aims section"},
    )
    assert resp.status_code != 403
```

> If the fixtures `owner_user` / `workspace` / `client_authed_owner` differ in name in `tests/proposal_assist/conftest.py`, use the existing ones (the workspace-owner client + its user + a workspace row). Check `conftest.py` first.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_disclosure_gate.py -v`
Expected: `test_post_turn_blocked_without_disclosure_ack` FAILS (returns non-403, because there is no gate yet).

- [ ] **Step 3: Add the gate in `post_turn`**

Insert, in `src/call_manager/proposal_assist/web/workspaces.py`, immediately after the kill-switch block (after line 708, the `return HTMLResponse(... 503)` / `if not settings.drafting_enabled:` block) and before `storage = AttachmentStorage(root=Path(settings.workspaces_dir))`:

```python
    # Spec 2026-06-10 §9: the disclosure acknowledgement is enforced
    # server-side here, not just by the UI modal. A direct POST (curl /
    # replayed HTMX) that skips the modal must NOT reach the cloud LLM.
    # Per-actor: each owner/editor acknowledges before their first turn.
    prior_ack = session.scalar(
        select(WorkspaceEvent.id)
        .where(
            WorkspaceEvent.workspace_id == ws.id,
            WorkspaceEvent.kind == "disclosure_acknowledged",
            WorkspaceEvent.actor_id == user.id,
        )
        .limit(1)
    )
    if prior_ack is None:
        return HTMLResponse(
            "<p>Please acknowledge the data-disclosure notice before "
            "sending a message to the drafting assistant.</p>",
            status_code=403,
        )
```

`WorkspaceEvent` and `select` are already imported in this module (used by the identical query at line 567). Confirm with `grep -n "^from\|^import\|WorkspaceEvent\|select" src/call_manager/proposal_assist/web/workspaces.py | head`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_disclosure_gate.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Run the surrounding suite (no regressions in turn submission)**

Run: `uv run pytest tests/proposal_assist/ -k "turn or disclosure or workspace" -q`
Expected: green. Existing turn-submission tests that POST `/turns` may now need a disclosure ack first — if any fail with 403, add a `client.post(f"/workspaces/{ws.id}/disclosure")` before the turn POST in that test (this is the correct new precondition, not a regression).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/web/workspaces.py tests/proposal_assist/test_web_workspaces_disclosure_gate.py
git commit -m "fix(security): enforce cloud-LLM disclosure ack server-side in post_turn"
```

---

## Task 2: Bound untrusted file parsing with a wall-clock kill

`extract_text` ([extract.py:64](../../../src/call_manager/proposal_assist/references/extract.py#L64)) runs synchronously in Phase 2 of ingest ([ingest.py:166](../../../src/call_manager/proposal_assist/references/ingest.py#L166)). Byte/page caps bound memory but **not CPU time** — a small pathological PDF/RTF can pin the single-process app. We run extraction in a separate `multiprocessing` process and `terminate()` it on timeout (the only approach that actually kills CPU-bound work; a thread can't be force-killed). On timeout the existing `except Exception` block at [ingest.py:200](../../../src/call_manager/proposal_assist/references/ingest.py#L200) already marks the reference `text_status="failed"`.

**Files:**
- Create: `src/call_manager/proposal_assist/references/extract_sandbox.py`
- Modify: `src/call_manager/config.py` (add one setting)
- Modify: `src/call_manager/proposal_assist/references/ingest.py:166` (call the bounded wrapper)
- Test: `tests/proposal_assist/references/test_extract_sandbox.py`

- [ ] **Step 1: Add the config setting**

In `src/call_manager/config.py`, add alongside the other reference/ingest settings (anywhere in the `Settings` class body):

```python
    reference_extract_timeout_seconds: float = Field(
        default=30.0,
        alias="REFERENCE_EXTRACT_TIMEOUT_SECONDS",
        description="Hard wall-clock bound on untrusted file text-extraction. "
        "Extraction runs in a separate process that is terminated on timeout; "
        "the reference is then marked text_status='failed'. Guards the single "
        "app process against a pathological upload pinning the CPU.",
    )
```

- [ ] **Step 2: Write the failing test**

```python
# tests/proposal_assist/references/test_extract_sandbox.py
"""extract_text_bounded runs extraction in a killable subprocess and
raises ExtractError on timeout. See spec 2026-06-10 §9."""
import pytest

from call_manager.proposal_assist.references.extract import ExtractError
from call_manager.proposal_assist.references.extract_sandbox import extract_text_bounded


def test_bounded_extraction_returns_sections_for_normal_input():
    md = b"# Title\n\nSome body text."
    sections = extract_text_bounded(
        mime="text/markdown", data=md, timeout_seconds=30.0
    )
    assert sections
    assert any("body text" in t for (_, t) in sections)


def test_bounded_extraction_times_out(monkeypatch):
    # A 0.5s budget against a parser we force to sleep -> ExtractError.
    import call_manager.proposal_assist.references.extract_sandbox as mod

    def _slow(mime, data):
        import time

        time.sleep(5)
        return []

    monkeypatch.setattr(mod, "_extract_in_child", _slow, raising=False)
    with pytest.raises(ExtractError):
        extract_text_bounded(mime="text/markdown", data=b"x", timeout_seconds=0.5)
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run pytest tests/proposal_assist/references/test_extract_sandbox.py -v`
Expected: FAIL — `ModuleNotFoundError: extract_sandbox`.

- [ ] **Step 4: Implement the sandbox**

```python
# src/call_manager/proposal_assist/references/extract_sandbox.py
"""Run untrusted file text-extraction in a separate, killable process.

extract_text() (pdfminer / python-docx / openpyxl / striprtf / json) runs
synchronously in the request path. Byte/page caps bound memory but not CPU
time, so a crafted-but-small file can pin the single app process. We run the
extractor in a `multiprocessing` child and terminate() it on timeout — the
only mechanism that actually stops CPU-bound work (a thread cannot be
force-killed). On timeout we raise ExtractError, which ingest Phase 2 already
maps to text_status='failed'.
"""
from __future__ import annotations

import multiprocessing as mp

from call_manager.proposal_assist.references.extract import ExtractError, extract_text

# Spawn (not fork) so the child is a clean interpreter with no inherited DB
# connections / open sockets from the parent — important under a forking app.
_CTX = mp.get_context("spawn")


def _extract_in_child(mime: str, data: bytes):
    # Indirection point so tests can monkeypatch a slow/raising extractor.
    return extract_text(mime=mime, data=data)


def _worker(mime: str, data: bytes, q: "mp.Queue") -> None:
    try:
        q.put(("ok", _extract_in_child(mime, data)))
    except Exception as e:  # noqa: BLE001 - surfaced to the parent verbatim
        q.put(("err", f"{type(e).__name__}: {e}"))


def extract_text_bounded(
    *, mime: str, data: bytes, timeout_seconds: float
) -> list[tuple[str, str]]:
    """extract_text() with a hard wall-clock bound.

    Raises ExtractError on timeout (child is terminated) or if the child
    crashes. Otherwise returns the same [(section_path, text), ...] as
    extract_text().
    """
    q: mp.Queue = _CTX.Queue()
    proc = _CTX.Process(target=_worker, args=(mime, data, q), daemon=True)
    proc.start()
    proc.join(timeout_seconds)
    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        if proc.is_alive():  # pragma: no cover - terminate is reliable
            proc.kill()
        raise ExtractError(f"extraction exceeded {timeout_seconds:.0f}s budget")
    try:
        status, payload = q.get_nowait()
    except Exception as e:  # empty queue == child died without putting
        raise ExtractError("extraction process crashed") from e
    if status == "err":
        raise ExtractError(payload)
    return payload
```

> Confirm `ExtractError` is defined in `extract.py`: `grep -n "class ExtractError" src/call_manager/proposal_assist/references/extract.py`. The migration-extraction reviewer confirmed it exists and that Phase 2's `except Exception` maps it to `failed`.

- [ ] **Step 5: Run the sandbox tests**

Run: `uv run pytest tests/proposal_assist/references/test_extract_sandbox.py -v`
Expected: both PASS.

- [ ] **Step 6: Wire it into Phase 2**

In `src/call_manager/proposal_assist/references/ingest.py`, change the call at line 166 from:

```python
        sections = extract_text(mime=mime_type, data=data)
```
to:
```python
        sections = extract_text_bounded(
            mime=mime_type,
            data=data,
            timeout_seconds=settings.reference_extract_timeout_seconds,
        )
```

Add the import near the existing `extract_text` import at the top of `ingest.py`:
```python
from call_manager.proposal_assist.references.extract_sandbox import extract_text_bounded
```
Confirm `settings` is in scope in `ingest_bytes` (the function receives it / has access — `grep -n "settings" src/call_manager/proposal_assist/references/ingest.py | head`). If `ingest_bytes` does not already receive `settings`, thread it through from the two call sites ([references.py:220](../../../src/call_manager/proposal_assist/web/references.py#L220) and [references.py:316](../../../src/call_manager/proposal_assist/web/references.py#L316)) which both have `request.app.state.settings`. If `extract_text` is now unused in `ingest.py`, drop its import.

- [ ] **Step 7: Run the reference-ingest suite**

Run: `uv run pytest tests/proposal_assist/references/ -q`
Expected: green. The normal-path ingest tests still pass (extraction succeeds well under 30s).

- [ ] **Step 8: Commit**

```bash
git add src/call_manager/proposal_assist/references/extract_sandbox.py src/call_manager/proposal_assist/references/ingest.py src/call_manager/config.py tests/proposal_assist/references/test_extract_sandbox.py
git commit -m "fix(security): hard wall-clock kill for untrusted file extraction"
```

---

## Task 3: Catch-all exception handler

`main.py` registers only `@app.exception_handler(HTTPException)` ([main.py:292](../../../src/call_manager/main.py#L292)). Any non-`HTTPException` (stray `ValueError`, DB error) falls through to Starlette's default bare 500 and bypasses the styled `error.html`. Add a catch-all that renders the styled page for browsers and JSON otherwise, logging server-side with no internals in the body. FastAPI dispatches the more specific `HTTPException` handler first, so 401→/login and `_HTML_ERROR_STATUSES` behavior is untouched.

**Files:**
- Modify: `src/call_manager/main.py` (add a handler right after the `HTTPException` handler ending at line 316)
- Test: `tests/test_exception_handler.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_exception_handler.py
"""An unhandled (non-HTTPException) error returns a styled 500 for browsers
and JSON for API callers, never a traceback. See spec 2026-06-10 §9."""
from fastapi import APIRouter
from fastapi.testclient import TestClient

from call_manager.main import create_app


def _app_with_boom(monkeypatch, pg_url):
    # Build the app the same way other tests do, then mount a route that raises.
    from tests.conftest import _set_http_env  # existing helper used by test_security_headers

    _set_http_env(monkeypatch, pg_url)
    app = create_app()
    boom = APIRouter()

    @boom.get("/_boom")
    def _boom():
        raise ValueError("internal detail that must not leak")

    app.include_router(boom)
    return app


def test_unhandled_error_html_is_styled_500(monkeypatch, pg_url):
    app = _app_with_boom(monkeypatch, pg_url)
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/_boom", headers={"accept": "text/html"})
    assert r.status_code == 500
    assert "internal detail that must not leak" not in r.text
    assert "<html" in r.text.lower()  # styled error.html, not a bare string


def test_unhandled_error_json_for_api(monkeypatch, pg_url):
    app = _app_with_boom(monkeypatch, pg_url)
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/_boom")  # no text/html in Accept
    assert r.status_code == 500
    assert r.json() == {"detail": "internal error"}
```

> Match the existing test bootstrap. `tests/test_security_headers.py` shows the canonical `_set_http_env(monkeypatch, pg_url)` + `create_app()` pattern and the `pg_url` fixture — reuse exactly that. If the helper name differs, copy the env-setup lines from `test_security_headers.py`.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_exception_handler.py -v`
Expected: FAIL — without the handler, `TestClient(raise_server_exceptions=False)` returns Starlette's default 500 whose body is `Internal Server Error` (not styled HTML, and the JSON test's body isn't `{"detail":"internal error"}`).

- [ ] **Step 3: Add the catch-all handler**

In `src/call_manager/main.py`, immediately after the `HTTPException` handler (after line 316, before `app.state.settings = settings`), add:

```python
    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception):
        # Catch-all for non-HTTPException errors. FastAPI dispatches the more
        # specific HTTPException handler above first, so this only fires for
        # genuinely unhandled errors. Log the detail server-side; never put it
        # in the response body.
        import logging

        logging.getLogger(__name__).exception(
            "unhandled exception", extra={"event": "unhandled_exception"}
        )
        wants_html = "text/html" in request.headers.get("accept", "")
        if wants_html:
            return app.state.templates.TemplateResponse(
                request=request,
                name="error.html",
                context={"status_code": 500, "message": "Something went wrong."},
                status_code=500,
            )
        return JSONResponse(status_code=500, content={"detail": "internal error"})
```

`Request` and `JSONResponse` are already imported in `main.py` (used by the existing handler). Confirm with `grep -n "JSONResponse\|from starlette\|from fastapi" src/call_manager/main.py | head`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_exception_handler.py -v`
Expected: both PASS.

- [ ] **Step 5: Confirm no regression to existing error handling**

Run: `uv run pytest tests/test_security_headers.py tests/ -k "error or 404 or unauthor or login" -q`
Expected: green — the `HTTPException` paths (401→/login, styled 404/403) are unchanged because the specific handler still wins.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/main.py tests/test_exception_handler.py
git commit -m "fix(security): catch-all exception handler renders styled 500, no leak"
```

---

## Task 4: Deployment hardening template + checklist

No code; ships the production config template and the operator checklist the security review called for (real `ALLOWED_EMAIL_DOMAINS`, TLS proxy, `TRUSTED_PROXY`, strong `SESSION_SECRET`, email transport).

**Files:**
- Create: `.env.production.example`
- Create: `docs/deployment-hardening.md`

- [ ] **Step 1: Write `.env.production.example`**

```bash
# .env.production.example — copy to .env on the production host and fill in.
# Do NOT commit the filled-in file. Generate secrets fresh per environment.

# --- Database ---
DATABASE_URL=postgresql+psycopg://call_manager:CHANGE_ME@db-host:5432/call_manager

# --- Session signing (REQUIRED, >=32 chars in prod; the app refuses to start
#     with a weak/placeholder value when APP_BASE_URL is https). Generate with:
#       python -c "import secrets; print(secrets.token_urlsafe(48))"
SESSION_SECRET=CHANGE_ME_64_RANDOM_CHARS

# --- Access boundary: ONLY real institutional domains. NEVER include public
#     providers like gmail.com in production — login self-service is gated on
#     this list (P2 adds admin approval on top, but keep this tight regardless).
ALLOWED_EMAIL_DOMAINS=ki.se
INITIAL_ADMIN_EMAIL=first.admin@ki.se

# --- Public URL the BROWSER sees. https:// enables Secure cookies + HSTS,
#     forces the strong-secret check, and refuses console-email fallback.
APP_BASE_URL=https://call-manager.example.org

# --- Reverse-proxy topology. Set true ONLY behind a proxy you control that
#     OVERWRITES X-Forwarded-For; otherwise per-IP rate limiting buckets every
#     client under the proxy IP. TRUSTED_PROXY_HOPS = number of trusted hops.
TRUSTED_PROXY=true
TRUSTED_PROXY_HOPS=1

# --- Email transport (REQUIRED in prod — the app refuses to start on https
#     without one, to avoid logging magic links to stderr). Pick ONE:
RESEND_API_KEY=
# or
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
EMAIL_FROM=no-reply@call-manager.example.org

# --- LLM endpoints (operator-config; user input never sets these) ---
OLLAMA_URL=http://ollama-host:11434
GEMMA_MODEL=gemma4:e4b-it-q4_K_M
EMBEDDING_MODEL=mxbai-embed-large
EMBEDDING_DIM=1024
# Cloud drafting (proposal-assist). Leave unset to disable cloud turns.
ANTHROPIC_API_KEY=

# --- Untrusted-file extraction wall-clock budget (P1 Task 2) ---
REFERENCE_EXTRACT_TIMEOUT_SECONDS=30
```

- [ ] **Step 2: Write `docs/deployment-hardening.md`**

```markdown
# Deployment hardening checklist

Run through this before exposing call-manager outside the hospital network.
See `docs/superpowers/specs/2026-06-10-user-system-and-security-hardening-design.md` §9.

## Network topology
- [ ] TLS terminates at a reverse proxy (Nginx/Traefik/Apache). uvicorn binds to
      loopback only: `uv run uvicorn call_manager.main:create_app --factory
      --host 127.0.0.1 --port 8000 --workers 1`. Never expose uvicorn directly.
      (`--workers 1` is mandatory: the in-process APScheduler is single-instance.)
- [ ] The proxy OVERWRITES the inbound `X-Forwarded-For` header (does not append
      a client-supplied value), and `client_max_body_size` is set at the proxy.

## Config (`.env`, from `.env.production.example`)
- [ ] `APP_BASE_URL` is `https://…` (enables Secure cookies, HSTS, strong-secret
      enforcement, and refuses console-email fallback).
- [ ] `SESSION_SECRET` is a fresh 48+ char random value, unique to this env.
- [ ] `ALLOWED_EMAIL_DOMAINS` contains ONLY real institutional domains — no
      `gmail.com` or other public providers.
- [ ] `TRUSTED_PROXY=true` and `TRUSTED_PROXY_HOPS` matches the real hop count.
- [ ] `RESEND_API_KEY` or `SMTP_*` is set (the app refuses to start on https
      without one).
- [ ] `REFERENCE_EXTRACT_TIMEOUT_SECONDS` is set (default 30).

## Verify after deploy
- [ ] `curl -sI https://…/login` shows `strict-transport-security`,
      `content-security-policy`, `x-frame-options: DENY`,
      `referrer-policy: same-origin`, and a `Secure` session cookie on login.
- [ ] An unknown-domain email at `/login` does not grant access.
- [ ] App starts cleanly (no "refusing to start" RuntimeError in logs).
```

- [ ] **Step 3: Verify the template parses as valid env**

Run: `set -a; . ./.env.production.example; set +a; echo "$APP_BASE_URL"`
Expected: prints `https://call-manager.example.org` with no shell errors.

- [ ] **Step 4: Commit**

```bash
git add .env.production.example docs/deployment-hardening.md
git commit -m "docs(security): production env template + deployment hardening checklist"
```

---

## Final verification

- [ ] **Run the full suite**

Run: `uv run pytest -q`
Expected: all pass (the count should match the pre-P1 baseline plus the new tests; if any pre-existing turn test now 403s on the disclosure gate, add the `/disclosure` precondition per Task 1 Step 5).

- [ ] **Manual smoke (optional but recommended)** — start the app against the dev DB, create a workspace, and confirm: posting a turn before clicking "I understand" on the modal is rejected; after acknowledging, it works. Upload a normal PDF reference and confirm it still indexes.

## Self-review notes (coverage vs spec §9)
- §9.1 disclosure gate → Task 1.
- §9.2 file-parse timeout → Task 2 (subprocess hard-kill, the spec's "wall-clock kill").
- §9.3 catch-all handler → Task 3.
- §9.4 deployment hardening → Task 4.
