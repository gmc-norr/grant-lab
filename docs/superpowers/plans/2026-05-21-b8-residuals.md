# B8 Residuals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan.

**Goal:** Close one important kill-switch gap + four nice-to-haves + five regression tests from the post-B8 review. Tighten the operational-safety foundation before the next slice.

**Architecture:** Surgical fixes. The kill-switch fix extends `if not settings.drafting_enabled: return` to two more scheduler closures (`_phase0_tick`, `_compaction_tick`) — mirrors the existing `ANTHROPIC_API_KEY` skip-pattern in the same closures. The other changes are: a one-line `_html.escape` consistency fix, a two-line owner-check order swap, a template-readability rewrite, and a one-sentence `.env.example` clarification.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Postgres 16, pytest + testcontainers.

---

## File Structure

**Modify:**
- `src/call_manager/scheduler.py` — gate `_phase0_tick` and `_compaction_tick` on `drafting_enabled`.
- `src/call_manager/proposal_assist/web/workspaces.py` — swap kill-switch and `_require_owner` order; escape `TokenCapExceeded.reason`.
- `src/call_manager/web/templates/admin_drafting.html` — readability rewrite of input-row status logic.
- `.env.example` — add the "many simultaneous workspaces" caveat to `GLOBAL_DAILY_TOKEN_CAP_INPUT`.

**Test (existing files):**
- `tests/proposal_assist/test_kill_switch.py` — add `test_phase0_and_compaction_skip_when_disabled`, `test_re_enable_drains_queued_backlog`.
- `tests/proposal_assist/test_global_cap.py` — add `test_global_cap_wins_when_per_workspace_also_exceeded`.
- `tests/test_web_admin_drafting_panel.py` — add `test_panel_shows_exceeded_state_when_over_cap`, `test_payload_with_html_chars_is_safely_rendered`.

**Out of scope:** UI-driven kill switch toggle (needs a `system_state` table), per-PI quota, the panel-query connection-holding backlog note.

---

## Task 1: Kill switch gates phase0 + compaction (the Important)

**Files:**
- Modify: `src/call_manager/scheduler.py:325-354` and `:441-469`.
- Modify: `.env.example` — update `DRAFTING_ENABLED` description to mention phase0 + compaction.
- Test: `tests/proposal_assist/test_kill_switch.py` — `test_phase0_and_compaction_skip_when_disabled`.

### Step 1: Write the failing test

Add to `tests/proposal_assist/test_kill_switch.py`:

```python
def test_phase0_and_compaction_skip_when_disabled(monkeypatch):
    """DRAFTING_ENABLED=false must also gate _phase0_tick and
    _compaction_tick (both spend Anthropic tokens). Without this gate
    an operator flipping the kill switch to 'stop the bleeding' sees
    the user queue pause while extractors + compaction keep billing."""
    from unittest.mock import MagicMock

    from apscheduler.schedulers.background import BackgroundScheduler

    from call_manager.config import Settings
    from call_manager.scheduler import build_scheduler

    # Build a scheduler with DRAFTING_ENABLED=false. Capture the
    # tick closures via add_job calls and invoke them directly.
    monkeypatch.setenv("DRAFTING_ENABLED", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    settings = Settings()

    # Patch the run_phase0_tick and run_compaction_tick at their
    # import sites so we can prove they're not called.
    phase0_calls: list = []
    compaction_calls: list = []
    monkeypatch.setattr(
        "call_manager.scheduler.run_phase0_tick",
        lambda **kw: phase0_calls.append(kw),
    )
    monkeypatch.setattr(
        "call_manager.proposal_assist.turns.compaction.run_compaction_tick",
        lambda **kw: compaction_calls.append(kw),
    )

    captured: dict[str, callable] = {}

    class _SchedStub:
        def add_job(self, func, *, id, **_kw):
            captured[id] = func

        def start(self):
            pass

    monkeypatch.setattr(
        "call_manager.scheduler.BackgroundScheduler",
        lambda *_a, **_kw: _SchedStub(),
    )

    sched = build_scheduler(
        session_factory=MagicMock(),
        settings=settings,
        alerter=MagicMock(),
        ollama_url=settings.ollama_url,
        gemma_model=settings.gemma_model,
        ranking_tick_seconds=settings.ranking_tick_seconds,
        ranking_queue_batch=settings.ranking_queue_batch,
        embedding_pregate_threshold=settings.embedding_pregate_threshold,
        embedding_hard_fail_threshold=settings.embedding_hard_fail_threshold,
        anthropic_api_key=settings.anthropic_api_key,
        remote_rank_model=settings.remote_rank_model,
        remote_rank_cosine_threshold=settings.remote_rank_cosine_threshold,
        embed_client=MagicMock(),
        digest_email_client=None,
        digest_settings=settings,
        limiters=[],
        workspace_controller=MagicMock(),
        app_loop=None,
        start=False,
    )

    captured["proposal_assist_phase0_tick"]()
    captured["compaction_tick"]()

    # If DRAFTING_ENABLED=false correctly gates these ticks, neither
    # run_* function was invoked.
    assert phase0_calls == [], "_phase0_tick must skip when drafting_enabled=False"
    assert compaction_calls == [], "_compaction_tick must skip when drafting_enabled=False"
```

