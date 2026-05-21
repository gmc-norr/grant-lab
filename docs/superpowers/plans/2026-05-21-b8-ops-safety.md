# B8 — Operational Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Three production-safety controls on top of proposal-assist: a `DRAFTING_ENABLED` kill switch, a global per-day token ceiling across all workspaces, and an `/admin/drafting` panel for visibility.

**Architecture:**

- **Kill switch:** new boolean Setting `drafting_enabled` (env `DRAFTING_ENABLED`, default `True`). Checked at two enforcement points: (1) POST `/workspaces/<id>/turns` returns 503 with a clear message when disabled — operators flip this to "stop accepting new work"; (2) the drain tick logs + no-ops so queued turns wait for re-enable rather than crashing.
- **Global cap:** new Settings `global_daily_token_cap_input` / `_output` (env `GLOBAL_DAILY_TOKEN_CAP_INPUT` / `_OUTPUT`, defaults 5_000_000 / 1_000_000 — 10× the per-workspace default). New `check_global_daily_token_cap` in `caps.py` sums `ChatTurn.tokens_in/out` across **all** workspaces for today (Stockholm). Called from `submit_user_turn` after the workspace FOR UPDATE row lock but before the per-workspace cap check. Raises new `GlobalTokenCapExceeded` exception. Web layer catches → 429 with `X-Token-Cap: global` header so the UI distinguishes from per-workspace.
- **/admin/drafting panel:** new admin route + template. Shows global today's totals against the cap, per-workspace usage breakdown, kill-switch state (read-only — UI toggling deferred to a follow-up), and the last 50 `cloud_turn_failed` / `cloud_turn_cancelled` events across all workspaces. Mirrors the existing `/admin` page's `_panel_context` helper pattern.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Postgres 16, pytest + testcontainers.

**Concurrency note (intentional):** the global cap is read-only without serialization (no global advisory lock). Two concurrent submits in different workspaces can both pass a near-cap global check and queue, overshooting by one turn each. This is acceptable because the cap is a **guardrail** against runaway spend, not an exact ledger — and a global advisory lock would bottleneck all submits org-wide. Documented in the docstring. If precise enforcement becomes a requirement, a per-day advisory lock on a stable key (e.g. `hashtext("global-cap:" || stockholm_date)`) is the upgrade path.

---

## File Structure

**Modify:**
- `src/call_manager/config.py` — add `drafting_enabled`, `global_daily_token_cap_input/output` Settings fields.
- `src/call_manager/proposal_assist/turns/caps.py` — add `GlobalTokenCapExceeded` exception + `check_global_daily_token_cap` function.
- `src/call_manager/proposal_assist/turns/submit.py` — call global cap check after FOR UPDATE row lock; raise `GlobalTokenCapExceeded`.
- `src/call_manager/proposal_assist/web/workspaces.py` — `post_turn` early kill-switch check (503); catch `GlobalTokenCapExceeded` → 429 with header.
- `src/call_manager/proposal_assist/turns/drain.py` — accept `drafting_enabled: bool = True`; log + no-op when False.
- `src/call_manager/scheduler.py` — pass `settings.drafting_enabled` into drain closure.
- `src/call_manager/web/routes/admin.py` — add `/admin/drafting` route + helper functions for the panel.
- `src/call_manager/web/templates/admin_drafting.html` — new panel template (mirrors admin.html structure).
- `src/call_manager/web/templates/admin.html` — add link to `/admin/drafting`.
- `.env.example` — document the three new env vars in the proposal-assist section.

**Create:**
- `tests/proposal_assist/test_kill_switch.py` — POST /turns returns 503 when disabled; drain skips when disabled.
- `tests/proposal_assist/test_global_cap.py` — submit returns 429 when global cap reached even though per-workspace cap is fine; convergent across workspaces.
- `tests/test_web_admin_drafting_panel.py` — admin renders / non-admin 403 / shows kill-switch state / shows global totals / shows recent failed events.

**Out of scope (genuine future work, document in plan):**
- UI-driven kill-switch toggle (changing env requires a redeploy; in v1 toggling needs an ops action). A POST /admin/drafting/toggle endpoint that writes to a `system_state` table is a backlog item.
- Per-workspace cap override widget on the panel (the column already exists; the UI is a one-form addition deferred).
- Global cap precision via advisory lock (acceptable overshoot in v1).
- Vision-token-specific dashboard (sum of attachment tokens per workspace) — surfaced in the panel as part of the per-workspace breakdown only if cheap to compute via existing data; otherwise backlog.
- Alert hooks (Slack/email when kill switch trips or global cap hit) — separate notification feature.

