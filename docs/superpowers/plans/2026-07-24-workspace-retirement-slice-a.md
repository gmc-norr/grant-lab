# Workspace Retirement — Slice A (durable floor + generation lifecycle) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **v2 — revised after a Codex review of v1** that found four implementation defects which would have survived v1's tests, plus factual errors in v1's test code (wrong adapter class name, wrong `make_chat_turn` form, colliding default users) and a gate that could not fail. All fixes are folded below.

**Goal:** When a workspace is deleted, its running turn reaches a terminal row with token usage counted exactly once, relaunch is refused in the common case, and every SDK subprocess generation is tracked until confirmed dead — with orphans retried each sweep, counted against the live-subprocess ceiling, and their registry entries + advisory locks released.

**Architecture:** Three durable mechanisms plus one lifecycle record. (1) The delete transaction terminalizes `running` turn rows, so the processor's existing conditional `UPDATE ... WHERE status='running'` becomes a no-op — the TOCTOU closes via same-row contention. (2) `controller.launch` checks `soft_deleted_at` above `claim_launch_or_wait`, **narrowing (not closing)** the relaunch window. (3) A per-generation live-client inventory in the adapter tracks every SDK client from before `connect()` until confirmed death; a reconciliation pass retries orphans at both the registry level (entries + advisory locks) and the adapter level (detached generations).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, `claude-agent-sdk`, pytest (`pytest.mark.asyncio` for drafting/registry/adapter tests; real-PG fixtures for DB tests). No new dependencies.

## Global Constraints

- **Backend-only. No migration. No frontend change. No OpenAPI/`schema.gen.ts` change.**
- Slice A is **standalone and independently mergeable** — it must not require the Slice-B coordinator, route change, or collab work.
- **Explicitly NOT claimed by this slice** (do not write code comments, docstrings, or test names asserting otherwise): that any path proves a subprocess died; that reconciliation *contains* a leak (it guarantees a remediation **attempt each tick**); that relaunch is impossible (the precheck is a narrowing — a deletion committing after it can still see a cached handle); that non-mutex writers are covered.
- Backend tests: `EMBEDDING_MODEL=bge-m3 uv run pytest <paths> -p no:cacheprovider` — grouped dirs, **never** whole-suite; final sweep includes `tests/test_api_spec.py`.
- Drafting/registry/adapter tests use `@pytest.mark.asyncio` (NOT anyio — that is the collab convention).
- Lint: `uv run ruff check .` (line-length 100). CI runs `ruff check` only, never `ruff format`.
- Explicit-path `git add` only (never `-A`). Merges are **local `--no-ff` only**, never pushed without an explicit ask. Do NOT invoke `/code-review` or `/ultrareview`.
- Every commit message ends with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- All git commands use `git -C /home/parlar_ai/dev/call-monitor`.

## Codebase facts (verified — v1 got these wrong)

Use these exact names/forms; do not guess:

| Fact | Reality |
|---|---|
| SDK adapter class | **`ClaudeAgentSDKAdapter`** (`claude_adapter.py:145`) — NOT `ClaudeAdapter` |
| Adapter constructor | keyword-only: `ClaudeAgentSDKAdapter(model=..., workspaces_dir=<str>, permission_mode=..., tools_factory=..., tool_context_sink=..., allowed_tools=..., anthropic_api_key=...)` |
| Controller constructor | keyword-only: `WorkspaceController(registry=..., session_factory=..., workspaces_dir=..., lock_session_factory=None)` |
| `make_chat_turn` | `make_chat_turn(session, workspace, *, role, content, tokens_in=None, tokens_out=None, model=None)` — **positional workspace OBJECT**, `content` **required** |
| `make_workspace` | `make_workspace(session, *, owner=None, title=..., current_phase="active", ...)`; with `owner=None` it calls `make_user(session)` |
| `make_user` | `make_user(session, *, email="owner@ki.se")` — the default is **FIXED** (its docstring wrongly claims uniqueness), so two defaulted users in one test **collide on the unique email**. Always pass explicit distinct emails when a test needs more than one user/workspace. |
| `WorkspaceEvent.payload` | `JSONB` → `payload["k"].astext` works for lookup, but provides **no uniqueness** |
| `_finalize_success` | conditional `UPDATE ... WHERE status='running'`; on `rowcount == 0` it **rolls back** (`processor.py:451`), discarding its token stamps |

## File Structure

| File | Role |
|---|---|
| `src/call_manager/proposal_assist/drafting/live_clients.py` | **Create** — `LiveClientRecord` + `LiveClientRegistry`: pure per-generation bookkeeping (no SDK types) |
| `src/call_manager/proposal_assist/turns/outcomes.py` | Modify — `record_terminal_outcome(usage_unknown=...)`; new `stamp_post_terminal_usage` (row-serialized) |
| `src/call_manager/workspaces.py` | Modify — delete txn terminalizes `running` turn rows |
| `src/call_manager/proposal_assist/turns/processor.py` | Modify — guarded region; `_finalize_success`/`_finalize_failed` return "won the row"; usage-reported tracking; late-usage stamp |
| `src/call_manager/proposal_assist/drafting/contract.py` | Modify — add `WorkspaceRetired` |
| `src/call_manager/proposal_assist/drafting/controller.py` | Modify — pre-claim check; `_workspace_is_deleted`; `reconcile_live_clients` (registry pass **then** adapter pass) |
| `src/call_manager/proposal_assist/drafting/claude_adapter.py` | Modify — register before `connect()`; bounded teardown + timer force-kill + reap; confirm death |
| `src/call_manager/proposal_assist/drafting/registry.py` | Modify — capacity counts unconfirmed generations **and re-checks after eviction teardown**; `reconcile_deleted_entries` |
| `src/call_manager/proposal_assist/drafting/fake_adapter.py` | Modify — mirror the inventory surface |
| `src/call_manager/scheduler.py` | Modify — sweep tick also reconciles |
| `tests/proposal_assist/test_live_clients.py` | **Create** |
| `tests/proposal_assist/test_claude_adapter_teardown.py` | **Create** |
| `tests/proposal_assist/test_turn_outcomes.py` | Extend |
| `tests/test_workspaces_service.py` | Extend |
| `tests/proposal_assist/test_drafting_controller.py` | Extend |
| `tests/proposal_assist/test_drafting_registry_capacity.py` | Extend |
| `tests/proposal_assist/test_turns_processor.py` | Extend |
| `tests/proposal_assist/test_drafting_scheduler_tick.py` | Extend |

---

### Task 0: Cut the build branch

**Files:** none (git only).

- [ ] **Step 1: Verify master + clean tree**

```bash
git -C /home/parlar_ai/dev/call-monitor branch --show-current
git -C /home/parlar_ai/dev/call-monitor status --porcelain
```
Expected: `master`; only pre-existing untracked `docs/superpowers/...` files.

- [ ] **Step 2: Cut the branch**

```bash
git -C /home/parlar_ai/dev/call-monitor checkout -b workspace-retirement-slice-a
```

---

### Task 1: `record_terminal_outcome(usage_unknown=...)` retains the estimate

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/outcomes.py:41-90`
- Test: `tests/proposal_assist/test_turn_outcomes.py`

**Interfaces:**
- Produces: `record_terminal_outcome(..., usage_unknown: bool = False) -> bool`. `True` retains the submit-time user-row estimate instead of clearing it. Default `False` = today's behavior byte-for-byte.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_turn_outcomes.py`:

```python
def test_usage_unknown_retains_submit_time_estimate(pg_session_factory):
    # A running turn terminalized by workspace deletion HAS consumed input we
    # cannot measure yet. The submit-time estimate must survive, or the spend
    # vanishes from the daily cap.
    with pg_session_factory() as s:
        ws = make_workspace(s)
        user_turn = make_chat_turn(
            s, ws, role="user", content={"type": "text", "text": "hi"}, tokens_in=1234
        )
        job = TurnJob(
            workspace_id=ws.id, status="running",
            user_turn_id=user_turn.id, submitted_by_user_id=ws.owner_id,
        )
        s.add(job)
        s.commit()
        ws_id, job_id, ut_id = ws.id, job.id, user_turn.id

    with pg_session_factory() as s:
        assert record_terminal_outcome(
            s, turn_job_id=job_id, workspace_id=ws_id, outcome="cancelled",
            expected_status="running", raw_error="workspace soft-deleted",
            emit_chat=False, usage_unknown=True,
        ) is True
        s.commit()

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status == "cancelled"
        assert s.get(ChatTurn, ut_id).tokens_in == 1234      # RETAINED


def test_default_still_clears_estimate_when_nothing_consumed(pg_session_factory):
    # Byte-for-byte guard on the pre-existing queued/reaped path.
    with pg_session_factory() as s:
        ws = make_workspace(s)
        user_turn = make_chat_turn(
            s, ws, role="user", content={"type": "text", "text": "hi"}, tokens_in=999
        )
        job = TurnJob(
            workspace_id=ws.id, status="queued",
            user_turn_id=user_turn.id, submitted_by_user_id=ws.owner_id,
        )
        s.add(job)
        s.commit()
        ws_id, job_id, ut_id = ws.id, job.id, user_turn.id

    with pg_session_factory() as s:
        record_terminal_outcome(
            s, turn_job_id=job_id, workspace_id=ws_id, outcome="cancelled",
            expected_status="queued", raw_error="workspace soft-deleted", emit_chat=False,
        )
        s.commit()

    with pg_session_factory() as s:
        assert s.get(ChatTurn, ut_id).tokens_in is None      # cleared, as before
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_turn_outcomes.py -p no:cacheprovider -q -k "usage_unknown or clears_estimate"
```
Expected: FAIL — unexpected keyword argument `usage_unknown`.

- [ ] **Step 3: Implement**