(If `BackgroundScheduler` monkeypatch is tricky due to import-time bindings, swap to importing `build_scheduler` after setting envs and stubbing at the module attribute level. Adapt as needed — the test's intent is "neither run_* is invoked when drafting_enabled=False".)

### Step 2: Run the failing test

`uv run pytest tests/proposal_assist/test_kill_switch.py::test_phase0_and_compaction_skip_when_disabled -v` — expected FAIL (both ticks fire).

### Step 3: Gate `_phase0_tick`

In `src/call_manager/scheduler.py:325-346`, at the very top of the closure body (before the `if not settings.anthropic_api_key` check):

```python
    def _phase0_tick() -> None:
        if not settings.drafting_enabled:
            _logger.info("phase0 tick skipped: DRAFTING_ENABLED=false")
            return
        if not settings.anthropic_api_key:
            _logger.info("phase0 tick skipped: ANTHROPIC_API_KEY not set")
            return
        # ... rest unchanged
```

### Step 4: Gate `_compaction_tick`

In `src/call_manager/scheduler.py:441-469`, also at the very top:

```python
    def _compaction_tick() -> None:
        if not settings.drafting_enabled:
            _logger.info("compaction tick skipped: DRAFTING_ENABLED=false")
            return
        from call_manager.proposal_assist.turns.compaction import run_compaction_tick
        # ... rest unchanged
```

### Step 5: Update `.env.example` to describe the broader scope

Find the existing `DRAFTING_ENABLED=true` line and update its docstring:

```bash
# Kill switch for the proposal-assist drafting subsystem. When false:
#   - POST /workspaces/<id>/turns returns 503
#   - the drain tick is a no-op (queued turns wait for re-enable)
#   - the Phase 0 setup tick (rubric/sections extractors) is skipped
#   - the compaction tick is skipped
# All Anthropic-spending paths pause until re-enabled. The idle-sweep
# for live drafting subprocesses still runs (cleanup is independent
# of accepting new work). Toggle requires app restart; UI-driven
# toggling is deferred.
DRAFTING_ENABLED=true
```

### Step 6: Run tests + commit

```bash
uv run pytest tests/proposal_assist/test_kill_switch.py -v
uv run pytest -q
git add ...
git commit -m "fix(b8-residuals): kill switch also gates phase0 + compaction ticks

Per post-B8 review, DRAFTING_ENABLED=false stopped only the user
queue (POST /turns 503 + drain no-op). The phase0 setup tick
(rubric/sections extractors) and the compaction tick both kept
firing — both spend Anthropic tokens. An operator flipping the
switch to 'stop the bleeding' would see the queue pause while
the bill grew from background extractors. Gate both ticks on
drafting_enabled at the closure entry."
```

---

## Task 2: TokenCapExceeded.reason escape consistency

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py:668` (or wherever the per-workspace `except TokenCapExceeded` block lives — `{e.reason}` interpolation).

### Step 1: One-line fix

Find the per-workspace `except TokenCapExceeded` block. Change `{e.reason}` to `{_html.escape(e.reason)}`. The other interpolated fields (`e.used_in`, etc.) are integers — no escape needed.

### Step 2: Existing per-workspace cap tests should still pass

`uv run pytest tests/proposal_assist/test_turns_submit.py tests/proposal_assist/test_web_workspaces_turns.py -q`

### Step 3: Commit

```bash
git commit -m "style(b8-residuals): escape TokenCapExceeded.reason in 429 response

Consistency with the b7-residuals 'escape every interpolation in
HTMLResponse' rule and the new GlobalTokenCapExceeded branch that
already escapes. e.reason is a server-side enum today, so no
practical XSS — this prevents silent regression if the enum grows
caller-influenced values later."
```

---

## Task 3: Swap kill switch and owner check in post_turn

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py:post_turn` opening lines.

### Step 1: Read the current order

`post_turn` currently does:
1. `if not settings.drafting_enabled: return 503` (kill switch)
2. `ws = _require_owner(...)` (auth check)

Swap so auth runs first.

### Step 2: Apply the swap

Move the kill-switch check to AFTER `_require_owner`. Same 503 response.

### Step 3: Test still passes

The existing `test_post_turn_returns_503_when_kill_switch_off` uses an owner, so it still passes. Verify by re-running it.

### Step 4: Commit

```bash
git commit -m "fix(b8-residuals): run owner check before kill switch in post_turn

Auth-first matches the established pattern in workspaces.py
(every other route runs _require_owner / _require_owner_or_viewer
before any other gating). The kill-switch state isn't sensitive,
but the inverted order let a non-owner probing a workspace they
don't own learn whether drafting is enabled (503 instead of 403)."
```

---

## Task 4: admin_drafting.html input-row status readability

**Files:**
- Modify: `src/call_manager/web/templates/admin_drafting.html`.

### Step 1: Apply the cleanup

Find the input-row status logic. Current:

```jinja
{% if global_cap.allowed or global_cap.reason != "input_cap_exceeded" %}OK{% else %}EXCEEDED{% endif %}
```

Replace with the mirrored, simpler form:

```jinja
{% if global_cap.reason == "input_cap_exceeded" %}<strong>EXCEEDED</strong>{% else %}OK{% endif %}
```

(If the original is already nested in a `<td>` with `<strong>`, preserve the surrounding markup.)

### Step 2: Existing admin panel tests still pass

`uv run pytest tests/test_web_admin_drafting_panel.py -v`

### Step 3: Commit

```bash
git commit -m "style(b8-residuals): admin_drafting.html input-row status readability"
```

---

## Task 5: .env.example global cap caveat

**Files:**
- Modify: `.env.example` — the `GLOBAL_DAILY_TOKEN_CAP_INPUT` block.

### Step 1: Add the caveat

After the existing description, append:

```bash
# If you operate >10 simultaneously active workspaces, raise this
# proportionally — per-workspace cap × concurrent workspaces is the
# load that would hit this ceiling.
```

### Step 2: Commit

```bash
git commit -m "docs(b8-residuals): note global cap binds at many simultaneous workspaces"
```

---

## Task 6: Remaining regression tests

**Files:**
- Test: `tests/proposal_assist/test_kill_switch.py` — `test_re_enable_drains_queued_backlog`.
- Test: `tests/proposal_assist/test_global_cap.py` — `test_global_cap_wins_when_per_workspace_also_exceeded`.
- Test: `tests/test_web_admin_drafting_panel.py` — `test_panel_shows_exceeded_state_when_over_cap`, `test_payload_with_html_chars_is_safely_rendered`.

### Step 1: `test_re_enable_drains_queued_backlog` (in test_kill_switch.py)

```python
@pytest.mark.asyncio
async def test_re_enable_drains_queued_backlog(
    pg_session_factory, _settings_env
):
    """Submit while disabled (well, simulate by inserting a queued
    TurnJob directly), then run drain twice: first with disabled=False
    (no-op), then with disabled=True (dispatches). Locks in the
    re-enable resumption behavior."""
    from datetime import UTC, datetime

    from call_manager.db.models import ChatTurn, TurnJob
    from call_manager.proposal_assist.drafting.contract import (
        AssistantTextEvent,
        TurnDoneEvent,
    )
    from call_manager.proposal_assist.drafting.controller import WorkspaceController
    from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
    from call_manager.proposal_assist.drafting.registry import SubprocessRegistry
    from call_manager.proposal_assist.turns.drain import drain_turn_queue_tick
    from tests.proposal_assist.conftest import make_user, make_workspace

    adapter = FakeAgentAdapter()
    adapter.script_next_turn(
        [
            AssistantTextEvent(text="ok"),
            TurnDoneEvent(tokens_in=1, tokens_out=1, model="fake", session_id="s"),
        ]
    )
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(registry=registry, session_factory=pg_session_factory)

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

    # First tick: kill switch OFF → no-op.
    dispatched = await drain_turn_queue_tick(
        session_factory=pg_session_factory,
        controller=controller,
        drafting_enabled=False,
    )
    assert dispatched == 0
    with pg_session_factory() as s:
        from sqlalchemy import select

        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
    assert job.status == "queued", "queued turns must wait for re-enable"

    # Second tick: kill switch ON → dispatches.
    dispatched = await drain_turn_queue_tick(
        session_factory=pg_session_factory,
        controller=controller,
        drafting_enabled=True,
    )
    assert dispatched == 1
    with pg_session_factory() as s:
        from sqlalchemy import select

        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
    assert job.status == "completed"
```

### Step 2: `test_global_cap_wins_when_per_workspace_also_exceeded` (in test_global_cap.py)

```python
def test_global_cap_wins_when_per_workspace_also_exceeded(
    monkeypatch, pg_url, pg_session_factory
):
    """When BOTH caps are exceeded, the global check runs first and
    raises GlobalTokenCapExceeded. The web layer surfaces the global
    header so the UI can distinguish."""
    import io

    from fastapi.testclient import TestClient

    from call_manager.auth.session_cookie import SessionSigner
    from call_manager.db.models import ChatTurn, User, Workspace
    from call_manager.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "100")
    monkeypatch.setenv("DAILY_TOKEN_CAP_INPUT", "100")
    app = create_app()

    with pg_session_factory() as s:
        owner = User(email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T")
        s.add(ws)
        s.flush()
        # Seed: 200 input tokens used today (over BOTH caps).
        s.add(
            ChatTurn(
                workspace_id=ws.id,
                role="assistant",
                content={"type": "text", "text": "x"},
                tokens_in=200,
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
    # GLOBAL wins because it's checked first in submit_user_turn.
    assert resp.headers.get("x-token-cap") == "global"
```

### Step 3: `test_panel_shows_exceeded_state_when_over_cap` (in test_web_admin_drafting_panel.py)

```python
def test_panel_shows_exceeded_state_when_over_cap(
    monkeypatch, pg_url, pg_session_factory
):
    """Seed ChatTurn.tokens_in above the global cap; the panel must
    show 'EXCEEDED' (not 'OK') for the input-row status."""
    app = _build_app(monkeypatch, pg_url, drafting_enabled=True)
    with pg_session_factory() as s:
        owner = User(email="o@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="OverCapWs")
        s.add(ws)
        s.flush()
        # _build_app sets GLOBAL_DAILY_TOKEN_CAP_INPUT=1000; seed 1500.
        s.add(
            ChatTurn(
                workspace_id=ws.id,
                role="assistant",
                content={"type": "text", "text": "x"},
                tokens_in=1500,
            )
        )
        s.commit()
    client = _admin_client(app)
    try:
        resp = client.get("/admin/drafting")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    assert "EXCEEDED" in resp.text
```

### Step 4: `test_payload_with_html_chars_is_safely_rendered` (in test_web_admin_drafting_panel.py)

```python
def test_payload_with_html_chars_is_safely_rendered(
    monkeypatch, pg_url, pg_session_factory
):
    """Locks in the Jinja `tojson` autoescape contract for the
    recent-events table. A cloud_turn_failed event whose payload
    contains a <script> substring (e.g., an SDK exception message
    that quoted user input) MUST NOT render as live HTML."""
    app = _build_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = User(email="o@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="W")
        s.add(ws)
        s.flush()
        s.add(
            WorkspaceEvent(
                workspace_id=ws.id,
                kind="cloud_turn_failed",
                actor_id=owner.id,
                payload={"error": "<script>alert(1)</script>"},
            )
        )
        s.commit()
    client = _admin_client(app)
    try:
        resp = client.get("/admin/drafting")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    assert "<script>" not in resp.text
    # tojson encodes < as <; the escaped form must appear.
    assert "\\u003cscript" in resp.text or "&lt;script" in resp.text
```

### Step 5: Run + commit

```bash
uv run pytest tests/proposal_assist/test_kill_switch.py tests/proposal_assist/test_global_cap.py tests/test_web_admin_drafting_panel.py -v
git add ...
git commit -m "test(b8-residuals): re-enable resume + global-wins-over-per-workspace + panel exceeded + tojson safety"
```

---

## Final Verification

- [ ] Full suite: `uv run pytest -q` — expect 945 + 6 new = 951 passed / 1 skipped.
- [ ] Ruff: `uv run ruff format src tests && uv run ruff check src tests` — clean.
- [ ] If ruff reformatted, `git commit -m "style(b8-residuals): ruff format"`.
- [ ] Hand off to controller for merge via `superpowers:finishing-a-development-branch`.