---

## Task 1: Kill switch — settings + web enforcement + drain skip

**Files:**
- Modify: `src/call_manager/config.py`
- Modify: `src/call_manager/proposal_assist/web/workspaces.py:`(top of `post_turn`)
- Modify: `src/call_manager/proposal_assist/turns/drain.py`
- Modify: `src/call_manager/scheduler.py`
- Modify: `.env.example`
- Create: `tests/proposal_assist/test_kill_switch.py`

### Step 1: Write the failing kill-switch tests

Create `tests/proposal_assist/test_kill_switch.py`:

```python
"""DRAFTING_ENABLED=false: POST /turns refuses with 503; drain
tick logs + no-ops so queued turns wait for re-enable."""

import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import ChatTurn, TurnJob, User, Workspace
from call_manager.main import create_app


def _build_app_kill(monkeypatch, pg_url, *, drafting_enabled: bool):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ENABLED", "true" if drafting_enabled else "false")
    return create_app()


def _authed_client(app, *, user_id: int) -> TestClient:
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=user_id))
    return client


def test_post_turn_returns_503_when_kill_switch_off(
    monkeypatch, pg_url, pg_session_factory
):
    app = _build_app_kill(monkeypatch, pg_url, drafting_enabled=False)
    with pg_session_factory() as s:
        owner = User(email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="Test")
        s.add(ws)
        s.commit()
        uid, wsid = owner.id, ws.id

    client = _authed_client(app, user_id=uid)
    try:
        resp = client.post(f"/workspaces/{wsid}/turns", data={"content": "hi"})
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 503
    assert "drafting" in resp.text.lower()
    # No ChatTurn or TurnJob persisted.
    with pg_session_factory() as s:
        turns = list(s.scalars(select(ChatTurn).where(ChatTurn.workspace_id == wsid)))
        jobs = list(s.scalars(select(TurnJob).where(TurnJob.workspace_id == wsid)))
    assert turns == []
    assert jobs == []


def test_post_turn_works_when_kill_switch_on(
    monkeypatch, pg_url, pg_session_factory
):
    """Sanity: defaulted/explicitly-True kill switch lets submits through."""
    app = _build_app_kill(monkeypatch, pg_url, drafting_enabled=True)
    with pg_session_factory() as s:
        owner = User(email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="Test")
        s.add(ws)
        s.commit()
        uid, wsid = owner.id, ws.id

    client = _authed_client(app, user_id=uid)
    try:
        resp = client.post(f"/workspaces/{wsid}/turns", data={"content": "hi"})
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    with pg_session_factory() as s:
        jobs = list(s.scalars(select(TurnJob).where(TurnJob.workspace_id == wsid)))
    assert len(jobs) == 1


@pytest.mark.asyncio
async def test_drain_tick_noops_when_kill_switch_off(
    pg_session_factory,
    _settings_env,
):
    """When DRAFTING_ENABLED=False, drain_turn_queue_tick must NOT
    dispatch even if a queued turn exists. The turn stays queued so
    re-enabling resumes work."""
    from datetime import UTC, datetime

    from call_manager.proposal_assist.drafting.controller import WorkspaceController
    from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
    from call_manager.proposal_assist.drafting.registry import SubprocessRegistry
    from call_manager.proposal_assist.turns.drain import drain_turn_queue_tick
    from tests.proposal_assist.conftest import make_user, make_workspace

    adapter = FakeAgentAdapter()
    # No scripted turn: if drain dispatches by mistake, the adapter
    # raises and the test sees a non-zero dispatched count.
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        user_turn = ChatTurn(workspace_id=ws.id, role="user", content="hi")
        s.add(user_turn)
        s.flush()
        s.add(
            TurnJob(
                workspace_id=ws.id,
                user_turn_id=user_turn.id,
                status="queued",
                queued_at=datetime.now(UTC),
                submitted_by_user_id=user.id,
            )
        )
        s.commit()
        wsid = ws.id

    dispatched = await drain_turn_queue_tick(
        session_factory=pg_session_factory,
        controller=controller,
        drafting_enabled=False,
    )
    assert dispatched == 0
    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
    assert job.status == "queued", "queued turns must wait for re-enable"
```