Add to the signature after `tokens_out: int = 0,`:

```python
    usage_unknown: bool = False,
```
Docstring addition:
```
    usage_unknown: True when the turn is terminalized while RUNNING and its real
        usage is not yet known (workspace deletion). Retains the submit-time
        estimate so the spend stays in the daily-cap sum; stamp_post_terminal_usage
        later replaces it with the SDK-reported truth.
```
Change the clearing branch:
```python
    if user_turn_id is not None:
        user_turn = session.scalar(select(ChatTurn).where(ChatTurn.id == user_turn_id))
        if user_turn is not None and not consumed and not usage_unknown:
            user_turn.tokens_in = None
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_turn_outcomes.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/proposal_assist/turns/outcomes.py
```

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/turns/outcomes.py tests/proposal_assist/test_turn_outcomes.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(turns): record_terminal_outcome(usage_unknown) retains the estimate

Terminalizing a RUNNING turn at 0/0 would clear the submit-time pre-count while
the real usage is later rolled back by the terminal-row guard — invisible spend
against the daily cap. Default behavior unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `stamp_post_terminal_usage` — row-serialized, atomically idempotent

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/outcomes.py` (append)
- Test: `tests/proposal_assist/test_turn_outcomes.py`

**Interfaces:**
- Produces:
  ```python
  def stamp_post_terminal_usage(
      session: Session, *, turn_job_id: uuid.UUID, workspace_id: uuid.UUID,
      user_turn_id: uuid.UUID | None, last_assistant_id: uuid.UUID | None,
      tokens_in: int, tokens_out: int, model: str | None,
  ) -> bool
  ```
  `True` if this call stamped; `False` if already reconciled, the job is missing / in another workspace, or the job is not terminal. Never changes `TurnJob.status`.

**Why row-serialized (review P1-2):** a JSONB lookup gives no uniqueness — two sessions can both see "no event" and both insert. Serialize on the `TurnJob` row with `SELECT … FOR UPDATE`, then re-check the event **inside** that lock.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_turn_outcomes.py`:

```python
def test_post_terminal_usage_stamps_truth_clears_estimate_and_is_idempotent(pg_session_factory):
    from call_manager.proposal_assist.turns.outcomes import stamp_post_terminal_usage

    with pg_session_factory() as s:
        ws = make_workspace(s)
        user_turn = make_chat_turn(
            s, ws, role="user", content={"type": "text", "text": "q"}, tokens_in=1234
        )
        assistant = make_chat_turn(s, ws, role="assistant", content={"type": "text", "text": "a"})
        job = TurnJob(
            workspace_id=ws.id, status="cancelled",          # already terminal
            user_turn_id=user_turn.id, submitted_by_user_id=ws.owner_id,
        )
        s.add(job)
        s.commit()
        ws_id, job_id, ut_id, a_id = ws.id, job.id, user_turn.id, assistant.id

    with pg_session_factory() as s:
        assert stamp_post_terminal_usage(
            s, turn_job_id=job_id, workspace_id=ws_id, user_turn_id=ut_id,
            last_assistant_id=a_id, tokens_in=500, tokens_out=42, model="claude-opus-4-7",
        ) is True
        s.commit()

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status == "cancelled"   # status untouched
        assert s.get(ChatTurn, a_id).tokens_in == 500
        assert s.get(ChatTurn, a_id).tokens_out == 42
        assert s.get(ChatTurn, ut_id).tokens_in is None       # estimate cleared (no double count)
        assert s.scalar(
            select(func.count()).select_from(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "cloud_turn_usage_reconciled",
            )
        ) == 1

    with pg_session_factory() as s:
        assert stamp_post_terminal_usage(
            s, turn_job_id=job_id, workspace_id=ws_id, user_turn_id=ut_id,
            last_assistant_id=a_id, tokens_in=999, tokens_out=999, model="x",
        ) is False                                            # idempotent
        s.commit()

    with pg_session_factory() as s:
        assert s.get(ChatTurn, a_id).tokens_in == 500         # unchanged
        assert s.scalar(
            select(func.count()).select_from(WorkspaceEvent).where(
                WorkspaceEvent.kind == "cloud_turn_usage_reconciled",
                WorkspaceEvent.workspace_id == ws_id,
            )
        ) == 1


def test_post_terminal_usage_is_atomic_under_concurrency(pg_session_factory):
    """Two concurrent stampers must produce exactly ONE event and ONE stamp.
    A plain 'select event then insert' loses this race — the guard is the
    SELECT ... FOR UPDATE on the TurnJob row."""
    import threading

    from call_manager.proposal_assist.turns.outcomes import stamp_post_terminal_usage

    with pg_session_factory() as s:
        ws = make_workspace(s)
        user_turn = make_chat_turn(
            s, ws, role="user", content={"type": "text", "text": "q"}, tokens_in=1234
        )
        job = TurnJob(
            workspace_id=ws.id, status="cancelled",
            user_turn_id=user_turn.id, submitted_by_user_id=ws.owner_id,
        )
        s.add(job)
        s.commit()
        ws_id, job_id, ut_id = ws.id, job.id, user_turn.id

    results: list[bool] = []
    barrier = threading.Barrier(2)

    def _stamp():
        with pg_session_factory() as s:
            barrier.wait(timeout=10)          # maximize overlap
            ok = stamp_post_terminal_usage(
                s, turn_job_id=job_id, workspace_id=ws_id, user_turn_id=ut_id,
                last_assistant_id=None, tokens_in=300, tokens_out=10, model=None,
            )
            s.commit()
            results.append(ok)

    threads = [threading.Thread(target=_stamp) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(results) == [False, True]    # exactly one winner
    with pg_session_factory() as s:
        assert s.scalar(
            select(func.count()).select_from(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "cloud_turn_usage_reconciled",
            )
        ) == 1
        assert s.get(ChatTurn, ut_id).tokens_in == 300   # stamped once, not twice


def test_post_terminal_usage_refuses_non_terminal_or_foreign_job(pg_session_factory):
    from call_manager.proposal_assist.turns.outcomes import stamp_post_terminal_usage

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="a-owner@ki.se"))
        other = make_workspace(s, owner=make_user(s, email="b-owner@ki.se"))
        user_turn = make_chat_turn(s, ws, role="user", content={"type": "text", "text": "q"})
        job = TurnJob(
            workspace_id=ws.id, status="running",        # NOT terminal
            user_turn_id=user_turn.id, submitted_by_user_id=ws.owner_id,
        )
        s.add(job)
        s.commit()
        ws_id, other_id, job_id, ut_id = ws.id, other.id, job.id, user_turn.id

    with pg_session_factory() as s:
        assert stamp_post_terminal_usage(
            s, turn_job_id=job_id, workspace_id=ws_id, user_turn_id=ut_id,
            last_assistant_id=None, tokens_in=1, tokens_out=1, model=None,
        ) is False                                        # still running
        assert stamp_post_terminal_usage(
            s, turn_job_id=job_id, workspace_id=other_id, user_turn_id=ut_id,
            last_assistant_id=None, tokens_in=1, tokens_out=1, model=None,
        ) is False                                        # wrong workspace
```

Ensure the module imports `func` (`from sqlalchemy import func, select`), `WorkspaceEvent`, and `make_user`.

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_turn_outcomes.py -p no:cacheprovider -q -k post_terminal
```
Expected: FAIL — `cannot import name 'stamp_post_terminal_usage'`.

- [ ] **Step 3: Implement**

Append to `outcomes.py`:

```python
USAGE_RECONCILED_KIND = "cloud_turn_usage_reconciled"

_TERMINAL_STATUSES = ("failed", "cancelled", "completed")


def stamp_post_terminal_usage(
    session: Session,
    *,
    turn_job_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_turn_id: uuid.UUID | None,
    last_assistant_id: uuid.UUID | None,
    tokens_in: int,
    tokens_out: int,
    model: str | None,
) -> bool:
    """Record a terminalized turn's REAL usage after the fact.

    Used when a turn was terminalized while RUNNING (workspace deletion) and its
    stream later reported actual usage, so the normal finalize lost the row and
    rolled its token stamp back. Writes the SDK truth and CLEARS the retained
    submit-time estimate (mirroring _finalize_success) so the daily cap counts the
    spend exactly once. NEVER changes TurnJob.status; emits ONE non-outcome
    ``cloud_turn_usage_reconciled`` audit event rather than a second outcome.

    Concurrency: serialized on the TurnJob row (SELECT ... FOR UPDATE) — the event
    lookup alone is not atomic (JSONB gives no uniqueness), so two callers could
    otherwise both stamp. Returns True only for the caller that performed the
    stamp. Caller commits.
    """
    job = session.execute(
        select(TurnJob)
        .where(TurnJob.id == turn_job_id, TurnJob.workspace_id == workspace_id)
        .with_for_update()
    ).scalar_one_or_none()
    if job is None or job.status not in _TERMINAL_STATUSES:
        return False

    already = session.scalar(
        select(WorkspaceEvent.id).where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.kind == USAGE_RECONCILED_KIND,
            WorkspaceEvent.payload["turn_job_id"].astext == str(turn_job_id),
        )
    )
    if already is not None:
        return False

    user_turn = (
        session.scalar(select(ChatTurn).where(ChatTurn.id == user_turn_id))
        if user_turn_id is not None
        else None
    )
    if last_assistant_id is not None:
        assistant = session.scalar(select(ChatTurn).where(ChatTurn.id == last_assistant_id))
        if assistant is not None:
            assistant.tokens_in = tokens_in or None
            assistant.tokens_out = tokens_out or None
            assistant.model = model
        # The SDK total already includes user-input tokens; drop the estimate so
        # the cap sum doesn't count input twice (mirrors _finalize_success).
        if user_turn is not None:
            user_turn.tokens_in = None
    elif user_turn is not None:
        # No assistant row — persist totals on the user row, REPLACING the estimate.
        user_turn.tokens_in = tokens_in or None
        user_turn.tokens_out = tokens_out or None
        if model is not None:
            user_turn.model = model

    session.add(
        WorkspaceEvent(
            workspace_id=workspace_id,
            kind=USAGE_RECONCILED_KIND,
            actor_id=None,
            payload={
                "turn_job_id": str(turn_job_id),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "model": model,
            },
        )
    )
    return True
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_turn_outcomes.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/proposal_assist/turns/outcomes.py
```

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/turns/outcomes.py tests/proposal_assist/test_turn_outcomes.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(turns): stamp_post_terminal_usage — row-serialized late-usage reconciliation

Writes a terminalized turn's real usage and CLEARS the retained estimate (else
estimate + truth double-count), never touching TurnJob.status. Serialized on the
TurnJob row with SELECT ... FOR UPDATE: the JSONB event lookup alone is not
atomic, so two stampers could both insert. Emits one non-outcome event.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: delete transaction terminalizes RUNNING turn rows

**Files:**
- Modify: `src/call_manager/workspaces.py:82-133`
- Test: `tests/test_workspaces_service.py`

**Interfaces:**
- Consumes: `record_terminal_outcome(..., usage_unknown=True)` (Task 1).
- Produces: after `soft_delete_workspace`, no `turn_jobs` row for the workspace remains `running`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_workspaces_service.py`:

```python
def test_soft_delete_terminalizes_running_turn_and_finalize_is_noop(pg_session_factory, tmp_path):
    from call_manager.db.models import ChatTurn, TurnJob
    from call_manager.proposal_assist.turns.processor import _finalize_success
    from call_manager.workspaces import soft_delete_workspace
    from tests.proposal_assist.conftest import make_chat_turn, make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s)
        user_turn = make_chat_turn(
            s, ws, role="user", content={"type": "text", "text": "q"}, tokens_in=1234
        )
        job = TurnJob(
            workspace_id=ws.id, status="running",
            user_turn_id=user_turn.id, submitted_by_user_id=ws.owner_id,
        )
        s.add(job)
        s.commit()
        ws_id, job_id, ut_id = ws.id, job.id, user_turn.id

    with pg_session_factory() as s:
        soft_delete_workspace(s, workspace_id=ws_id, workspaces_dir=tmp_path)

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status != "running"     # terminalized in the delete txn
        assert s.get(ChatTurn, ut_id).tokens_in == 1234       # estimate retained

    # The in-flight processor now finalizes "successfully" — must be a NO-OP.
    _finalize_success(
        pg_session_factory,
        turn_job_id=job_id, workspace_id=ws_id, submitted_by_user_id=None,
        user_turn_id=ut_id, last_assistant_id=None,
        tokens_in=10, tokens_out=5, model="m", session_id=None,
    )

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status != "completed"   # never resurrected
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/test_workspaces_service.py -p no:cacheprovider -q -k terminalizes
```
Expected: FAIL — the row is still `running`.

- [ ] **Step 3: Implement**

In `workspaces.py`, after the queued-turn loop and **before** `session.commit()`:

```python
    # Terminalize any RUNNING turn in THIS transaction. The processor's
    # success-finalize is a conditional UPDATE ... WHERE status='running', so once
    # this commits both paths contend on the same row and the finalize becomes a
    # no-op — closing the delete-vs-finalize race durably, with no in-process
    # recheck. usage_unknown=True keeps the submit-time estimate: the turn HAS
    # consumed input we cannot measure yet, and stamp_post_terminal_usage replaces
    # it if the stream reports real usage.
    running = session.execute(
        select(TurnJob.id).where(
            TurnJob.workspace_id == workspace_id,
            TurnJob.status == "running",
        )
    ).all()
    for row in running:
        record_terminal_outcome(
            session,
            turn_job_id=row.id,
            workspace_id=workspace_id,
            outcome="cancelled",
            expected_status="running",
            raw_error="workspace soft-deleted",
            emit_chat=False,
            usage_unknown=True,
        )
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/test_workspaces_service.py tests/proposal_assist/test_turn_outcomes.py \
  -p no:cacheprovider -q && uv run ruff check src/call_manager/workspaces.py
```

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/workspaces.py tests/test_workspaces_service.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(workspaces): delete txn terminalizes RUNNING turn rows

Delete and the processor's success-finalize now contend on the SAME row, so the
finalize becomes a no-op — the TOCTOU closes durably and the defensive
TurnDoneEvent path cannot resurrect a deleted turn.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `WorkspaceRetired` + pre-claim soft-delete check

**Files:**
- Modify: `src/call_manager/proposal_assist/drafting/contract.py`
- Modify: `src/call_manager/proposal_assist/drafting/controller.py:105`
- Test: `tests/proposal_assist/test_drafting_controller.py`

**Interfaces:**
- Produces: `WorkspaceRetired(workspace_id)`; `controller.launch` raises it for a soft-deleted workspace; `controller._workspace_is_deleted(ws) -> bool`.

**Honest scope:** the check must sit **above `claim_launch_or_wait`** (the cached/pending path returns early at `controller.py:109`). It **narrows** the relaunch window; it does not close it. Task 4 includes a test that *documents the conceded race* rather than asserting it away.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_drafting_controller.py`:

```python
@pytest.mark.asyncio
async def test_launch_refuses_soft_deleted_workspace(pg_session_factory, tmp_path):
    from datetime import UTC, datetime

    from call_manager.db.models import Workspace
    from call_manager.proposal_assist.drafting.contract import WorkspaceRetired
    from tests.proposal_assist.conftest import make_workspace

    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory, workspaces_dir=tmp_path
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id
    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    with pytest.raises(WorkspaceRetired):
        await controller.launch(ws_id)
    assert adapter.launches == []


@pytest.mark.asyncio
async def test_launch_refuses_deleted_workspace_even_when_handle_is_cached(
    pg_session_factory, tmp_path
):
    """The cached/pending path returns early at controller.py:109, so the check
    MUST precede claim_launch_or_wait."""
    from datetime import UTC, datetime

    from call_manager.db.models import Workspace
    from call_manager.proposal_assist.drafting.contract import WorkspaceRetired
    from tests.proposal_assist.conftest import make_workspace

    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory, workspaces_dir=tmp_path
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    try:
        await controller.launch(ws_id)               # warm: handle cached
        assert len(adapter.launches) == 1

        with pg_session_factory() as s:
            s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
            s.commit()

        with pytest.raises(WorkspaceRetired):
            await controller.launch(ws_id)           # cached path must STILL refuse
        assert len(adapter.launches) == 1
    finally:
        # Release the advisory-lock session held by the cached entry.
        await controller.terminate(ws_id)


@pytest.mark.asyncio
async def test_precheck_race_is_documented_not_closed(pg_session_factory, tmp_path):
    """CONCEDED RACE (spec v8): a deletion committing AFTER the precheck can still
    see a cached handle returned. This test pins the documented behavior so a
    future change cannot silently claim more than the design promises."""
    from datetime import UTC, datetime

    from call_manager.db.models import Workspace
    from tests.proposal_assist.conftest import make_workspace

    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory, workspaces_dir=tmp_path
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    try:
        await controller.launch(ws_id)               # warm the cache

        original = controller._workspace_is_deleted

        def _deleting_precheck(w):
            result = original(w)                     # observes NOT deleted
            with pg_session_factory() as s:          # deletion commits AFTER the check
                s.get(Workspace, w).soft_deleted_at = datetime.now(UTC)
                s.commit()
            return result

        controller._workspace_is_deleted = _deleting_precheck

        handle = await controller.launch(ws_id)      # documented: still succeeds
        assert handle is not None
        assert len(adapter.launches) == 1            # cached — no NEW subprocess
    finally:
        await controller.terminate(ws_id)
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_drafting_controller.py -p no:cacheprovider -q \
  -k "soft_deleted or cached or precheck_race"
```
Expected: FAIL — `cannot import name 'WorkspaceRetired'`.

- [ ] **Step 3a: Add the exception**

In `contract.py`, after `DraftingAtCapacity`:

```python
class WorkspaceRetired(RuntimeError):
    """The workspace is soft-deleted, so no subprocess may be launched for it.

    Raised by WorkspaceController.launch. Turn-path callers finalize instead of
    sending. NOTE: this is a NARROWING, not a race-free gate — a deletion
    committing after the check can still see a cached handle returned; the
    reconciliation sweep is what eventually reaps such a subprocess."""

    def __init__(self, workspace_id: uuid.UUID) -> None:
        super().__init__(f"workspace {workspace_id} is retired")
        self.workspace_id = workspace_id
```

- [ ] **Step 3b: Add the check + helper**

In `controller.py`, immediately **before** `am_launcher, pending = await self._registry.claim_launch_or_wait(workspace_id)`:

```python
        # Durable no-relaunch floor: refuse a soft-deleted workspace BEFORE
        # claim_launch_or_wait, because the cached/pending path returns early below
        # and never reaches the work_session block. Restart-safe; needs no wiring.
        # NARROWING ONLY — a deletion committing after this check can still see a
        # cached handle returned; reconciliation is the backstop.
        if self._workspace_is_deleted(workspace_id):
            raise WorkspaceRetired(workspace_id)
```

Add next to `_workspace_has_queued_turn`:

```python
    def _workspace_is_deleted(self, workspace_id: uuid.UUID) -> bool:
        """True if the workspace row is missing or soft-deleted. Fresh short-lived
        session. Best-effort: on a DB error return False so a transient blip cannot
        block legitimate drafting — reconciliation still reaps a genuinely deleted
        workspace's subprocess."""
        from call_manager.db.models import Workspace

        try:
            with self._session_factory() as s:
                row = s.execute(
                    select(Workspace.soft_deleted_at).where(Workspace.id == workspace_id)
                ).first()
                return row is None or row[0] is not None
        except Exception:
            logger.exception(
                "launch: soft-delete lookup failed for %s; allowing launch", workspace_id
            )
            return False