Run: `uv run pytest tests/proposal_assist/test_kill_switch.py -v` — Expected FAIL (`drafting_enabled` not a Settings field yet; `drain_turn_queue_tick` doesn't accept the kwarg).

### Step 2: Add `drafting_enabled` Setting

In `src/call_manager/config.py`, find the proposal-assist Settings block (after `anthropic_api_key` around line 57). Add:

```python
    drafting_enabled: bool = Field(
        default=True,
        alias="DRAFTING_ENABLED",
        description=(
            "Kill switch for the proposal-assist drafting subsystem. "
            "When False, POST /workspaces/<id>/turns returns 503 and "
            "the drain tick is a no-op (queued turns wait for re-enable). "
            "Toggle requires app restart; UI-driven toggling is deferred."
        ),
    )
```

### Step 3: Wire the kill switch into POST /turns

In `src/call_manager/proposal_assist/web/workspaces.py:post_turn` (the multipart turn submission route), at the TOP of the function body (before owner check is fine — kill switch is the most fundamental gate):

```python
    if not request.app.state.settings.drafting_enabled:
        return HTMLResponse(
            "<p>Drafting is currently disabled by an administrator. "
            "Please try again later.</p>",
            status_code=503,
        )
```

(Match the existing handler's style — no f-string interpolation here so no XSS to worry about.)

### Step 4: Wire kill switch into the drain tick

In `src/call_manager/proposal_assist/turns/drain.py:drain_turn_queue_tick`, add an optional kwarg:

```python
async def drain_turn_queue_tick(
    *,
    session_factory: sessionmaker,
    controller: WorkspaceController,
    drafting_enabled: bool = True,
) -> int:
```

At the top of the function body, before any DB query:

```python
    if not drafting_enabled:
        logger.info("drain_turn_queue_tick: skipped (drafting_enabled=False)")
        return 0
```

### Step 5: Pass `drafting_enabled` from the scheduler

In `src/call_manager/scheduler.py`, find the `_turn_drain_tick` closure (which calls `drain_turn_queue_tick`). Update the call:

```python
        await drain_turn_queue_tick(
            session_factory=session_factory,
            controller=workspace_controller,
            drafting_enabled=settings.drafting_enabled,
        )
```

(The settings object is already in scope per the existing closure pattern; verify with the surrounding code.)

### Step 6: Document in .env.example

Add to the proposal-assist section of `.env.example`:

```bash
# Kill switch for the proposal-assist drafting subsystem. When false,
# POST /workspaces/<id>/turns returns 503 and the drain tick is a
# no-op (queued turns wait for re-enable). Toggle requires app
# restart; UI-driven toggling is deferred.
DRAFTING_ENABLED=true
```

### Step 7: Run tests + commit

```bash
uv run pytest tests/proposal_assist/test_kill_switch.py -v
uv run pytest -q
git add ...
git commit -m "feat(b8): DRAFTING_ENABLED kill switch on web + drain

POST /workspaces/<id>/turns returns 503 when disabled; drain
tick is a no-op so queued turns wait for re-enable. Operator
flips the env and restarts to drain the queue safely."
```

---

## Task 2: Global daily token cap

**Files:**
- Modify: `src/call_manager/config.py` — add `global_daily_token_cap_input/output`.
- Modify: `src/call_manager/proposal_assist/turns/caps.py` — add `GlobalTokenCapExceeded` + `check_global_daily_token_cap`.
- Modify: `src/call_manager/proposal_assist/turns/submit.py` — call global check after FOR UPDATE lock; raise on overshoot.
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` — catch `GlobalTokenCapExceeded` → 429 with `X-Token-Cap: global` header.
- Modify: `.env.example`
- Create: `tests/proposal_assist/test_global_cap.py`

### Step 1: Write the failing global-cap tests

Create `tests/proposal_assist/test_global_cap.py`:

```python
"""Global daily cap: when ALL workspaces' today usage sums above the
global ceiling, no new submit is accepted regardless of per-workspace
remaining budget. The cap is a guardrail against runaway org-wide
spend; small overshoot from concurrent submits is acceptable."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from call_manager.config import Settings
from call_manager.db.models import ChatTurn, TurnJob
from call_manager.proposal_assist.turns.caps import GlobalTokenCapExceeded
from call_manager.proposal_assist.turns.submit import submit_user_turn
from tests.proposal_assist.conftest import make_user, make_workspace


def test_check_global_cap_sums_across_workspaces(
    pg_session_factory, _settings_env, monkeypatch
):
    """Two workspaces' assistant rows from today must both count
    toward the global sum returned by check_global_daily_token_cap."""
    from call_manager.proposal_assist.turns.caps import check_global_daily_token_cap

    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "1000")
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_OUTPUT", "1000")
    settings = Settings()

    with pg_session_factory() as s:
        owner = make_user(s)
        s.commit()
        ws_a = make_workspace(s, owner=owner, title="A")
        ws_b = make_workspace(s, owner=owner, title="B")
        s.commit()
        s.add_all(
            [
                ChatTurn(
                    workspace_id=ws_a.id,
                    role="assistant",
                    content={"type": "text", "text": "x"},
                    tokens_in=400,
                    tokens_out=10,
                ),
                ChatTurn(
                    workspace_id=ws_b.id,
                    role="assistant",
                    content={"type": "text", "text": "y"},
                    tokens_in=300,
                    tokens_out=20,
                ),
            ]
        )
        s.commit()

    with pg_session_factory() as s:
        result = check_global_daily_token_cap(s, settings=settings)
    assert result.used_in == 700
    assert result.used_out == 30
    assert result.allowed is True


def test_submit_raises_global_cap_exceeded(
    pg_session_factory, _settings_env, monkeypatch
):
    """When the global sum is already at/above the cap, a submit on
    a workspace with PLENTY of per-workspace headroom still fails."""
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "500")
    # Per-workspace stays at default (500_000) — way above what we'll seed.
    settings = Settings()

    with pg_session_factory() as s:
        owner = make_user(s)
        s.commit()
        ws_a = make_workspace(s, owner=owner, title="A")
        ws_b = make_workspace(s, owner=owner, title="B")
        s.commit()
        # Seed: ws_a already used 500 input today via a completed turn.
        s.add(
            ChatTurn(
                workspace_id=ws_a.id,
                role="assistant",
                content={"type": "text", "text": "x"},
                tokens_in=500,
                created_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        s.commit()
        wsid_b, owner_id = ws_b.id, owner.id

    # Submit on ws_b should fail with GlobalTokenCapExceeded even
    # though ws_b's per-workspace usage is 0.
    with pg_session_factory() as s, pytest.raises(GlobalTokenCapExceeded):
        submit_user_turn(
            session=s,
            workspace_id=wsid_b,
            content_text="hi",
            submitted_by_user_id=owner_id,
            settings=settings,
        )


def test_post_turn_returns_429_with_global_header(
    monkeypatch, pg_url, pg_session_factory
):
    """The HTTP layer surfaces global cap exhaustion as 429 with the
    X-Token-Cap: global header so the UI can distinguish it from a
    per-workspace 429."""
    import io

    from fastapi.testclient import TestClient

    from call_manager.auth.session_cookie import SessionSigner
    from call_manager.db.models import User, Workspace
    from call_manager.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "100")
    app = create_app()

    with pg_session_factory() as s:
        owner = User(email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="Test")
        s.add(ws)
        s.flush()
        s.add(
            ChatTurn(
                workspace_id=ws.id,
                role="assistant",
                content={"type": "text", "text": "x"},
                tokens_in=150,  # already over the 100 global cap
            )
        )
        s.commit()
        uid, wsid = owner.id, ws.id

    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    try:
        signer: SessionSigner = app.state.session_signer
        client.cookies.set("session", signer.make(user_id=uid))
        resp = client.post(f"/workspaces/{wsid}/turns", data={"content": "hi"})
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 429
    assert resp.headers.get("X-Token-Cap") == "global"
    assert "global" in resp.text.lower()
```

Run: `uv run pytest tests/proposal_assist/test_global_cap.py -v` — Expected FAIL (no global cap implementation yet).

### Step 2: Add global cap Settings

In `src/call_manager/config.py`, after the per-workspace cap fields:

```python
    global_daily_token_cap_input: int = Field(
        default=5_000_000,
        alias="GLOBAL_DAILY_TOKEN_CAP_INPUT",
        description=(
            "Org-wide ceiling on total input tokens (sum across all "
            "workspaces) per Europe/Stockholm day. Defaults to 10x the "
            "per-workspace cap so a single runaway workspace can't bill "
            "the whole budget. Enforced before per-workspace cap; a small "
            "overshoot from concurrent submits is acceptable (guardrail, "
            "not exact ledger)."
        ),
    )
    global_daily_token_cap_output: int = Field(
        default=1_000_000,
        alias="GLOBAL_DAILY_TOKEN_CAP_OUTPUT",
        description=(
            "Org-wide ceiling on total output tokens (sum across all "
            "workspaces) per Europe/Stockholm day. Defaults to 10x the "
            "per-workspace cap."
        ),
    )
```

### Step 3: Implement `GlobalTokenCapExceeded` + `check_global_daily_token_cap`

In `src/call_manager/proposal_assist/turns/caps.py`, after the existing `TokenCapExceeded`:

```python
class GlobalTokenCapExceeded(RuntimeError):
    """Raised when the org-wide daily-token ceiling is reached.

    Distinct from TokenCapExceeded so the web layer can differentiate
    'your workspace ran out' vs 'the whole org ran out'."""

    def __init__(
        self,
        *,
        used_in: int,
        used_out: int,
        cap_in: int,
        cap_out: int,
        reason: str,
    ) -> None:
        super().__init__(
            f"global {reason}: used_in={used_in} used_out={used_out} "
            f"cap_in={cap_in} cap_out={cap_out}"
        )
        self.used_in = used_in
        self.used_out = used_out
        self.cap_in = cap_in
        self.cap_out = cap_out
        self.reason = reason
```

After the existing `check_daily_token_cap`:

```python
def check_global_daily_token_cap(
    session: Session,
    *,
    settings: Settings,
) -> CapCheckResult:
    """Sum today's input/output tokens across ALL workspaces against the
    global ceiling. Read-only; caller (submit_user_turn) raises.

    Concurrency: NO global advisory lock. The cap is a guardrail
    against runaway org-wide spend, not an exact ledger. Two concurrent
    submits in different workspaces can both pass a near-cap global
    check and queue, overshooting by one turn each. Acceptable because
    a global lock would bottleneck all submits org-wide. If precise
    enforcement becomes a requirement, add `pg_try_advisory_xact_lock`
    on a stable per-day key (e.g. hashtext('global-cap:' || YYYYMMDD)).
    """
    day_start_utc = _stockholm_day_start_utc()
    sums = session.execute(
        select(
            func.coalesce(func.sum(ChatTurn.tokens_in), 0),
            func.coalesce(func.sum(ChatTurn.tokens_out), 0),
        ).where(ChatTurn.created_at >= day_start_utc)
    ).one()
    used_in, used_out = int(sums[0]), int(sums[1])
    cap_in = settings.global_daily_token_cap_input
    cap_out = settings.global_daily_token_cap_output
    reason: str | None = None
    if used_in >= cap_in:
        reason = "input_cap_exceeded"
    elif used_out >= cap_out:
        reason = "output_cap_exceeded"
    return CapCheckResult(
        allowed=reason is None,
        used_in=used_in,
        used_out=used_out,
        cap_in=cap_in,
        cap_out=cap_out,
        reason=reason,
    )
```

### Step 4: Wire global cap into `submit_user_turn`

In `src/call_manager/proposal_assist/turns/submit.py`, after the `FOR UPDATE` row lock acquired (currently around line 215-219) and BEFORE the existing per-workspace `check_daily_token_cap` call (line ~270):

```python
    # Global cap check (org-wide guardrail). Read-only — no global
    # advisory lock. See check_global_daily_token_cap docstring.
    global_cap = check_global_daily_token_cap(session, settings=settings)
    if not global_cap.allowed:
        raise GlobalTokenCapExceeded(
            used_in=global_cap.used_in,
            used_out=global_cap.used_out,
            cap_in=global_cap.cap_in,
            cap_out=global_cap.cap_out,
            reason=global_cap.reason or "cap_exceeded",
        )
```

Update the imports at the top of `submit.py`:

```python
from call_manager.proposal_assist.turns.caps import (
    GlobalTokenCapExceeded,
    TokenCapExceeded,
    check_daily_token_cap,
    check_global_daily_token_cap,
)
```

### Step 5: Catch in web handler → 429 with header

In `src/call_manager/proposal_assist/web/workspaces.py:post_turn`, find the existing `except TokenCapExceeded` block. Add a sibling block that handles `GlobalTokenCapExceeded`:

```python
    except GlobalTokenCapExceeded as e:
        session.rollback()
        _rollback_created_uploads()
        return HTMLResponse(
            f"<p>Org-wide daily token cap exceeded: {_html.escape(e.reason)}. "
            f"Used {e.used_in} input / {e.used_out} output today; "
            f"cap is {e.cap_in} / {e.cap_out}. Try again after midnight "
            f"Europe/Stockholm or ask an admin to raise the cap.</p>",
            status_code=429,
            headers={"X-Token-Cap": "global"},
        )
```

(Place BEFORE `except TokenCapExceeded` since the per-workspace path is the more common one — order matters only for readability; they're distinct exception types.)

Update the import at top:

```python
from call_manager.proposal_assist.turns.caps import (
    GlobalTokenCapExceeded,
    TokenCapExceeded,
)
```

### Step 6: Document in .env.example

Add after the per-workspace cap section:

```bash
# Org-wide daily-token ceiling (sum across all workspaces). Defaults
# to 10x the per-workspace cap so one runaway workspace can't bill
# the whole budget. Guardrail, not exact ledger — small overshoot
# from concurrent submits is acceptable. Enforced before the per-
# workspace cap. UI surfaces a 429 with X-Token-Cap: global header.
GLOBAL_DAILY_TOKEN_CAP_INPUT=5000000
GLOBAL_DAILY_TOKEN_CAP_OUTPUT=1000000
```

### Step 7: Run tests + commit

```bash
uv run pytest tests/proposal_assist/test_global_cap.py -v
uv run pytest -q
git add ...
git commit -m "feat(b8): global daily token cap across all workspaces

New check_global_daily_token_cap + GlobalTokenCapExceeded enforce
an org-wide ceiling before the per-workspace cap. Web layer
surfaces as 429 with X-Token-Cap: global header so the UI can
distinguish from a per-workspace 429.

Concurrency: read-only check, no global advisory lock — overshoot
is acceptable because a global lock would bottleneck all submits."
```

---

## Task 3: /admin/drafting panel

**Files:**
- Modify: `src/call_manager/web/routes/admin.py` — add `/admin/drafting` route + helper.
- Create: `src/call_manager/web/templates/admin_drafting.html`
- Modify: `src/call_manager/web/templates/admin.html` — add link.
- Create: `tests/test_web_admin_drafting_panel.py`

### Step 1: Write the failing panel tests

Create `tests/test_web_admin_drafting_panel.py` (in `tests/` top level — admin tests live there per the recon):

```python
"""Owner-side admin panel for the proposal-assist drafting subsystem.

Mirrors the test pattern from test_web_admin_digest_panel.py:
- enter TestClient context (fires lifespan → bootstrap_admins)
- promote a test user to admin via direct DB write
- sign session cookie via SessionSigner
- assert page renders with expected fragments
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import (
    ChatTurn,
    TurnJob,
    User,
    Workspace,
    WorkspaceEvent,
)
from call_manager.main import create_app


def _build_app(monkeypatch, pg_url, *, drafting_enabled: bool = True):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ENABLED", "true" if drafting_enabled else "false")
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "1000")
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_OUTPUT", "500")
    return create_app()


def _admin_client(app, *, admin_email: str = "admin@ki.se") -> TestClient:
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    # Promote a user to admin AFTER lifespan runs (so bootstrap doesn't fight us).
    from sqlalchemy.orm import sessionmaker

    sf: sessionmaker = app.state.session_factory
    with sf() as s:
        u = s.scalar(select(User).where(User.email == admin_email))
        if u is None:
            u = User(email=admin_email, is_admin=True)
            s.add(u)
            s.commit()
            s.refresh(u)
        elif not u.is_admin:
            u.is_admin = True
            s.commit()
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_drafting_panel_returns_403_for_non_admin(
    monkeypatch, pg_url, pg_session_factory
):
    app = _build_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="user@ki.se", is_admin=False)
        s.add(u)
        s.commit()
        uid = u.id
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    try:
        signer: SessionSigner = app.state.session_signer
        client.cookies.set("session", signer.make(user_id=uid))
        resp = client.get("/admin/drafting")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403


def test_drafting_panel_renders_for_admin(
    monkeypatch, pg_url, pg_session_factory
):
    app = _build_app(monkeypatch, pg_url, drafting_enabled=True)
    client = _admin_client(app)
    try:
        resp = client.get("/admin/drafting")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    # Kill switch state visible.
    assert "Drafting enabled" in resp.text or "enabled: true" in resp.text.lower()
    # Global cap caption visible.
    assert "1000" in resp.text or "1,000" in resp.text  # input cap default we set


def test_drafting_panel_shows_kill_switch_off(
    monkeypatch, pg_url, pg_session_factory
):
    app = _build_app(monkeypatch, pg_url, drafting_enabled=False)
    client = _admin_client(app)
    try:
        resp = client.get("/admin/drafting")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    assert "disabled" in resp.text.lower()


def test_drafting_panel_shows_per_workspace_usage(
    monkeypatch, pg_url, pg_session_factory
):
    app = _build_app(monkeypatch, pg_url)

    with pg_session_factory() as s:
        owner = User(email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="VisibleInPanel")
        s.add(ws)
        s.flush()
        s.add(
            ChatTurn(
                workspace_id=ws.id,
                role="assistant",
                content={"type": "text", "text": "x"},
                tokens_in=42,
                tokens_out=7,
            )
        )
        s.commit()

    client = _admin_client(app)
    try:
        resp = client.get("/admin/drafting")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    assert "VisibleInPanel" in resp.text
    assert "42" in resp.text
    assert "7" in resp.text