```

Import `WorkspaceRetired` from `.contract` in `controller.py`.

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_drafting_controller.py tests/proposal_assist/test_drafting_registry.py \
  -p no:cacheprovider -q && uv run ruff check \
  src/call_manager/proposal_assist/drafting/contract.py \
  src/call_manager/proposal_assist/drafting/controller.py
```

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/drafting/contract.py \
  src/call_manager/proposal_assist/drafting/controller.py \
  tests/proposal_assist/test_drafting_controller.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(drafting): refuse launch for a soft-deleted workspace (WorkspaceRetired)

Checked ABOVE claim_launch_or_wait — the cached/pending path returns early and
would otherwise keep serving send_turn's internal launch. Documented as a
NARROWING; a test pins the conceded post-check race rather than asserting it away.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: processor — guarded region + correct late-usage routing

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/processor.py:276-380`, `_finalize_success` (`:398-460`), `_finalize_failed`
- Test: `tests/proposal_assist/test_turns_processor.py`

**Interfaces:**
- Consumes: `WorkspaceRetired` (Task 4), `stamp_post_terminal_usage` (Task 2).
- Produces: `_finalize_success(...) -> bool` and `_finalize_failed(...) -> bool` — **True when the call won the conditional row update**, False when the row was already terminal.

**Why the v1 routing was wrong (review P1-1):** `WorkspaceRetired` fires *before* streaming, so `tokens_in/out` are `0/0` — stamping there would clear the retained estimate and re-create the cap bypass. The case that actually needs reconciliation is the opposite one: deletion lands *mid-stream*, `TurnDoneEvent` arrives normally, execution takes the **success** branch, and `_finalize_success` rolls back its token stamp because the row is terminal (`processor.py:451`). Correct rule: **stamp only when usage was reported AND the finalize lost the row.**

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/test_turns_processor.py` (build the workspace/queued-job/controller with this module's existing helpers — grep the file for how neighbouring tests construct `process_turn_once` inputs, and reuse that construction verbatim):

```python
@pytest.mark.asyncio
async def test_retired_before_send_finalizes_without_sending_and_keeps_estimate(
    pg_session_factory, tmp_path, monkeypatch
):
    """Deletion between claim and the post-claim launch: no send, row not left
    'running', and the retained estimate is NOT cleared (no usage was reported)."""
    from call_manager.db.models import ChatTurn, TurnJob
    from call_manager.proposal_assist.drafting.contract import WorkspaceRetired

    # ... construct ws + user_turn (tokens_in=1234) + queued job + controller/adapter ...

    calls = {"n": 0}
    real_launch = controller.launch

    async def _launch(ws_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return await real_launch(ws_id)
        raise WorkspaceRetired(ws_id)

    monkeypatch.setattr(controller, "launch", _launch)

    await process_turn_once(
        session_factory=pg_session_factory, controller=controller,
        turn_job_id=job_id, workspaces_dir=tmp_path,
    )

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status != "running"
        assert s.get(ChatTurn, ut_id).tokens_in == 1234    # estimate PRESERVED
    assert adapter.turns == []                             # nothing sent


@pytest.mark.asyncio
async def test_delete_during_stream_reconciles_reported_usage(
    pg_session_factory, tmp_path
):
    """The real race: deletion terminalizes the row WHILE streaming. TurnDoneEvent
    arrives normally, _finalize_success loses the row and rolls back its stamp —
    so the usage must be reconciled instead of lost."""
    from call_manager.db.models import ChatTurn, TurnJob, WorkspaceEvent
    from call_manager.proposal_assist.drafting.contract import TurnDoneEvent
    from call_manager.workspaces import soft_delete_workspace

    # ... construct ws + user_turn (tokens_in=1234) + queued job + controller/adapter ...

    # Script a stream that deletes the workspace mid-flight, then reports usage.
    class _DeletingStream:
        def __init__(self, ws_id):
            self.ws_id = ws_id

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            with pg_session_factory() as s:
                soft_delete_workspace(s, workspace_id=self.ws_id, workspaces_dir=tmp_path)
            yield TurnDoneEvent(tokens_in=700, tokens_out=90, model="m", session_id=None)

    controller.send_turn = lambda ws, msg: _DeletingStream(ws_id)

    await process_turn_once(
        session_factory=pg_session_factory, controller=controller,
        turn_job_id=job_id, workspaces_dir=tmp_path,
    )

    with pg_session_factory() as s:
        job = s.get(TurnJob, job_id)
        assert job.status != "completed"          # delete won the row
        # The reported usage was reconciled rather than rolled back with the finalize.
        assert s.scalar(
            select(func.count()).select_from(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "cloud_turn_usage_reconciled",
            )
        ) == 1
        ut = s.get(ChatTurn, ut_id)
        assert ut.tokens_in == 700 and ut.tokens_out == 90   # truth replaced the estimate
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_turns_processor.py -p no:cacheprovider -q \
  -k "retired_before_send or delete_during_stream"
```
Expected: FAIL — `WorkspaceRetired` escapes `process_turn_once`; and no `cloud_turn_usage_reconciled` event exists (the usage was rolled back).

- [ ] **Step 3a: Make the finalizers report whether they won the row**

In `_finalize_success`, change the signature to `-> bool` and return explicitly:

```python
        if (claimed.rowcount or 0) == 0:
            s.rollback()
            logger.warning(... unchanged ...)
            return False        # lost the row (already terminal) — caller may reconcile usage
```
and end the function with `return True`.

In `_finalize_failed`, change the signature to `-> bool` and return the result of its `record_terminal_outcome(...)` call (that helper already returns whether it performed the transition).

- [ ] **Step 3b: Guarded region, usage tracking, and the reconciliation call**

Move the post-claim launch + provenance **inside** the guarded region and track reported usage:

```python
    sdk_error: SDKTurnError | None = None
    other_error: BaseException | None = None
    retired: WorkspaceRetired | None = None
    usage_reported = False

    try:
        # COLD-LAUNCH ORDER (unchanged): ensure launch, THEN stamp provenance, THEN
        # send. Now INSIDE the guarded region — a WorkspaceRetired raised here must
        # finalize the turn, not escape and leave the row 'running' for the reaper.
        await controller.launch(workspace_id)
        await controller.set_turn_provenance(
            workspace_id,
            triggering_turn_id=user_turn_id,
            triggering_prompt=_extract_triggering_prompt(raw_turn_content),
            created_by=submitted_by_user_id,
            origin_session_id=ws_session_id,
            allowed_tool_names=allowed_tools_for_content(raw_turn_content),
            turn_selection=selection_from_content(raw_turn_content),
        )
        async for event in controller.send_turn(workspace_id, user_message):
            ...   # UNCHANGED handling, except the TurnDoneEvent branch below
            elif isinstance(event, TurnDoneEvent):
                tokens_in = event.tokens_in or 0
                tokens_out = event.tokens_out or 0
                model = event.model
                session_id = event.session_id
                usage_reported = tokens_in > 0 or tokens_out > 0
    except WorkspaceRetired as e:
        retired = e
    except SDKTurnError as e:
        sdk_error = e
    except BaseException as e:  # noqa: BLE001
        other_error = e
```

Then make the finalize dispatch capture the outcome and reconcile when needed:

```python
    if retired is not None:
        # The delete transaction already terminalized this row, so this is
        # normally a no-op; it keeps the code path honest when it is not.
        won = _finalize_failed(
            session_factory,
            turn_job_id=turn_job_id,
            workspace_id=workspace_id,
            submitted_by_user_id=submitted_by_user_id,
            error="workspace_retired",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            user_turn_id=user_turn_id,
        )
    elif sdk_error is None and other_error is None:
        won = _finalize_success(... unchanged args ...)
    elif sdk_error is not None:
        won = _finalize_failed(... unchanged args ...)
    else:
        assert other_error is not None
        won = _finalize_failed(... unchanged args ...)
        if not isinstance(other_error, Exception):
            raise other_error

    if not won and usage_reported:
        # We LOST the row — deletion terminalized it mid-stream — so the finalize
        # rolled its token stamp back. Reconcile the usage that really was spent,
        # exactly once. (When no usage was reported there is nothing to reconcile
        # and the retained submit-time estimate must be left alone.)
        with session_factory() as s:
            stamp_post_terminal_usage(
                s,
                turn_job_id=turn_job_id,
                workspace_id=workspace_id,
                user_turn_id=user_turn_id,
                last_assistant_id=last_assistant_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                model=model,
            )
            s.commit()
```

Add imports: `WorkspaceRetired` to the existing `contract` import list, and `from call_manager.proposal_assist.turns.outcomes import stamp_post_terminal_usage`.

- [ ] **Step 4: Run to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_turns_processor.py tests/proposal_assist/test_turns_end_to_end.py \
  tests/proposal_assist/test_turns_drain.py tests/proposal_assist/test_turn_outcomes.py \
  tests/proposal_assist/test_turns_session_id_persistence.py \
  -p no:cacheprovider -q && uv run ruff check src/call_manager/proposal_assist/turns/processor.py
```
Expected: all pass — existing behavior is unchanged for non-retired, row-winning paths.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/turns/processor.py tests/proposal_assist/test_turns_processor.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(turns): guard the post-claim launch; reconcile usage only when the row is lost

The post-claim launch + provenance sat OUTSIDE the finalization try, so a
WorkspaceRetired left the row 'running' for the reaper. Finalizers now report
whether they won the conditional update; when the delete won mid-stream AND the
stream reported usage, that usage is reconciled instead of rolled back. Retiring
BEFORE any stream reports nothing, so the retained estimate is left intact.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: `LiveClientRegistry` — per-generation bookkeeping

**Files:**
- Create: `src/call_manager/proposal_assist/drafting/live_clients.py`
- Test: `tests/proposal_assist/test_live_clients.py`

**Interfaces:**
- Produces: `LiveClientRecord(launch_id, workspace_id, client, state, teardown)`; `LiveClientRegistry` with `register(workspace_id, client) -> uuid.UUID`, `mark_retiring(launch_id) -> LiveClientRecord | None`, `confirm_dead(launch_id)`, `get`, `find_by_workspace`, `records()`, `retiring_count()`, `unconfirmed_count()`; module constants `LIVE`, `RETIRING`.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_live_clients.py`:

```python
"""Per-generation live-client bookkeeping (Slice A). Pure in-memory logic."""
import uuid

from call_manager.proposal_assist.drafting.live_clients import LiveClientRegistry


def test_register_returns_distinct_generations_for_one_workspace():
    reg = LiveClientRegistry()
    ws = uuid.uuid4()
    a = reg.register(ws, client="client-a")
    b = reg.register(ws, client="client-b")
    assert a != b
    assert {r.launch_id for r in reg.find_by_workspace(ws)} == {a, b}
    assert reg.unconfirmed_count() == 2


def test_clearing_is_by_identity_not_by_workspace():
    # Orphan/replacement: a stale teardown must NOT clear the new client.
    reg = LiveClientRegistry()
    ws = uuid.uuid4()
    orphan = reg.register(ws, client="orphan")
    reg.mark_retiring(orphan)
    fresh = reg.register(ws, client="fresh")

    reg.confirm_dead(orphan)
    assert reg.get(orphan) is None
    assert reg.get(fresh) is not None
    assert reg.get(fresh).state == "live"


def test_retiring_count_tracks_unconfirmed_orphans_only():
    reg = LiveClientRegistry()
    live = reg.register(uuid.uuid4(), client="live")
    orphan = reg.register(uuid.uuid4(), client="orphan")
    assert reg.retiring_count() == 0

    reg.mark_retiring(orphan)
    assert reg.retiring_count() == 1
    assert reg.unconfirmed_count() == 2

    reg.confirm_dead(orphan)
    assert reg.retiring_count() == 0
    assert reg.unconfirmed_count() == 1
    assert reg.get(live) is not None


def test_mark_retiring_is_idempotent_and_tolerates_unknown_ids():
    reg = LiveClientRegistry()
    lid = reg.register(uuid.uuid4(), client="c")
    assert reg.mark_retiring(lid).state == "retiring"
    assert reg.mark_retiring(lid).state == "retiring"
    assert reg.mark_retiring(uuid.uuid4()) is None
    reg.confirm_dead(uuid.uuid4())          # no raise
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_live_clients.py -p no:cacheprovider -q
```
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `src/call_manager/proposal_assist/drafting/live_clients.py`:

```python
"""Per-generation inventory of live SDK clients (Slice A).

The durable ownership anchor for subprocess teardown. Every path that discards a
handle — capacity eviction, idle sweep, retirement teardown, and the launcher's
own cleanup (which suppresses exceptions) — drops its reference BEFORE
termination is confirmed, and the adapter pops handle.state["client"] before
disconnect. Without this inventory a failed teardown leaves nothing to retry and
nothing to count.

Records are keyed by LAUNCH GENERATION, never by workspace: an unconfirmed
teardown releases the workspace's advisory lock, so a fresh launch for the same
workspace can legitimately coexist with an orphan. Clearing is by identity.

Pure bookkeeping — no SDK types, no I/O, no locking (single event loop).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

LIVE = "live"
RETIRING = "retiring"


@dataclass
class LiveClientRecord:
    launch_id: uuid.UUID
    workspace_id: uuid.UUID
    client: Any
    state: str = LIVE
    teardown: asyncio.Task | None = field(default=None, repr=False)


class LiveClientRegistry:
    """Tracks every SDK client from before connect() until confirmed death."""

    def __init__(self) -> None:
        self._records: dict[uuid.UUID, LiveClientRecord] = {}

    def register(self, workspace_id: uuid.UUID, client: Any) -> uuid.UUID:
        """Record a new generation and return its launch_id. Called BEFORE
        client.connect() so a connect-time failure is still discoverable."""
        launch_id = uuid.uuid4()
        self._records[launch_id] = LiveClientRecord(
            launch_id=launch_id, workspace_id=workspace_id, client=client
        )
        return launch_id

    def mark_retiring(self, launch_id: uuid.UUID) -> LiveClientRecord | None:
        """Flag a generation as being torn down. Idempotent; None if unknown."""
        record = self._records.get(launch_id)
        if record is None:
            return None
        record.state = RETIRING
        return record

    def confirm_dead(self, launch_id: uuid.UUID) -> None:
        """Drop a generation — ONLY after its process is confirmed gone. Clearing
        by identity means a late teardown of an orphan can never remove the
        replacement generation for the same workspace."""
        self._records.pop(launch_id, None)

    def get(self, launch_id: uuid.UUID) -> LiveClientRecord | None:
        return self._records.get(launch_id)

    def find_by_workspace(self, workspace_id: uuid.UUID) -> list[LiveClientRecord]:
        return [r for r in self._records.values() if r.workspace_id == workspace_id]

    def records(self) -> list[LiveClientRecord]:
        """Snapshot — safe to iterate while callers mutate the inventory."""
        return list(self._records.values())

    def retiring_count(self) -> int:
        """Generations whose teardown was attempted but not confirmed — the ones
        the registry's _entries no longer knows about, so capacity accounting must
        add them explicitly."""
        return sum(1 for r in self._records.values() if r.state == RETIRING)

    def unconfirmed_count(self) -> int:
        return len(self._records)
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_live_clients.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/proposal_assist/drafting/live_clients.py \
       tests/proposal_assist/test_live_clients.py
```
Expected: 4 passed; ruff clean.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/drafting/live_clients.py \
  tests/proposal_assist/test_live_clients.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(drafting): per-generation live-client inventory

Every existing path drops its handle before termination is confirmed, so a failed
teardown left nothing to retry or count. Keyed by launch generation (never
workspace) and cleared by identity, so an orphan and its replacement coexist.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: adapter — register before `connect()`, hard-bounded teardown, confirm death

**Files:**
- Modify: `src/call_manager/proposal_assist/drafting/claude_adapter.py` (class **`ClaudeAgentSDKAdapter`**, `:151` `__init__`, `:185` `launch`, `:525-551` `terminate`/force-kill)
- Modify: `src/call_manager/proposal_assist/drafting/fake_adapter.py`
- Test: `tests/proposal_assist/test_claude_adapter_teardown.py` (create)

**Interfaces:**
- Consumes: `LiveClientRegistry`, `RETIRING` (Task 6).
- Produces on `ClaudeAgentSDKAdapter`: `self._live`; `retiring_count() -> int`; `async reconcile_live_clients(is_deleted) -> int`; `AgentHandle.state["launch_id"]`. Same three names on `FakeAgentAdapter`.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_claude_adapter_teardown.py`:

```python
"""Bounded teardown + generation lifecycle for ClaudeAgentSDKAdapter (Slice A)."""
import asyncio
import uuid

import pytest

from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
from call_manager.proposal_assist.drafting.contract import AgentHandle


class _HangingClient:
    """disconnect() ignores cancellation — the shape asyncio.wait_for cannot bound."""

    def __init__(self):
        self.killed = False
        self.reaped = False
        self._transport = self
        self._process = self

    async def disconnect(self):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(3600)

    def kill(self):
        self.killed = True

    async def wait(self):
        self.reaped = True
        return 0


class _CleanClient:
    def __init__(self):
        self.disconnected = False
        self.killed = False

    async def disconnect(self):
        self.disconnected = True

    def kill(self):
        self.killed = True


def _adapter(tmp_path, **kw):
    # Keyword-only; workspaces_dir is typed str.
    return ClaudeAgentSDKAdapter(workspaces_dir=str(tmp_path), model="m", **kw)


def _handle(adapter, client):
    ws = uuid.uuid4()
    launch_id = adapter._live.register(ws, client)
    return ws, launch_id, AgentHandle(
        workspace_id=ws, state={"client": client, "launch_id": launch_id}
    )


@pytest.mark.asyncio
async def test_clean_disconnect_confirms_death_and_clears_the_record(tmp_path):
    adapter = _adapter(tmp_path)
    client = _CleanClient()
    _ws, launch_id, handle = _handle(adapter, client)

    await adapter.terminate(handle)

    assert client.disconnected is True
    assert client.killed is False
    assert adapter._live.get(launch_id) is None
    assert adapter.retiring_count() == 0


@pytest.mark.asyncio
async def test_hanging_disconnect_is_force_killed_at_the_deadline_and_reaped(tmp_path):
    adapter = _adapter(tmp_path, disconnect_deadline_seconds=0.05, reap_deadline_seconds=0.5)
    client = _HangingClient()
    _ws, launch_id, handle = _handle(adapter, client)

    await asyncio.wait_for(adapter.terminate(handle), timeout=5)   # must NOT hang

    assert client.killed is True
    assert client.reaped is True
    assert adapter._live.get(launch_id) is None


@pytest.mark.asyncio
async def test_unreapable_client_stays_retiring_for_the_sweep(tmp_path):
    class _Unkillable(_HangingClient):
        def kill(self):
            raise RuntimeError("no such process")

    adapter = _adapter(tmp_path, disconnect_deadline_seconds=0.05, reap_deadline_seconds=0.2)
    client = _Unkillable()
    _ws, launch_id, handle = _handle(adapter, client)

    await asyncio.wait_for(adapter.terminate(handle), timeout=5)

    rec = adapter._live.get(launch_id)
    assert rec is not None and rec.state == "retiring"     # retained for retry
    assert adapter.retiring_count() == 1                   # and counted against capacity


@pytest.mark.asyncio
async def test_connect_failure_still_retires_the_generation(tmp_path, monkeypatch):
    """Registration happens BEFORE connect(), so a connect-time failure is
    discoverable instead of leaving an unowned half-built subprocess."""
    adapter = _adapter(tmp_path, disconnect_deadline_seconds=0.05, reap_deadline_seconds=0.5)
    client = _CleanClient()

    class _BoomConnect(_CleanClient):
        async def connect(self):
            raise RuntimeError("connect failed")

    client = _BoomConnect()
    monkeypatch.setattr(
        "call_manager.proposal_assist.drafting.claude_adapter.ClaudeSDKClient",
        lambda options: client,
    )
    from call_manager.proposal_assist.drafting.contract import LaunchContext

    with pytest.raises(RuntimeError):
        await adapter.launch(LaunchContext(workspace_id=uuid.uuid4()))

    assert client.disconnected is True          # cleanup ran through the inventory
    assert adapter._live.unconfirmed_count() == 0


@pytest.mark.asyncio
async def test_reconcile_retries_every_retiring_record_even_on_live_workspaces(tmp_path):
    """Capacity-eviction / idle-sweep orphans belong to LIVE workspaces — they must
    be retried too, not only deleted ones."""
    adapter = _adapter(tmp_path, disconnect_deadline_seconds=0.05, reap_deadline_seconds=0.5)
    client = _HangingClient()
    ws = uuid.uuid4()
    launch_id = adapter._live.register(ws, client)
    adapter._live.mark_retiring(launch_id)

    n = await adapter.reconcile_live_clients(is_deleted=lambda _ws: False)

    assert n >= 1
    await asyncio.sleep(0.3)
    assert client.killed is True
    assert adapter._live.get(launch_id) is None


@pytest.mark.asyncio
async def test_reconcile_retires_live_records_of_deleted_workspaces(tmp_path):
    adapter = _adapter(tmp_path, disconnect_deadline_seconds=0.05, reap_deadline_seconds=0.5)
    client = _CleanClient()
    ws = uuid.uuid4()
    launch_id = adapter._live.register(ws, client)

    await adapter.reconcile_live_clients(is_deleted=lambda w: w == ws)
    await asyncio.sleep(0.2)

    assert client.disconnected is True
    assert adapter._live.get(launch_id) is None


@pytest.mark.asyncio
async def test_reconcile_does_not_spawn_duplicate_teardowns(tmp_path):
    adapter = _adapter(tmp_path, disconnect_deadline_seconds=5, reap_deadline_seconds=5)
    client = _HangingClient()
    launch_id = adapter._live.register(uuid.uuid4(), client)
    adapter._live.mark_retiring(launch_id)

    await adapter.reconcile_live_clients(is_deleted=lambda _w: False)
    first = adapter._live.get(launch_id).teardown
    await adapter.reconcile_live_clients(is_deleted=lambda _w: False)
    second = adapter._live.get(launch_id).teardown

    assert first is second          # one tracked task per record, reused
    first.cancel()
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_claude_adapter_teardown.py -p no:cacheprovider -q
```
Expected: FAIL — no `_live` / `retiring_count` / `reconcile_live_clients`; `__init__` rejects the deadline kwargs.

- [ ] **Step 3a: Constructor + launch registration**

In `claude_adapter.py`, add to `ClaudeAgentSDKAdapter.__init__` (keeping every existing parameter):

```python
        disconnect_deadline_seconds: float = 3.0,
        reap_deadline_seconds: float = 2.0,
```
body:
```python
        self._disconnect_deadline_seconds = disconnect_deadline_seconds
        self._reap_deadline_seconds = reap_deadline_seconds
        self._live = LiveClientRegistry()
```
import: `from call_manager.proposal_assist.drafting.live_clients import RETIRING, LiveClientRegistry`

Change `launch` (`:199-205`):

```python
        client = ClaudeSDKClient(options=options)
        # Register BEFORE connect(): a connect-time failure otherwise leaves a
        # half-built subprocess nobody owns. This record is what the reconciliation
        # sweep and the capacity ceiling both count.
        launch_id = self._live.register(context.workspace_id, client)
        try:
            await client.connect()
        except BaseException:
            await self._retire_generation(launch_id)
            raise

        return AgentHandle(
            workspace_id=context.workspace_id,
            state={"client": client, "launch_id": launch_id},
        )
```

- [ ] **Step 3b: Bounded teardown + reconciliation**

Replace `terminate`'s body and add the helpers:

```python
    async def terminate(self, handle: AgentHandle) -> None:
        """Disconnect the SDK client, bounded. Safe to call even if never connected."""
        self._tool_context_sink.pop(handle.workspace_id, None)
        launch_id = handle.state.get("launch_id")
        client = handle.state.pop("client", None)
        if launch_id is None:
            # Pre-inventory handle (bare-adapter tests): best-effort legacy path.
            if client is not None:
                await self._disconnect_bounded(client, handle.workspace_id)
            return
        await self._retire_generation(launch_id)

    async def _retire_generation(self, launch_id: uuid.UUID) -> None:
        """Mark the generation retiring and await ONE tracked teardown task for it.
        Shared by retirement, capacity eviction, idle sweep and reconciliation, so
        concurrent callers never spawn a second kill for the same generation."""
        record = self._live.mark_retiring(launch_id)
        if record is None:
            return
        if record.teardown is None or record.teardown.done():
            record.teardown = asyncio.create_task(self._teardown_record(record))
        await asyncio.shield(record.teardown)

    async def _teardown_record(self, record) -> None:
        confirmed = await self._disconnect_bounded(record.client, record.workspace_id)
        if confirmed:
            self._live.confirm_dead(record.launch_id)
        else:
            logger.warning(
                "drafting subprocess for workspace %s not confirmed dead; retained "
                "for the reconciliation sweep",
                record.workspace_id,
                extra={"event": "drafting_termination_unconfirmed",
                       "workspace_id": str(record.workspace_id)},
            )

    async def _disconnect_bounded(self, client, workspace_id: uuid.UUID) -> bool:
        """Disconnect within a hard deadline; force-kill + reap on overrun.

        asyncio.wait_for waits for CANCELLATION to finish, so a
        cancellation-resistant disconnect() would never reach force-kill. Run it as
        a tracked task and fire the kill from an INDEPENDENT timer. Returns True
        only when death is confirmed (clean disconnect, or kill + successful reap).
        """
        task = asyncio.create_task(client.disconnect())
        done, _ = await asyncio.wait({task}, timeout=self._disconnect_deadline_seconds)
        if task in done:
            exc = task.exception()
            if exc is None:
                return True
            logger.warning(
                "SDK client disconnect failed for workspace %s; force-killing subprocess",
                workspace_id, exc_info=exc,
            )
        else:
            logger.warning(
                "SDK client disconnect timed out for workspace %s; force-killing subprocess",
                workspace_id,
            )
            task.cancel()   # best-effort; NOT awaited (it may resist)
        if not self._force_kill_subprocess(client, workspace_id):
            return False
        return await self._reap_subprocess(client, workspace_id)

    async def _reap_subprocess(self, client, workspace_id: uuid.UUID) -> bool:
        """Confirmed death means REAPED, not merely signalled."""
        transport = getattr(client, "_transport", None)
        process = getattr(transport, "_process", None) if transport is not None else None
        wait = getattr(process, "wait", None)
        if wait is None:
            return False
        try:
            await asyncio.wait_for(wait(), timeout=self._reap_deadline_seconds)
            return True
        except Exception:
            logger.warning(
                "drafting subprocess for workspace %s was killed but not reaped",
                workspace_id, exc_info=True,
            )
            return False

    def retiring_count(self) -> int:
        """Generations whose death is not confirmed — counted against the live
        ceiling by SubprocessRegistry."""
        return self._live.retiring_count()

    async def reconcile_live_clients(self, is_deleted) -> int:
        """Ensure a teardown is running for (a) every RETIRING generation — whose
        workspace may well be LIVE (capacity-eviction / idle-sweep orphans) — and
        (b) every generation of a soft-deleted workspace. Returns the number acted
        on. Guarantees a remediation ATTEMPT, not that the process died."""
        acted = 0
        for record in self._live.records():
            if record.state != RETIRING and not is_deleted(record.workspace_id):
                continue
            self._live.mark_retiring(record.launch_id)
            if record.teardown is None or record.teardown.done():
                record.teardown = asyncio.create_task(self._teardown_record(record))
            acted += 1
        return acted
```

Change `_force_kill_subprocess` to **return `bool`**: `True` only when `kill()` was invoked; `False` on the "no subprocess handle reachable" branch and in the `except` branch (both keep their existing log messages).

- [ ] **Step 3c: Mirror the surface on `FakeAgentAdapter`**

In `fake_adapter.py` `__init__` add `self._live = LiveClientRegistry()` (import it), and add:

```python
    def retiring_count(self) -> int:
        return self._live.retiring_count()

    async def reconcile_live_clients(self, is_deleted) -> int:
        return 0        # the fake owns no subprocesses
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_claude_adapter_teardown.py tests/proposal_assist/test_live_clients.py \
  tests/proposal_assist/test_drafting_registry.py tests/proposal_assist/test_drafting_contract.py \
  tests/proposal_assist/test_drafting_factory.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/proposal_assist/drafting/claude_adapter.py \
       src/call_manager/proposal_assist/drafting/fake_adapter.py
```

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/drafting/claude_adapter.py \
  src/call_manager/proposal_assist/drafting/fake_adapter.py \
  tests/proposal_assist/test_claude_adapter_teardown.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(drafting): generation-tracked, hard-bounded subprocess teardown

Registers each SDK client BEFORE connect(), tears down via ONE tracked task per
generation, and clears the record only on CONFIRMED death (clean disconnect, or
kill + reap). Disconnect is bounded by an independent timer, not wait_for, which
waits for cancellation to complete and would never force-kill.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: capacity ceiling — count orphans **and** re-check after eviction teardown

**Files:**
- Modify: `src/call_manager/proposal_assist/drafting/registry.py:127-153`
- Test: `tests/proposal_assist/test_drafting_registry_capacity.py`

**Interfaces:**
- Consumes: `adapter.retiring_count()` (Task 7).
- Produces: `in_use = len(_entries) + len(_pending_launches) + adapter.retiring_count()`, evaluated **both** before victim selection **and again after the victim's teardown**; on the second check failing, the pending reservation is failed and `DraftingAtCapacity` is raised.

**Why the re-check (review P1-3):** the count runs *before* the victim is chosen. The registry then pops the victim, installs the replacement's pending reservation, tears down **outside the lock**, and returns launcher ownership — so if that teardown ends unconfirmed, v1's code would still launch the replacement and exceed the ceiling.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/test_drafting_registry_capacity.py`:

```python
@pytest.mark.asyncio
async def test_unconfirmed_generation_counts_toward_the_ceiling():
    """An orphan whose death is unconfirmed still pins a subprocess (and its pooled
    DB connection), so it must occupy a slot rather than being invisible."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600, max_live=1)

    launch_id = adapter._live.register(uuid.uuid4(), client=object())
    adapter._live.mark_retiring(launch_id)
    assert adapter.retiring_count() == 1
    assert registry._entries == {}

    with pytest.raises(DraftingAtCapacity):
        await registry.claim_launch_or_wait(uuid.uuid4())

    adapter._live.confirm_dead(launch_id)
    am_launcher, _ = await registry.claim_launch_or_wait(uuid.uuid4())
    assert am_launcher is True


@pytest.mark.asyncio
async def test_eviction_whose_teardown_is_unconfirmed_refuses_the_replacement():
    """The eviction path pops the victim, reserves the replacement, and tears down
    OUTSIDE the lock. If that teardown ends unconfirmed the slot was never freed —
    the replacement must be refused, and its reservation cleaned up, BEFORE any
    adapter.launch."""
    adapter = FakeAgentAdapter()

    # The victim's teardown leaves an unconfirmed generation behind.
    async def _terminate_unconfirmed(handle):
        adapter.terminations.append(handle)
        lid = adapter._live.register(handle.workspace_id, client=object())
        adapter._live.mark_retiring(lid)          # died? unknown.

    adapter.terminate = _terminate_unconfirmed
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600, max_live=1)

    ws_victim = uuid.uuid4()
    await _install_handle(registry, ws_victim)
    assert len(registry._entries) == 1

    with pytest.raises(DraftingAtCapacity):
        await registry.claim_launch_or_wait(uuid.uuid4())

    # The refused launcher must not leave a dangling reservation behind.
    assert registry._pending_launches == {}
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_drafting_registry_capacity.py -p no:cacheprovider -q \
  -k "unconfirmed or eviction_whose"
```
Expected: FAIL — no `DraftingAtCapacity` (orphans invisible; no post-teardown re-check).

- [ ] **Step 3: Implement**

Add a helper to `SubprocessRegistry`:

```python
    def _unconfirmed_generations(self) -> int:
        """Generations whose death the adapter has not confirmed. They may still
        pin a subprocess and a pooled DB connection, so they occupy slots.
        Adapters without an inventory report 0."""
        counter = getattr(self._adapter, "retiring_count", None)
        return counter() if counter is not None else 0
```

Replace the capacity computation inside `claim_launch_or_wait`:

```python
            if self._max_live is not None:
                in_use = (
                    len(self._entries)
                    + len(self._pending_launches)
                    + self._unconfirmed_generations()
                )
                if in_use >= self._max_live:
                    victim_id = self._pick_lru_idle_victim_locked()
                    if victim_id is None:
                        raise DraftingAtCapacity(
                            f"drafting at capacity ({self._max_live} live subprocesses, "
                            "all mid-turn); retry shortly"
                        )
                    victim = self._entries.pop(victim_id)
```

And after the existing out-of-lock teardown of the victim, re-check before handing back launcher ownership:

```python
        if victim is not None and victim_id is not None:
            await self._teardown_entry(victim_id, victim, context="capacity-evict")
            # The teardown may have ended UNCONFIRMED, in which case the victim's
            # process (and its pooled connection) may still be alive and the slot
            # was never really freed. Re-check before launching the replacement;
            # surrender the reservation rather than exceed the ceiling.
            async with self._lock:
                over = self._max_live is not None and (
                    len(self._entries)
                    + len(self._pending_launches)
                    + self._unconfirmed_generations()
                ) > self._max_live
            if over:
                exc = DraftingAtCapacity(
                    f"drafting at capacity ({self._max_live} live subprocesses; an "
                    "evicted subprocess is not confirmed dead); retry shortly"
                )
                await self.fail_pending_launch(workspace_id, exc)
                raise exc
        return (True, None)
```

Extend the `claim_launch_or_wait` docstring's capacity paragraph:

```
        Unconfirmed-dead generations (retiring records in the adapter's live-client
        inventory) also occupy slots, and the ceiling is RE-CHECKED after an
        eviction's teardown: if the victim's death is unconfirmed the replacement is
        refused with DraftingAtCapacity (retriable 429) instead of exceeding the cap.
```

- [ ] **Step 4: Run to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_drafting_registry_capacity.py tests/proposal_assist/test_drafting_registry.py \
  -p no:cacheprovider -q && uv run ruff check src/call_manager/proposal_assist/drafting/registry.py
```

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/drafting/registry.py \
  tests/proposal_assist/test_drafting_registry_capacity.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
fix(drafting): count unconfirmed generations and re-check after eviction

Capacity counted only _entries + _pending_launches, so an unconfirmed-dead
process bypassed DRAFTING_MAX_LIVE_SUBPROCESSES. The count now includes orphans
AND is re-evaluated after the eviction victim's teardown — if that teardown ended
unconfirmed the reservation is failed and DraftingAtCapacity raised before any
adapter.launch, instead of silently exceeding the ceiling.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: reconciliation — registry entries **then** adapter generations

**Files:**
- Modify: `src/call_manager/proposal_assist/drafting/registry.py` (add `reconcile_deleted_entries`)
- Modify: `src/call_manager/proposal_assist/drafting/controller.py` (add `reconcile_live_clients`)
- Modify: `src/call_manager/scheduler.py:610-640`
- Test: `tests/proposal_assist/test_drafting_controller.py`, `tests/proposal_assist/test_drafting_scheduler_tick.py`

**Interfaces:**
- Produces: `registry.reconcile_deleted_entries(is_deleted) -> int`; `controller.reconcile_live_clients() -> int` (registry pass **then** adapter pass); the scheduler's idle-sweep tick calls it.

**Why the registry pass (review P1-4):** adapter-level reconciliation cannot touch `_entries`, so a soft-deleted workspace's cached entry — and the `lock_session` holding its advisory lock — would persist forever (the idle sweep deliberately skips `in_flight > 0`). `_teardown_entry` is what releases the lock.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/test_drafting_controller.py`:

```python
@pytest.mark.asyncio
async def test_reconcile_pops_deleted_entries_even_when_in_flight(pg_session_factory, tmp_path):
    """A deleted workspace's entry must be torn down (releasing its advisory-lock
    session) even mid-turn — the idle sweep deliberately skips in_flight > 0."""
    from datetime import UTC, datetime

    from call_manager.db.models import Workspace
    from tests.proposal_assist.conftest import make_user, make_workspace

    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory, workspaces_dir=tmp_path
    )
    with pg_session_factory() as s:
        live = make_workspace(s, owner=make_user(s, email="live-owner@ki.se"))
        dead = make_workspace(s, owner=make_user(s, email="dead-owner@ki.se"))
        s.commit()
        live_id, dead_id = live.id, dead.id

    await controller.launch(live_id)
    await controller.launch(dead_id)
    await registry.increment_in_flight(dead_id)          # mid-turn
    with pg_session_factory() as s:
        s.get(Workspace, dead_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    n = await controller.reconcile_live_clients()

    assert n >= 1
    assert dead_id not in registry._entries              # entry popped, lock released
    assert live_id in registry._entries                  # live workspace untouched
    assert any(h.workspace_id == dead_id for h in adapter.terminations)
    await controller.terminate(live_id)


@pytest.mark.asyncio
async def test_reconcile_passes_soft_delete_predicate_to_the_adapter(pg_session_factory, tmp_path):
    seen: list = []

    class _RecordingAdapter(FakeAgentAdapter):
        async def reconcile_live_clients(self, is_deleted):
            seen.append(is_deleted)
            return 0

    from datetime import UTC, datetime

    from call_manager.db.models import Workspace
    from tests.proposal_assist.conftest import make_user, make_workspace

    adapter = _RecordingAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory, workspaces_dir=tmp_path
    )
    with pg_session_factory() as s:
        live = make_workspace(s, owner=make_user(s, email="p-live@ki.se"))
        dead = make_workspace(s, owner=make_user(s, email="p-dead@ki.se"))
        s.commit()
        live_id, dead_id = live.id, dead.id
    with pg_session_factory() as s:
        s.get(Workspace, dead_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    await controller.reconcile_live_clients()

    assert len(seen) == 1
    assert seen[0](dead_id) is True
    assert seen[0](live_id) is False
```

Append to `tests/proposal_assist/test_drafting_scheduler_tick.py`:

```python
def test_idle_sweep_tick_also_reconciles(monkeypatch):
    """The scheduled tick must run BOTH passes — otherwise reconciliation never
    runs in production."""
    called = {"sweep": 0, "reconcile": 0}

    class _Ctrl:
        async def sweep_idle(self):
            called["sweep"] += 1
            return 0

        async def reconcile_live_clients(self):
            called["reconcile"] += 1
            return 0

    # Build the scheduler job exactly as the neighbouring tick tests do, passing
    # _Ctrl() as workspace_controller and app_loop=None, then invoke the job fn.
    # ... reuse this module's existing tick-invocation helper ...

    assert called == {"sweep": 1, "reconcile": 1}
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_drafting_controller.py tests/proposal_assist/test_drafting_scheduler_tick.py \
  -p no:cacheprovider -q -k "reconcile"
```
Expected: FAIL — `WorkspaceController` has no `reconcile_live_clients`; the tick calls only `sweep_idle`.

- [ ] **Step 3a: Registry pass**

Add to `SubprocessRegistry` (mirroring `sweep_idle`'s I2 discipline — the blocking predicate runs **outside** the lock):

```python
    async def reconcile_deleted_entries(self, is_deleted) -> int:
        """Pop and tear down entries whose workspace is soft-deleted, REGARDLESS of
        in_flight (a running turn on a deleted workspace is exactly the target; the
        idle sweep deliberately skips those). _teardown_entry also releases the
        advisory lock and closes the lock_session — something adapter-level
        reconciliation cannot do. Returns the number torn down."""
        async with self._lock:
            candidates = list(self._entries.keys())
        # Blocking DB predicate OUTSIDE the lock (I2), then re-acquire to pop.
        deleted = [wsid for wsid in candidates if is_deleted(wsid)]
        doomed: list[tuple[uuid.UUID, _Entry]] = []
        async with self._lock:
            for wsid in deleted:
                entry = self._entries.pop(wsid, None)
                if entry is not None:
                    doomed.append((wsid, entry))
        for wsid, entry in doomed:
            await self._teardown_entry(wsid, entry, context="retire-reconcile")
        return len(doomed)
```

- [ ] **Step 3b: Controller method**

Add to `controller.py` next to `sweep_idle`:

```python
    async def reconcile_live_clients(self) -> int:
        """Retry teardown for everything a deletion or a failed teardown left behind.

        TWO passes, in order: (1) the REGISTRY pass pops entries for soft-deleted
        workspaces even mid-turn and releases their advisory locks; (2) the ADAPTER
        pass retries detached generations — retiring records (whose workspace may be
        LIVE: capacity-eviction / idle-sweep / launcher-cleanup orphans) plus any
        generation of a deleted workspace. Guarantees a remediation ATTEMPT each
        tick, not that a process died. Never raises: one bad record must not kill
        the tick."""
        total = 0
        try:
            total += await self._registry.reconcile_deleted_entries(self._workspace_is_deleted)
        except Exception:
            logger.exception("reconcile_live_clients: registry pass failed")
        reconcile = getattr(self._registry._adapter, "reconcile_live_clients", None)
        if reconcile is not None:
            try:
                total += await reconcile(is_deleted=self._workspace_is_deleted)
            except Exception:
                logger.exception("reconcile_live_clients: adapter pass failed")
        return total
```

- [ ] **Step 3c: Scheduler wiring**

In `scheduler.py`'s `_drafting_idle_sweep`, replace both `workspace_controller.sweep_idle()` call sites with:

```python
        async def _sweep_and_reconcile():
            await workspace_controller.sweep_idle()
            # Same tick: retry anything a deletion or a failed teardown left behind
            # (Slice A). Cheap when there is nothing to do.
            await workspace_controller.reconcile_live_clients()

        if app_loop is None:
            try:
                asyncio.run(_sweep_and_reconcile())
            except Exception:
                _logger.exception("drafting.idle_sweep failed")
            return

        fut = asyncio.run_coroutine_threadsafe(_sweep_and_reconcile(), app_loop)
```
(The `fut.result(timeout=300)` block is unchanged.)

- [ ] **Step 4: Run to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_drafting_controller.py tests/proposal_assist/test_drafting_scheduler_tick.py \
  tests/proposal_assist/test_claude_adapter_teardown.py tests/proposal_assist/test_drafting_registry.py \
  -p no:cacheprovider -q && uv run ruff check \
  src/call_manager/proposal_assist/drafting/registry.py \
  src/call_manager/proposal_assist/drafting/controller.py src/call_manager/scheduler.py
```

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/drafting/registry.py \
  src/call_manager/proposal_assist/drafting/controller.py src/call_manager/scheduler.py \
  tests/proposal_assist/test_drafting_controller.py \
  tests/proposal_assist/test_drafting_scheduler_tick.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(drafting): reconcile deleted entries AND detached generations each tick

Two passes: the registry pass pops soft-deleted workspaces' entries even when
in_flight > 0 (the idle sweep skips those) and releases their advisory locks —
which adapter-level reconciliation cannot do; the adapter pass retries retiring
generations, including orphans on LIVE workspaces. Wired into the sweep tick, with
a test proving the tick runs both.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Acceptance gates

**Files:** none (verification only). Any failure → fix on-branch, re-run.

- [ ] **Step 1: Grouped backend sweep**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist tests/test_workspaces_service.py tests/test_api_spec.py \
  -p no:cacheprovider -q
```
Expected: all pass. `connection refused` from testcontainers PG is an infra flake → re-run the flagged file isolated.

- [ ] **Step 2: Lint (whole repo, as CI runs it)**

```bash
cd /home/parlar_ai/dev/call-monitor && uv run ruff check .
```
Expected: `All checks passed!`

- [ ] **Step 3: Frontend regression (untouched — must stay green)**

```bash
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm exec tsc --noEmit
cd /home/parlar_ai/dev/call-monitor
```
Expected: silent; zero frontend changes, no `schema.gen.ts` diff. **Re-anchor to the repo root** (the second `cd`) — a lingering `cd frontend` makes later relative `git`/`pytest` paths resolve wrongly.

- [ ] **Step 4: Diff-scope check**

```bash
git -C /home/parlar_ai/dev/call-monitor diff --stat master...workspace-retirement-slice-a
```
Expected: only the files in the File Structure table. **No migration, no frontend, no `schema.gen.ts`.**

- [ ] **Step 5: Honest-scope gate (must FAIL the build if violated)**

```bash
cd /home/parlar_ai/dev/call-monitor
if git diff master...workspace-retirement-slice-a -- '*.py' \
     | grep -nEi "no send after|proves.*dead|guarantees.*death|no relaunch|cannot stream|containment within"; then
  echo "FAIL: overclaiming language introduced — Slice A promises a remediation ATTEMPT, not confirmed death or impossibility of relaunch"
  exit 1
fi
echo "OK: no overclaiming language"
```
Expected: `OK: no overclaiming language`.
(v1's gate used `grep … || echo`, which exits 0 whether or not the pattern matches — it could never fail. This form exits 1 on a match.)

---

## Self-Review (completed at plan-writing time)

- **Spec coverage:** A1 → Tasks 1–3; A2 → Task 4 (incl. the conceded-race test v8 requires); A5 → Task 5; A3 → Tasks 6–9; A4 → Task 7 (`_disconnect_bounded` + `_reap_subprocess`); Slice-A gates → Task 10.
- **Review fixes folded:** P1-1 → Task 5 (finalizers return "won the row"; `usage_reported`; stamp only when lost **and** reported; delete-during-stream test). P1-2 → Task 2 (`SELECT … FOR UPDATE` + terminal/workspace validation + two-thread concurrency test). P1-3 → Task 8 (post-teardown re-check, `fail_pending_launch`, eviction test). P1-4 → Task 9 (`registry.reconcile_deleted_entries`, in-flight test, scheduler tick test). P2-5 → Codebase-facts table + corrected `ClaudeAgentSDKAdapter`, `make_chat_turn(s, ws, role=…, content=…)`, explicit distinct user emails, `controller.terminate` in the cached test, connect-failure test. P2-6 → Task 10 Step 5 hard-fail gate incl. `no relaunch`, and the Goal line no longer claims relaunch is impossible.
- **Type consistency:** `record_terminal_outcome(..., usage_unknown: bool = False) -> bool` (T1 def, T3 call); `stamp_post_terminal_usage(...) -> bool` (T2 def, T5 call — argument names identical); `_finalize_success/_finalize_failed -> bool` (T5 def + call sites); `LiveClientRegistry` API (T6 def; T7/T8 use); `retiring_count()` (T7 def, T8 consumer); `reconcile_live_clients(is_deleted=…)` keyword-consistent across adapter (T7), fake (T7), controller (T9), recording fake (T9 test); `reconcile_deleted_entries(is_deleted)` (T9 def + controller call); `WorkspaceRetired(workspace_id)` (T4 def, T5/T7 use); `AgentHandle.state["launch_id"]` (T7 set + read).
- **Placeholder scan:** the only prose-deferred spots are Task 5's and Task 9's test preambles, which reuse each module's existing construction helpers rather than inventing divergent ones; every assertion, monkeypatch target and signature in them is exact.
- **Ordering:** `WorkspaceRetired` (T4) precedes its use (T5); `stamp_post_terminal_usage` (T2) precedes its caller (T5); `LiveClientRegistry` (T6) precedes T7–T9; `retiring_count` (T7) precedes T8.

## Execution Handoff

Two execution options:

**1. Subagent-Driven** — a fresh subagent per task with review between tasks.

**2. Inline Execution** — execute in this session via `superpowers:executing-plans`, batching with checkpoints.