def test_drafting_panel_shows_recent_failed_events(
    monkeypatch, pg_url, pg_session_factory
):
    app = _build_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = User(email="o@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="WithFails")
        s.add(ws)
        s.flush()
        for i in range(3):
            s.add(
                WorkspaceEvent(
                    workspace_id=ws.id,
                    kind="cloud_turn_failed",
                    actor_id=owner.id,
                    payload={
                        "turn_job_id": "00000000-0000-0000-0000-00000000000" + str(i),
                        "error": f"distinctive_test_error_{i}",
                    },
                )
            )
        s.commit()
    client = _admin_client(app)
    try:
        resp = client.get("/admin/drafting")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    # Distinctive error strings should surface in the recent-failures section.
    assert "distinctive_test_error_0" in resp.text
    assert "distinctive_test_error_2" in resp.text
```

Run: `uv run pytest tests/test_web_admin_drafting_panel.py -v` — Expected FAIL (route doesn't exist).

### Step 2: Add `/admin/drafting` route + helpers

In `src/call_manager/web/routes/admin.py`, add a new helper after the existing `_digest_panel_context` and `_embedding_coverage_context`:

```python
def _drafting_panel_context(session, settings) -> dict:
    """Aggregate everything the /admin/drafting page renders:
    - kill switch state (read from settings)
    - global cap usage today (input + output sums + caps)
    - per-workspace usage today (id, title, owner_email, used_in, used_out)
    - last 50 failed/cancelled cloud_turn events across workspaces"""
    from call_manager.db.models import (
        ChatTurn,
        User,
        Workspace,
        WorkspaceEvent,
    )
    from call_manager.proposal_assist.turns.caps import (
        check_global_daily_token_cap,
        _stockholm_day_start_utc,
    )

    global_cap = check_global_daily_token_cap(session, settings=settings)

    day_start_utc = _stockholm_day_start_utc()
    per_workspace_rows = list(
        session.execute(
            select(
                Workspace.id,
                Workspace.title,
                User.email.label("owner_email"),
                func.coalesce(func.sum(ChatTurn.tokens_in), 0).label("used_in"),
                func.coalesce(func.sum(ChatTurn.tokens_out), 0).label("used_out"),
            )
            .join(User, User.id == Workspace.owner_id)
            .outerjoin(
                ChatTurn,
                (ChatTurn.workspace_id == Workspace.id)
                & (ChatTurn.created_at >= day_start_utc),
            )
            .where(Workspace.soft_deleted_at.is_(None))
            .group_by(Workspace.id, Workspace.title, User.email)
            .order_by(func.coalesce(func.sum(ChatTurn.tokens_in), 0).desc())
        ).all()
    )

    recent_events = list(
        session.scalars(
            select(WorkspaceEvent)
            .where(
                WorkspaceEvent.kind.in_(
                    ["cloud_turn_failed", "cloud_turn_cancelled"]
                )
            )
            .order_by(WorkspaceEvent.created_at.desc())
            .limit(50)
        ).all()
    )

    return {
        "drafting_enabled": settings.drafting_enabled,
        "global_cap": global_cap,
        "per_workspace": per_workspace_rows,
        "recent_events": recent_events,
    }
```

Then add the route handler (after the existing `/admin` GET route):

```python
@router.get("/drafting", response_class=HTMLResponse)
def admin_drafting_panel(
    request: Request,
    user=Depends(require_admin),  # noqa: B008
    session: Session = Depends(get_db_dependency),  # noqa: B008
) -> HTMLResponse:
    """Read-only admin panel for the proposal-assist drafting subsystem.

    Toggling the kill switch / overriding the global cap from this UI
    is intentionally NOT implemented in v1 (requires app restart). The
    panel surfaces state so an operator can decide whether to restart
    with a new env."""
    context = _drafting_panel_context(session, request.app.state.settings)
    context["request"] = request
    context["user"] = user
    return request.app.state.templates.TemplateResponse(
        "admin_drafting.html", context
    )
```

(Verify the imports needed at top of admin.py: `func`, `select`, `User`, `Workspace`, `WorkspaceEvent`, `ChatTurn` if not already there.)

### Step 3: Create the panel template

Create `src/call_manager/web/templates/admin_drafting.html`. Mirror the structure of the existing `admin.html` (which extends `base.html`):

```html
{% extends "base.html" %}

{% block content %}
<h1>Drafting (B7/B8) admin panel</h1>

<section>
  <h2>Kill switch</h2>
  <p>
    {% if drafting_enabled %}
      <strong>Drafting enabled</strong> — POST /workspaces/&lt;id&gt;/turns
      accepts new turns; the drain tick is running.
    {% else %}
      <strong>Drafting disabled</strong> — POST /workspaces/&lt;id&gt;/turns
      returns 503; queued turns wait for re-enable.
    {% endif %}
  </p>
  <p>
    Toggle via env <code>DRAFTING_ENABLED=true|false</code> and restart.
    UI-driven toggling is a backlog item.
  </p>
</section>

<section>
  <h2>Global daily cap (today, Europe/Stockholm)</h2>
  <table>
    <tr><th>Input</th>
        <td>{{ global_cap.used_in }} / {{ global_cap.cap_in }}</td></tr>
    <tr><th>Output</th>
        <td>{{ global_cap.used_out }} / {{ global_cap.cap_out }}</td></tr>
    <tr><th>Status</th>
        <td>{% if global_cap.allowed %}OK{% else %}<strong>EXCEEDED ({{ global_cap.reason }})</strong>{% endif %}</td></tr>
  </table>
</section>

<section>
  <h2>Per-workspace usage today</h2>
  <table>
    <thead>
      <tr><th>Title</th><th>Owner</th><th>Input</th><th>Output</th></tr>
    </thead>
    <tbody>
      {% for row in per_workspace %}
        <tr>
          <td><a href="/workspaces/{{ row.id }}">{{ row.title|e }}</a></td>
          <td>{{ row.owner_email|e }}</td>
          <td>{{ row.used_in }}</td>
          <td>{{ row.used_out }}</td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
</section>

<section>
  <h2>Recent failed / cancelled turns (last 50, across all workspaces)</h2>
  <table>
    <thead>
      <tr><th>When</th><th>Workspace</th><th>Kind</th><th>Detail</th></tr>
    </thead>
    <tbody>
      {% for e in recent_events %}
        <tr>
          <td>{{ e.created_at | display_dt }}</td>
          <td>{{ e.workspace_id }}</td>
          <td>{{ e.kind }}</td>
          <td><pre>{{ e.payload | tojson(indent=2) }}</pre></td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
</section>
{% endblock %}
```

(Use `tojson` filter for the payload — it's a dict; the existing template engine should have it as a Jinja2 builtin. The `|e` autoescape covers user-supplied workspace titles.)

### Step 4: Add a link from the main admin page

In `src/call_manager/web/templates/admin.html`, add near the top of the existing content block:

```html
<p><a href="/admin/drafting">→ Drafting (B7/B8) panel</a></p>
```

### Step 5: Run tests + commit

```bash
uv run pytest tests/test_web_admin_drafting_panel.py -v
uv run pytest -q
git add ...
git commit -m "feat(b8): /admin/drafting panel — kill switch + global cap + recent failures

Read-only operator view of the proposal-assist drafting subsystem:
- kill switch state from settings.drafting_enabled
- global cap today (used/cap, input + output, OK/EXCEEDED)
- per-workspace usage today, sorted by input tokens DESC
- last 50 cloud_turn_failed / cloud_turn_cancelled events with payload

UI-driven toggling of kill switch / cap override is deferred to a
follow-up (requires a system_state table — out of v1 scope)."
```

---

## Final Verification

- [ ] **Step 1: Full test suite + ruff**

```bash
uv run pytest -q
uv run ruff format src tests
uv run ruff check src tests
```

Expected: 934 baseline + ~10 new tests = ~944 passed / 1 skipped. Ruff clean.

- [ ] **Step 2: Commit any ruff format changes**

```bash
git add -A && git commit -m "style(b8): ruff format" || true
```

- [ ] **Step 3: Hand off to controller for merge** via `superpowers:finishing-a-development-branch`.

---

## Self-review

**Coverage check** — every B8 element from the original whole-system review is addressed:
- ✓ `DRAFTING_ENABLED` kill switch (Task 1)
- ✓ Global per-day token ceiling (Task 2)
- ✓ `/admin/drafting` panel (Task 3)

**Out of scope items deferred** — UI-toggled kill switch (needs system_state table), per-workspace cap override widget (column exists; UI deferred), global cap precision via advisory lock (acceptable overshoot in v1), vision-token-specific dashboard (subsumed by per-workspace usage column), alert hooks.

**Risk areas:**
- The global cap check adds one SELECT per submit. At realistic org scale (~50 users, ~5 turns/user/day) the chat_turns scan is small; index on (workspace_id, created_at) exists. Fine.
- Kill switch in the drain tick means a flip from True→False leaves the queued backlog untouched. Operator should drain before flipping if they want a clean queue; document this in the panel template if it ever surprises someone.
