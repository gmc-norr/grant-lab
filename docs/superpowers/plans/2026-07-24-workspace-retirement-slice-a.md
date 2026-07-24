# Workspace Retirement — Slice A (durable floor + generation lifecycle) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a workspace is deleted, its running turn reaches a terminal row with exact token accounting, no relaunch is attempted, and every SDK subprocess generation is tracked until confirmed dead — with orphans retried each sweep and counted against the live-subprocess ceiling.

**Architecture:** Three durable mechanisms plus one lifecycle record. (1) The delete transaction terminalizes `running` turn rows, so the processor's existing conditional `UPDATE ... WHERE status='running'` becomes a no-op — the TOCTOU closes via same-row contention. (2) `controller.launch` checks `soft_deleted_at` above `claim_launch_or_wait`, narrowing (not closing) the relaunch window. (3) A per-generation live-client inventory in the adapter tracks every SDK client from before `connect()` until confirmed death, is retried by a reconciliation sweep, and counts toward `DRAFTING_MAX_LIVE_SUBPROCESSES`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, `claude-agent-sdk`, pytest (`pytest.mark.asyncio` for drafting/registry tests, real-PG fixtures for DB tests). No new dependencies.

## Global Constraints

- **Backend-only. No migration. No frontend change. No OpenAPI/`schema.gen.ts` change.**
- Slice A is **standalone and independently mergeable** — it must not require the Slice-B coordinator, route change, or collab work.
- **Explicitly NOT claimed by this slice** (do not write tests or docstrings asserting otherwise): that any code path proves a subprocess died; that the reconciliation sweep *contains* a leak (it guarantees a remediation **attempt each tick**); that non-mutex writers are covered.
- Backend tests: `EMBEDDING_MODEL=bge-m3 uv run pytest <paths> -p no:cacheprovider` — grouped dirs, **never** whole-suite; final sweep includes `tests/test_api_spec.py`.
- Drafting/registry/adapter tests use `@pytest.mark.asyncio` (NOT anyio — that is the collab convention).
- Lint: `uv run ruff check .` (line-length 100). CI runs `ruff check` only, never `ruff format`.
- Explicit-path `git add` only (never `-A`). Merges are **local `--no-ff` only**, never pushed without an explicit ask. Do NOT invoke `/code-review` or `/ultrareview`.
- Every commit message ends with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- All git commands use `git -C /home/parlar_ai/dev/call-monitor`.

## File Structure

| File | Role |
|---|---|
| `src/call_manager/proposal_assist/drafting/live_clients.py` | **Create** — `LiveClientRecord` + `LiveClientRegistry`: pure per-generation bookkeeping (no SDK types), the durable ownership anchor |
| `src/call_manager/proposal_assist/turns/outcomes.py` | Modify — `record_terminal_outcome(usage_unknown=...)`; new `stamp_post_terminal_usage` |
| `src/call_manager/workspaces.py` | Modify — delete txn terminalizes `running` turn rows |
| `src/call_manager/proposal_assist/turns/processor.py` | Modify — guarded region over post-claim launch; catch `WorkspaceRetired`; route late usage through the stamp |
| `src/call_manager/proposal_assist/drafting/contract.py` | Modify — add `WorkspaceRetired` exception |
| `src/call_manager/proposal_assist/drafting/controller.py` | Modify — pre-claim soft-delete check; `reconcile_live_clients()`; `_workspace_is_deleted()` |
| `src/call_manager/proposal_assist/drafting/claude_adapter.py` | Modify — register generation before `connect()`; bounded disconnect + timer force-kill + reap; confirm death |
| `src/call_manager/proposal_assist/drafting/registry.py` | Modify — capacity counts retiring generations |
| `src/call_manager/proposal_assist/drafting/fake_adapter.py` | Modify — mirror the inventory surface so registry/controller tests work |
| `src/call_manager/scheduler.py` | Modify — sweep tick also runs reconciliation |
| `tests/proposal_assist/test_live_clients.py` | **Create** — `LiveClientRegistry` unit tests |
| `tests/proposal_assist/test_turn_outcomes.py` | Extend — `usage_unknown`, `stamp_post_terminal_usage` |
| `tests/test_workspaces_service.py` | Extend — running-row terminalization + `_finalize_success` no-op |
| `tests/proposal_assist/test_drafting_controller.py` | Extend — pre-claim check (incl. cached path), reconciliation |
| `tests/proposal_assist/test_drafting_registry_capacity.py` | Extend — retiring generations count toward the ceiling |
| `tests/proposal_assist/test_claude_adapter_teardown.py` | **Create** — bounded disconnect, timer force-kill, reap, confirm-death |
| `tests/proposal_assist/test_turns_processor.py` | Extend — guarded region + late-usage stamp |

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
Expected: `Switched to a new branch 'workspace-retirement-slice-a'`.

---

### Task 1: `record_terminal_outcome(usage_unknown=...)` retains the estimate

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/outcomes.py:41-90`
- Test: `tests/proposal_assist/test_turn_outcomes.py`

**Interfaces:**
- Produces: `record_terminal_outcome(..., usage_unknown: bool = False) -> bool`. When `True`, the submit-time user-row estimate (`ChatTurn.tokens_in`) is **retained** instead of cleared. Default `False` = today's behavior, byte-for-byte.

**Why:** `record_terminal_outcome` computes `consumed = tokens_in > 0 or tokens_out > 0` and, when nothing was consumed, clears the pre-count (`user_turn.tokens_in = None`). Terminalizing a *running* turn at `0/0` would erase the estimate while the real usage is later rolled back by the terminal-row guard — repeated start-and-delete would then spend invisibly against the daily cap.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_turn_outcomes.py`:

```python
def test_usage_unknown_retains_submit_time_estimate(pg_session_factory):
    # A running turn terminalized by workspace deletion has consumed input we
    # cannot measure yet. The submit-time estimate MUST survive, or the spend
    # disappears from the daily cap.
    with pg_session_factory() as s:
        ws = make_workspace(s)
        user = make_user(s)
        user_turn = make_chat_turn(s, workspace_id=ws.id, role="user")
        user_turn.tokens_in = 1234  # submit-time estimate
        job = TurnJob(
            workspace_id=ws.id,
            status="running",
            user_turn_id=user_turn.id,
            submitted_by_user_id=user.id,
        )
        s.add(job)
        s.commit()
        job_id, ut_id = job.id, user_turn.id

    with pg_session_factory() as s:
        did = record_terminal_outcome(
            s,
            turn_job_id=job_id,
            workspace_id=ws.id,
            outcome="cancelled",
            expected_status="running",
            raw_error="workspace soft-deleted",
            emit_chat=False,
            usage_unknown=True,
        )
        s.commit()
        assert did is True

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status == "cancelled"
        assert s.get(ChatTurn, ut_id).tokens_in == 1234   # estimate RETAINED


def test_default_still_clears_estimate_when_nothing_consumed(pg_session_factory):
    # Byte-for-byte guard: the pre-existing queued/reaped path is unchanged.
    with pg_session_factory() as s:
        ws = make_workspace(s)
        user = make_user(s)
        user_turn = make_chat_turn(s, workspace_id=ws.id, role="user")
        user_turn.tokens_in = 999
        job = TurnJob(
            workspace_id=ws.id,
            status="queued",
            user_turn_id=user_turn.id,
            submitted_by_user_id=user.id,
        )
        s.add(job)
        s.commit()
        job_id, ut_id = job.id, user_turn.id

    with pg_session_factory() as s:
        record_terminal_outcome(
            s,
            turn_job_id=job_id,
            workspace_id=ws.id,
            outcome="cancelled",
            expected_status="queued",
            raw_error="workspace soft-deleted",
            emit_chat=False,
        )
        s.commit()

    with pg_session_factory() as s:
        assert s.get(ChatTurn, ut_id).tokens_in is None   # cleared, as before
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_turn_outcomes.py -p no:cacheprovider -q -k "usage_unknown or clears_estimate"
```
Expected: FAIL — `record_terminal_outcome() got an unexpected keyword argument 'usage_unknown'`.

- [ ] **Step 3: Implement**

In `outcomes.py`, add the parameter to the signature (after `tokens_out`):

```python
    tokens_out: int = 0,
    usage_unknown: bool = False,
```

Extend the docstring with:

```
    usage_unknown: True when the turn was terminalized while RUNNING and its
        real usage is not yet known (workspace deletion). The submit-time
        estimate on the user row is RETAINED rather than cleared, so the spend
        stays in the daily-cap sum; stamp_post_terminal_usage later replaces it
        with the SDK-reported truth.
```

Then change the estimate-clearing branch:

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
Expected: all pass (existing outcome tests unaffected); ruff clean.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/turns/outcomes.py \
  tests/proposal_assist/test_turn_outcomes.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(turns): record_terminal_outcome(usage_unknown) retains the estimate

Terminalizing a RUNNING turn at 0/0 would clear the submit-time pre-count while
the real usage is later rolled back by the terminal-row guard — invisible spend
against the daily cap. usage_unknown=True retains it; default is unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `stamp_post_terminal_usage` — idempotent late-usage reconciliation

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
  Returns `True` if this call performed the stamp, `False` if already reconciled (idempotent). Never changes `TurnJob.status`. Emits exactly one `cloud_turn_usage_reconciled` `WorkspaceEvent` on the first stamp. Caller commits.

**Why:** after `usage_unknown=True` the user row still holds the estimate. When the stream later reports real usage, we must write the truth **and clear the estimate** (mirroring `_finalize_success`, `processor.py:409-436`) or estimate + real double-count. Idempotency is keyed on the reconciliation event, because token columns alone cannot distinguish "estimate" from "already-stamped truth".

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_turn_outcomes.py`:

```python
def test_post_terminal_usage_stamps_truth_clears_estimate_and_is_idempotent(pg_session_factory):
    from call_manager.proposal_assist.turns.outcomes import stamp_post_terminal_usage

    with pg_session_factory() as s:
        ws = make_workspace(s)
        user = make_user(s)
        user_turn = make_chat_turn(s, workspace_id=ws.id, role="user")
        user_turn.tokens_in = 1234                    # retained estimate
        assistant = make_chat_turn(s, workspace_id=ws.id, role="assistant")
        job = TurnJob(
            workspace_id=ws.id, status="cancelled",   # already terminal
            user_turn_id=user_turn.id, submitted_by_user_id=user.id,
        )
        s.add(job)
        s.commit()
        job_id, ut_id, a_id, ws_id = job.id, user_turn.id, assistant.id, ws.id

    with pg_session_factory() as s:
        did = stamp_post_terminal_usage(
            s, turn_job_id=job_id, workspace_id=ws_id, user_turn_id=ut_id,
            last_assistant_id=a_id, tokens_in=500, tokens_out=42, model="claude-opus-4-7",
        )
        s.commit()
        assert did is True

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status == "cancelled"      # status untouched
        assert s.get(ChatTurn, a_id).tokens_in == 500            # truth on assistant row
        assert s.get(ChatTurn, a_id).tokens_out == 42
        assert s.get(ChatTurn, ut_id).tokens_in is None          # estimate CLEARED (no double count)
        events = s.scalars(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "cloud_turn_usage_reconciled",
            )
        ).all()
        assert len(events) == 1

    # Second call is a no-op: no re-stamp, no second event.
    with pg_session_factory() as s:
        again = stamp_post_terminal_usage(
            s, turn_job_id=job_id, workspace_id=ws_id, user_turn_id=ut_id,
            last_assistant_id=a_id, tokens_in=999, tokens_out=999, model="x",
        )
        s.commit()
        assert again is False

    with pg_session_factory() as s:
        assert s.get(ChatTurn, a_id).tokens_in == 500            # unchanged
        n = s.scalar(
            select(func.count()).select_from(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "cloud_turn_usage_reconciled",
            )
        )
        assert n == 1


def test_post_terminal_usage_without_assistant_row_stamps_user_row(pg_session_factory):
    from call_manager.proposal_assist.turns.outcomes import stamp_post_terminal_usage

    with pg_session_factory() as s:
        ws = make_workspace(s)
        user = make_user(s)
        user_turn = make_chat_turn(s, workspace_id=ws.id, role="user")
        user_turn.tokens_in = 77
        job = TurnJob(
            workspace_id=ws.id, status="cancelled",
            user_turn_id=user_turn.id, submitted_by_user_id=user.id,
        )
        s.add(job)
        s.commit()
        job_id, ut_id, ws_id = job.id, user_turn.id, ws.id

    with pg_session_factory() as s:
        stamp_post_terminal_usage(
            s, turn_job_id=job_id, workspace_id=ws_id, user_turn_id=ut_id,
            last_assistant_id=None, tokens_in=300, tokens_out=10, model=None,
        )
        s.commit()

    with pg_session_factory() as s:
        ut = s.get(ChatTurn, ut_id)
        assert ut.tokens_in == 300 and ut.tokens_out == 10   # truth replaces the estimate
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_turn_outcomes.py -p no:cacheprovider -q -k post_terminal
```
Expected: FAIL — `ImportError: cannot import name 'stamp_post_terminal_usage'`.

- [ ] **Step 3: Implement**

Append to `outcomes.py` (the module already imports `select`, `ChatTurn`, `TurnJob`, `WorkspaceEvent`, `uuid`):

```python
USAGE_RECONCILED_KIND = "cloud_turn_usage_reconciled"


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
    """Record a terminalized turn's REAL token usage after the fact.

    Used when a turn was terminalized while RUNNING (workspace deletion, via
    record_terminal_outcome(usage_unknown=True)) and its stream later reported
    actual usage. Writes the SDK truth and CLEARS the retained submit-time
    estimate — mirroring _finalize_success — so the daily cap counts the spend
    exactly once. NEVER changes TurnJob.status (the row is already terminal) and
    emits no second *outcome* event; instead it emits ONE non-outcome
    ``cloud_turn_usage_reconciled`` audit event so the trail stays honest.

    Idempotent: keyed on that event's existence, because the token columns alone
    cannot distinguish 'submit-time estimate' from 'already-stamped truth'.
    Returns True if this call performed the stamp. Caller commits.
    """
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
        # the cap sum doesn't count the input twice (mirrors _finalize_success).
        if user_turn is not None:
            user_turn.tokens_in = None
    elif user_turn is not None:
        # No assistant row was emitted — persist the totals on the user row,
        # replacing (not adding to) the submit-time estimate.
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

Note: `WorkspaceEvent.payload` is JSONB, so `payload["turn_job_id"].astext` is the correct comparison operator.

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_turn_outcomes.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/proposal_assist/turns/outcomes.py
```
Expected: all pass; ruff clean. If `func`/`WorkspaceEvent`/`select` are missing from the test module's imports, add them (`from sqlalchemy import func, select`, `from call_manager.db.models import WorkspaceEvent`).

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/turns/outcomes.py \
  tests/proposal_assist/test_turn_outcomes.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(turns): stamp_post_terminal_usage — idempotent late-usage reconciliation

Writes a terminalized turn's real usage and CLEARS the retained estimate (else
estimate + truth double-count), never touching TurnJob.status. Emits exactly one
non-outcome cloud_turn_usage_reconciled event, which is also the idempotency key.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: delete transaction terminalizes RUNNING turn rows

**Files:**
- Modify: `src/call_manager/workspaces.py:82-133` (`soft_delete_workspace`)
- Test: `tests/test_workspaces_service.py`

**Interfaces:**
- Consumes: `record_terminal_outcome(..., usage_unknown=True)` (Task 1).
- Produces: after `soft_delete_workspace`, no `turn_jobs` row for the workspace is left in `running`.

**Why:** `_finalize_success` is a conditional `UPDATE ... WHERE status='running'` (`processor.py:446-452`), so terminalizing in the delete transaction makes both racing paths contend on the same row — the finalize becomes a no-op, durably, with no in-process recheck. It also neutralizes the defensive-`TurnDoneEvent` success path (`claude_adapter.py:497` → `:338`) and frees the partial unique index on running.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_workspaces_service.py`:

```python
def test_soft_delete_terminalizes_running_turn_and_finalize_is_noop(pg_session_factory, tmp_path):
    """A running turn is terminalized in the delete txn; the in-flight processor's
    later success-finalize then contends on the same row and does nothing."""
    from call_manager.db.models import ChatTurn, TurnJob
    from call_manager.proposal_assist.turns.processor import _finalize_success
    from call_manager.workspaces import soft_delete_workspace
    from tests.proposal_assist.conftest import make_chat_turn, make_user, make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s)
        user = make_user(s)
        user_turn = make_chat_turn(s, workspace_id=ws.id, role="user")
        user_turn.tokens_in = 1234                      # submit-time estimate
        job = TurnJob(
            workspace_id=ws.id, status="running",
            user_turn_id=user_turn.id, submitted_by_user_id=user.id,
        )
        s.add(job)
        s.commit()
        ws_id, job_id, ut_id = ws.id, job.id, user_turn.id

    with pg_session_factory() as s:
        soft_delete_workspace(s, workspace_id=ws_id, workspaces_dir=tmp_path)

    with pg_session_factory() as s:
        row = s.get(TurnJob, job_id)
        assert row.status != "running"                  # terminalized in the delete txn
        assert s.get(ChatTurn, ut_id).tokens_in == 1234  # estimate retained (usage_unknown)

    # The in-flight processor now finalizes "successfully" — must be a NO-OP.
    _finalize_success(
        pg_session_factory,
        turn_job_id=job_id,
        workspace_id=ws_id,
        submitted_by_user_id=None,
        user_turn_id=ut_id,
        last_assistant_id=None,
        tokens_in=10,
        tokens_out=5,
        model="m",
        session_id=None,
    )

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status != "completed"   # never resurrected
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/test_workspaces_service.py -p no:cacheprovider -q -k terminalizes
```
Expected: FAIL — the row is still `running` after delete.

- [ ] **Step 3: Implement**

In `workspaces.py`, immediately after the existing queued-turn loop and **before** `session.commit()`, add:

```python
    # Terminalize any RUNNING turn in THIS transaction. The processor's
    # success-finalize is a conditional UPDATE ... WHERE status='running'
    # (processor.py), so once this commits both paths contend on the same row and
    # the finalize becomes a no-op — closing the delete-vs-finalize race durably,
    # with no in-process recheck. usage_unknown=True keeps the submit-time
    # estimate: the turn HAS consumed input we cannot measure yet, and
    # stamp_post_terminal_usage replaces it if the stream reports real usage.
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

(`select`, `TurnJob` and `record_terminal_outcome` are already imported in this module.)

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/test_workspaces_service.py tests/proposal_assist/test_turn_outcomes.py \
  -p no:cacheprovider -q && uv run ruff check src/call_manager/workspaces.py
```
Expected: all pass (the existing cascade/scrub test is unaffected); ruff clean.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/workspaces.py tests/test_workspaces_service.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(workspaces): delete txn terminalizes RUNNING turn rows

Both the delete and the processor's success-finalize now contend on the SAME
row (conditional UPDATE ... WHERE status='running'), so the finalize becomes a
no-op — the delete-vs-finalize TOCTOU closes durably, with no in-process
recheck, and the defensive TurnDoneEvent path can't resurrect a deleted turn.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `WorkspaceRetired` + pre-claim soft-delete check in `controller.launch`

**Files:**
- Modify: `src/call_manager/proposal_assist/drafting/contract.py` (add exception)
- Modify: `src/call_manager/proposal_assist/drafting/controller.py:105` (check above `claim_launch_or_wait`)
- Test: `tests/proposal_assist/test_drafting_controller.py`

**Interfaces:**
- Produces: `class WorkspaceRetired(RuntimeError)` in `drafting/contract.py`; `controller.launch` raises it when the workspace is soft-deleted.

**Why + honest scope:** `process_turn_once` launches three times (`processor.py:158`, `:276`, and `send_turn`→`launch` at `controller.py:213`), all get-or-create. The check must sit **above `claim_launch_or_wait`** because the cached/pending path returns early at `controller.py:109` and never reaches the existing `work_session` block at `:138`. **This narrows the relaunch window; it does not close it** — a deletion committing after the check can still see a cached handle returned. Do not write a test asserting otherwise.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_drafting_controller.py` (match the module's existing controller-construction helper; if it builds a controller via a local `_controller(...)` helper, reuse it):

```python
@pytest.mark.asyncio
async def test_launch_refuses_soft_deleted_workspace(pg_session_factory, tmp_path):
    """No relaunch for a deleted workspace — checked ABOVE claim_launch_or_wait."""
    from datetime import UTC, datetime

    from call_manager.db.models import Workspace
    from call_manager.proposal_assist.drafting.contract import WorkspaceRetired
    from tests.proposal_assist.conftest import make_workspace

    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        session_factory=pg_session_factory,
        registry=registry,
        workspaces_dir=tmp_path,
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
    assert adapter.launches == []          # no subprocess was started


@pytest.mark.asyncio
async def test_launch_refuses_deleted_workspace_even_when_handle_is_cached(
    pg_session_factory, tmp_path
):
    """The cached/pending path returns early at controller.py:109, so the check
    MUST precede claim_launch_or_wait — otherwise a warm workspace keeps its
    handle and send_turn's internal launch sails through."""
    from datetime import UTC, datetime

    from call_manager.db.models import Workspace
    from call_manager.proposal_assist.drafting.contract import WorkspaceRetired
    from tests.proposal_assist.conftest import make_workspace

    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        session_factory=pg_session_factory,
        registry=registry,
        workspaces_dir=tmp_path,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    await controller.launch(ws_id)            # warm: handle now cached
    assert len(adapter.launches) == 1

    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    with pytest.raises(WorkspaceRetired):
        await controller.launch(ws_id)        # cached path must STILL refuse
    assert len(adapter.launches) == 1         # no second subprocess
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_drafting_controller.py -p no:cacheprovider -q -k "soft_deleted or cached"
```
Expected: FAIL — `ImportError: cannot import name 'WorkspaceRetired'`.

- [ ] **Step 3a: Add the exception**

In `src/call_manager/proposal_assist/drafting/contract.py`, after `DraftingAtCapacity`:

```python
class WorkspaceRetired(RuntimeError):
    """The workspace is soft-deleted (or has been retired in-process), so no
    subprocess may be launched or kept for it.

    Raised by WorkspaceController.launch. Callers on the turn path finalize the
    turn instead of sending. NOTE: this is a NARROWING, not a race-free gate — a
    deletion committing after the check can still see a cached handle returned;
    the reconciliation sweep is what eventually reaps such a subprocess."""

    def __init__(self, workspace_id: uuid.UUID) -> None:
        super().__init__(f"workspace {workspace_id} is retired")
        self.workspace_id = workspace_id
```

- [ ] **Step 3b: Add the check at the top of `launch`**

In `controller.py`, insert **immediately before** `am_launcher, pending = await self._registry.claim_launch_or_wait(workspace_id)` (`:105`):

```python
        # Durable no-relaunch floor: refuse a soft-deleted workspace BEFORE
        # claim_launch_or_wait, because the cached/pending path returns early
        # below and never reaches the work_session block. Restart-safe (the DB
        # tombstone survives a process restart) and needs no route wiring.
        # NARROWING ONLY: a deletion committing after this check can still see a
        # cached handle returned — the reconciliation sweep is the backstop.
        if self._workspace_is_deleted(workspace_id):
            raise WorkspaceRetired(workspace_id)
```

Add the helper next to `_workspace_has_queued_turn` (mirrors its session + error contract):

```python
    def _workspace_is_deleted(self, workspace_id: uuid.UUID) -> bool:
        """True if the workspace row is missing or soft-deleted. Fresh short-lived
        session (per-unit-of-work). Best-effort: on a DB error return False so a
        transient blip cannot block legitimate drafting — the reconciliation sweep
        still reaps a subprocess for a workspace that really is deleted."""
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

Import `WorkspaceRetired` in `controller.py` from `.contract` alongside the existing imports.

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_drafting_controller.py tests/proposal_assist/test_drafting_registry.py \
  -p no:cacheprovider -q && uv run ruff check \
  src/call_manager/proposal_assist/drafting/contract.py \
  src/call_manager/proposal_assist/drafting/controller.py
```
Expected: all pass; ruff clean.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/drafting/contract.py \
  src/call_manager/proposal_assist/drafting/controller.py \
  tests/proposal_assist/test_drafting_controller.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(drafting): refuse launch for a soft-deleted workspace (WorkspaceRetired)

Checked ABOVE claim_launch_or_wait — the cached/pending path returns early and
never reaches the work_session block, so a warm workspace would otherwise keep
serving send_turn's internal launch. Durable + restart-safe; documented as a
NARROWING of the relaunch window, not a race-free gate.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: processor — guarded region over post-claim launch + late-usage routing

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/processor.py:276-295` (guarded region), and the finalize dispatch at `:338-380`
- Test: `tests/proposal_assist/test_turns_processor.py`

**Interfaces:**
- Consumes: `WorkspaceRetired` (Task 4), `stamp_post_terminal_usage` (Task 2).
- Produces: a `WorkspaceRetired` raised by the post-claim `launch` or by `send_turn` no longer escapes; the row is never left `running` by this path, and any real usage reported by the stream is stamped via `stamp_post_terminal_usage`.

**Why:** the post-claim `await controller.launch(workspace_id)` (`:276`) and `set_turn_provenance` sit **outside** the finalization `try` (which opens at `:295`), so `WorkspaceRetired` escapes and leaves the row `running` until the reaper.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_turns_processor.py` (reuse the module's existing controller/adapter/job fixtures — grep for how other tests build `process_turn_once` inputs):

```python
@pytest.mark.asyncio
async def test_retired_workspace_before_second_launch_finalizes_without_sending(
    pg_session_factory, tmp_path, monkeypatch
):
    """Deletion between the claim and the post-claim launch must NOT escape:
    the turn is finalized (never left 'running') and NOTHING is sent."""
    from call_manager.db.models import TurnJob
    from call_manager.proposal_assist.drafting.contract import WorkspaceRetired

    # ... build workspace + queued turn_job + controller/FakeAgentAdapter exactly as
    # the neighbouring processor tests do ...

    calls = {"n": 0}
    real_launch = controller.launch

    async def _launch(ws_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return await real_launch(ws_id)        # pre-claim launch succeeds
        raise WorkspaceRetired(ws_id)              # workspace deleted before launch #2

    monkeypatch.setattr(controller, "launch", _launch)

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
        workspaces_dir=tmp_path,
    )

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status != "running"   # finalized, not abandoned
    assert adapter.turns == []                              # no send_turn reached the SDK
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_turns_processor.py -p no:cacheprovider -q -k retired_workspace
```
Expected: FAIL — `WorkspaceRetired` propagates out of `process_turn_once` and the row stays `running`.

- [ ] **Step 3: Implement**

In `processor.py`, move the post-claim launch + provenance **inside** the guarded region. Replace the block that currently runs `await controller.launch(workspace_id)` … `set_turn_provenance(...)` followed by `try:` with a single `try:` that wraps both:

```python
    sdk_error: SDKTurnError | None = None
    other_error: BaseException | None = None
    retired: WorkspaceRetired | None = None

    try:
        # COLD-LAUNCH ORDER (unchanged): ensure the launch, THEN stamp provenance,
        # THEN send. Now INSIDE the guarded region: a WorkspaceRetired raised here
        # (workspace deleted between claim and launch) must finalize the turn, not
        # escape and leave the row 'running' until the reaper.
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
            ...   # UNCHANGED event handling
    except WorkspaceRetired as e:
        retired = e
    except SDKTurnError as e:
        sdk_error = e
    except BaseException as e:  # noqa: BLE001
        other_error = e
```

(The `last_assistant_id` / `tokens_*` / `model` / `session_id` locals must be initialised **before** this `try`, as they are today.)

Then add the retired branch to the finalize dispatch, **before** the success branch:

```python
    if retired is not None:
        # The delete transaction already terminalized this row (workspaces.py), so
        # a normal finalize would be a no-op. Record any usage the stream did
        # report so the spend still counts against the daily cap, then stop.
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
        _finalize_failed(
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
        ...   # UNCHANGED success branch
```

Add the imports at the top of `processor.py`:
```python
from call_manager.proposal_assist.drafting.contract import WorkspaceRetired
from call_manager.proposal_assist.turns.outcomes import stamp_post_terminal_usage
```
(`SDKTurnError` etc. are already imported from `contract`; extend that import list rather than adding a duplicate line.)

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_turns_processor.py tests/proposal_assist/test_turns_end_to_end.py \
  tests/proposal_assist/test_turns_drain.py tests/proposal_assist/test_turn_outcomes.py \
  -p no:cacheprovider -q && uv run ruff check src/call_manager/proposal_assist/turns/processor.py
```
Expected: all pass (existing processor behavior unchanged for non-retired paths); ruff clean.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/turns/processor.py \
  tests/proposal_assist/test_turns_processor.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(turns): guard the post-claim launch; stamp late usage on retirement

The post-claim controller.launch + set_turn_provenance sat OUTSIDE the
finalization try, so a WorkspaceRetired escaped and left the row 'running' until
the reaper. Both are now inside it, and the retired branch routes any reported
usage through stamp_post_terminal_usage before finalizing.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: `LiveClientRegistry` — per-generation bookkeeping

**Files:**
- Create: `src/call_manager/proposal_assist/drafting/live_clients.py`
- Test: `tests/proposal_assist/test_live_clients.py` (create)

**Interfaces:**
- Produces:
  ```python
  @dataclass
  class LiveClientRecord:
      launch_id: uuid.UUID
      workspace_id: uuid.UUID
      client: Any
      state: str = "live"            # "live" | "retiring"
      teardown: asyncio.Task | None = None

  class LiveClientRegistry:
      def register(self, workspace_id: uuid.UUID, client: Any) -> uuid.UUID   # -> launch_id
      def mark_retiring(self, launch_id: uuid.UUID) -> LiveClientRecord | None
      def confirm_dead(self, launch_id: uuid.UUID) -> None
      def get(self, launch_id: uuid.UUID) -> LiveClientRecord | None
      def find_by_workspace(self, workspace_id: uuid.UUID) -> list[LiveClientRecord]
      def records(self) -> list[LiveClientRecord]
      def retiring_count(self) -> int
      def unconfirmed_count(self) -> int
  ```

**Why keyed by generation:** after an unconfirmed teardown the registry releases its advisory lock (`registry.py:164`), so a fresh launch for the same workspace would overwrite a workspace-keyed record — or a late teardown would clear the *new* client's record. Records must be cleared **by identity**.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_live_clients.py`:

```python
"""Per-generation live-client bookkeeping (Slice A). Pure in-memory logic — no
SDK, no event loop required except where tasks are attached."""
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
    # The orphan/replacement case: a stale teardown must NOT clear the new client.
    reg = LiveClientRegistry()
    ws = uuid.uuid4()
    orphan = reg.register(ws, client="orphan")
    reg.mark_retiring(orphan)
    fresh = reg.register(ws, client="fresh")

    reg.confirm_dead(orphan)                     # late completion of the OLD teardown
    assert reg.get(orphan) is None
    assert reg.get(fresh) is not None            # replacement untouched
    assert reg.get(fresh).state == "live"


def test_retiring_count_tracks_unconfirmed_orphans_only():
    reg = LiveClientRegistry()
    ws = uuid.uuid4()
    live = reg.register(ws, client="live")
    orphan = reg.register(uuid.uuid4(), client="orphan")
    assert reg.retiring_count() == 0

    reg.mark_retiring(orphan)
    assert reg.retiring_count() == 1             # counts toward the ceiling
    assert reg.unconfirmed_count() == 2

    reg.confirm_dead(orphan)
    assert reg.retiring_count() == 0
    assert reg.unconfirmed_count() == 1
    assert reg.get(live) is not None


def test_mark_retiring_is_idempotent_and_tolerates_unknown_ids():
    reg = LiveClientRegistry()
    lid = reg.register(uuid.uuid4(), client="c")
    assert reg.mark_retiring(lid).state == "retiring"
    assert reg.mark_retiring(lid).state == "retiring"    # no flip-flop, no raise
    assert reg.mark_retiring(uuid.uuid4()) is None       # unknown id -> None
    reg.confirm_dead(uuid.uuid4())                       # unknown id -> no raise
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_live_clients.py -p no:cacheprovider -q
```
Expected: FAIL — `ModuleNotFoundError: ...drafting.live_clients`.

- [ ] **Step 3: Implement**

Create `src/call_manager/proposal_assist/drafting/live_clients.py`:

```python
"""Per-generation inventory of live SDK clients (Slice A).

The durable ownership anchor for subprocess teardown. Every path that discards a
handle — capacity eviction (registry.py), idle sweep, retirement teardown, and
the launcher's own cleanup (controller.py, which suppresses exceptions) — drops
its reference BEFORE termination is confirmed, and the adapter pops
handle.state["client"] before disconnect. Without this inventory a failed
teardown leaves nothing to retry and nothing to count.

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
        """Flag a generation as being torn down. Idempotent; returns the record,
        or None if the id is unknown (already confirmed dead)."""
        record = self._records.get(launch_id)
        if record is None:
            return None
        record.state = RETIRING
        return record

    def confirm_dead(self, launch_id: uuid.UUID) -> None:
        """Drop a generation — ONLY after its process is confirmed gone.
        Clearing is by identity, so a late teardown of an orphan can never
        remove the replacement generation for the same workspace."""
        self._records.pop(launch_id, None)

    def get(self, launch_id: uuid.UUID) -> LiveClientRecord | None:
        return self._records.get(launch_id)

    def find_by_workspace(self, workspace_id: uuid.UUID) -> list[LiveClientRecord]:
        return [r for r in self._records.values() if r.workspace_id == workspace_id]

    def records(self) -> list[LiveClientRecord]:
        """Snapshot — safe to iterate while callers mutate the inventory."""
        return list(self._records.values())

    def retiring_count(self) -> int:
        """Generations whose teardown was attempted but not confirmed. These are
        the ones the registry's _entries no longer knows about, so capacity
        accounting must add them explicitly."""
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

The durable ownership anchor: every existing path drops its handle before
termination is confirmed, so a failed teardown left nothing to retry or count.
Keyed by launch generation (never workspace) and cleared by identity, so an
orphan and its replacement coexist safely.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: adapter — register before `connect()`, hard-bounded teardown, confirm death

**Files:**
- Modify: `src/call_manager/proposal_assist/drafting/claude_adapter.py:185-205` (launch), `:525-551` (terminate + force-kill)
- Modify: `src/call_manager/proposal_assist/drafting/fake_adapter.py` (mirror the surface)
- Test: `tests/proposal_assist/test_claude_adapter_teardown.py` (create)

**Interfaces:**
- Consumes: `LiveClientRegistry` (Task 6).
- Produces on `ClaudeAdapter`: `self._live: LiveClientRegistry`; `retiring_count() -> int`; `async reconcile_live_clients(is_deleted: Callable[[uuid.UUID], bool]) -> int`. `AgentHandle.state["launch_id"]` carries the generation. Same three names on `FakeAgentAdapter`.

**Why:** `asyncio.wait_for` awaits cancellation completion, so a cancellation-resistant `disconnect()` can overrun indefinitely and force-kill never runs. Run disconnect as a **tracked task** and fire force-kill from an **independent timer**, then **reap** before clearing the record.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_claude_adapter_teardown.py`:

```python
"""Bounded teardown + generation lifecycle for ClaudeAdapter (Slice A)."""
import asyncio
import uuid

import pytest

from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAdapter
from call_manager.proposal_assist.drafting.contract import AgentHandle


class _HangingClient:
    """disconnect() ignores cancellation — the exact shape asyncio.wait_for
    cannot bound."""

    def __init__(self):
        self.killed = False
        self.reaped = False
        self._transport = self
        self._process = self

    async def disconnect(self):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(3600)     # resists cancellation

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
    # ClaudeAdapter takes workspaces_dir as a str (keyword-only, with model).
    return ClaudeAdapter(workspaces_dir=str(tmp_path), model="m", **kw)


@pytest.mark.asyncio
async def test_clean_disconnect_confirms_death_and_clears_the_record(tmp_path):
    adapter = _adapter(tmp_path)
    client = _CleanClient()
    ws = uuid.uuid4()
    launch_id = adapter._live.register(ws, client)
    handle = AgentHandle(workspace_id=ws, state={"client": client, "launch_id": launch_id})

    await adapter.terminate(handle)

    assert client.disconnected is True
    assert client.killed is False
    assert adapter._live.get(launch_id) is None      # confirmed dead -> cleared
    assert adapter.retiring_count() == 0


@pytest.mark.asyncio
async def test_hanging_disconnect_is_force_killed_at_the_deadline_and_reaped(tmp_path):
    adapter = _adapter(tmp_path, disconnect_deadline_seconds=0.05, reap_deadline_seconds=0.5)
    client = _HangingClient()
    ws = uuid.uuid4()
    launch_id = adapter._live.register(ws, client)
    handle = AgentHandle(workspace_id=ws, state={"client": client, "launch_id": launch_id})

    await asyncio.wait_for(adapter.terminate(handle), timeout=5)   # must NOT hang

    assert client.killed is True                 # timer fired despite cancellation-resistance
    assert client.reaped is True                 # death confirmed by reaping
    assert adapter._live.get(launch_id) is None


@pytest.mark.asyncio
async def test_unreapable_client_stays_retiring_for_the_sweep(tmp_path):
    class _Unkillable(_HangingClient):
        def kill(self):
            raise RuntimeError("no such process")

    adapter = _adapter(tmp_path, disconnect_deadline_seconds=0.05, reap_deadline_seconds=0.2)
    client = _Unkillable()
    ws = uuid.uuid4()
    launch_id = adapter._live.register(ws, client)
    handle = AgentHandle(workspace_id=ws, state={"client": client, "launch_id": launch_id})

    await asyncio.wait_for(adapter.terminate(handle), timeout=5)

    rec = adapter._live.get(launch_id)
    assert rec is not None and rec.state == "retiring"   # retained for retry
    assert adapter.retiring_count() == 1                 # and counted against capacity


@pytest.mark.asyncio
async def test_reconcile_retries_every_retiring_record_even_on_live_workspaces(tmp_path):
    """Capacity eviction / idle teardown orphans belong to LIVE workspaces — they
    must be retried too, not only deleted ones."""
    adapter = _adapter(tmp_path, disconnect_deadline_seconds=0.05, reap_deadline_seconds=0.5)
    client = _HangingClient()
    ws = uuid.uuid4()
    launch_id = adapter._live.register(ws, client)
    adapter._live.mark_retiring(launch_id)               # orphaned by an earlier failure

    n = await adapter.reconcile_live_clients(is_deleted=lambda _ws: False)  # workspace is LIVE

    assert n >= 1
    await asyncio.sleep(0.3)                             # let the teardown task finish
    assert client.killed is True
    assert adapter._live.get(launch_id) is None


@pytest.mark.asyncio
async def test_reconcile_retires_live_records_of_deleted_workspaces(tmp_path):
    adapter = _adapter(tmp_path, disconnect_deadline_seconds=0.05, reap_deadline_seconds=0.5)
    client = _CleanClient()
    ws = uuid.uuid4()
    launch_id = adapter._live.register(ws, client)       # still "live"

    await adapter.reconcile_live_clients(is_deleted=lambda w: w == ws)
    await asyncio.sleep(0.2)

    assert client.disconnected is True
    assert adapter._live.get(launch_id) is None


@pytest.mark.asyncio
async def test_reconcile_does_not_spawn_duplicate_teardowns(tmp_path):
    adapter = _adapter(tmp_path, disconnect_deadline_seconds=5, reap_deadline_seconds=5)
    client = _HangingClient()
    ws = uuid.uuid4()
    launch_id = adapter._live.register(ws, client)
    adapter._live.mark_retiring(launch_id)

    await adapter.reconcile_live_clients(is_deleted=lambda _w: False)
    first = adapter._live.get(launch_id).teardown
    await adapter.reconcile_live_clients(is_deleted=lambda _w: False)
    second = adapter._live.get(launch_id).teardown

    assert first is second                      # one tracked task per record, reused
    first.cancel()
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_claude_adapter_teardown.py -p no:cacheprovider -q
```
Expected: FAIL — `ClaudeAdapter` has no `_live` / `retiring_count` / `reconcile_live_clients`, and `__init__` rejects the deadline kwargs.

- [ ] **Step 3a: Adapter constructor + launch registration**

In `claude_adapter.py`, add to `__init__` (keep every existing parameter):

```python
        disconnect_deadline_seconds: float = 3.0,
        reap_deadline_seconds: float = 2.0,
```
and in the body:
```python
        self._disconnect_deadline_seconds = disconnect_deadline_seconds
        self._reap_deadline_seconds = reap_deadline_seconds
        self._live = LiveClientRegistry()
```
with `from call_manager.proposal_assist.drafting.live_clients import LiveClientRegistry` at the top.

Change `launch` (`:199-205`) to register **before** connecting:

```python
        client = ClaudeSDKClient(options=options)
        # Register BEFORE connect(): a connect-time failure otherwise leaves a
        # half-built subprocess nobody owns. The record is what the reconciliation
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

Replace the body of `terminate` (from `self._tool_context_sink.pop(...)` through the existing disconnect/force-kill block) with:

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
        Reused across retirement, capacity eviction, idle sweep and reconciliation,
        so concurrent callers never spawn a second kill for the same generation."""
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

        asyncio.wait_for waits for CANCELLATION to complete, so a
        cancellation-resistant disconnect() would never reach force-kill. Run it
        as a tracked task and fire the kill from an INDEPENDENT timer instead.
        Returns True only when death is confirmed (clean disconnect, or kill with
        a successful reap)."""
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
            task.cancel()   # best-effort; we do NOT await it (it may resist)
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
        workspace may well be LIVE, e.g. capacity eviction and idle-sweep orphans —
        and (b) every generation of a soft-deleted workspace. Returns the number
        acted on. Best-effort by contract: this guarantees a remediation ATTEMPT
        each tick, not that the process died."""
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

Change `_force_kill_subprocess` to **return `bool`** — `True` only when `kill()` was actually invoked; `False` when no process is reachable or `kill()` raised (both existing log branches keep their messages, and each gains `return False`; the success path ends with `return True`). Add `from call_manager.proposal_assist.drafting.live_clients import RETIRING` to the imports.

- [ ] **Step 3c: Mirror the surface on `FakeAgentAdapter`**

In `fake_adapter.py`, add to `__init__`: `self._live = LiveClientRegistry()`, and add:

```python
    def retiring_count(self) -> int:
        return self._live.retiring_count()

    async def reconcile_live_clients(self, is_deleted) -> int:
        return 0        # the fake owns no subprocesses
```
so registry/controller code can call the same names in tests without `getattr` gymnastics.

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_claude_adapter_teardown.py tests/proposal_assist/test_live_clients.py \
  tests/proposal_assist/test_drafting_registry.py tests/proposal_assist/test_drafting_contract.py \
  -p no:cacheprovider -q && uv run ruff check \
  src/call_manager/proposal_assist/drafting/claude_adapter.py \
  src/call_manager/proposal_assist/drafting/fake_adapter.py
```
Expected: all pass; ruff clean.

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
would wait for cancellation to complete and never force-kill. Unconfirmed
generations are retained for the reconciliation sweep.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: capacity ceiling counts unconfirmed generations

**Files:**
- Modify: `src/call_manager/proposal_assist/drafting/registry.py:127-142`
- Test: `tests/proposal_assist/test_drafting_registry_capacity.py`

**Interfaces:**
- Consumes: `adapter.retiring_count()` (Task 7).
- Produces: `in_use = len(self._entries) + len(self._pending_launches) + adapter.retiring_count()`.

**Why:** `DRAFTING_MAX_LIVE_SUBPROCESSES` (`config.py:177`) exists to bound checked-out pool connections, but the count ignored orphaned generations — an unconfirmed-dead process could push real usage past the ceiling. Counting them means a replacement launch is refused with the existing `DraftingAtCapacity` (retriable 429) instead of silently exceeding the cap. `retiring` records are exactly the ones no longer in `_entries`, so there is no double-count.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_drafting_registry_capacity.py`:

```python
@pytest.mark.asyncio
async def test_unconfirmed_generation_counts_toward_the_ceiling():
    """An orphan whose death is unconfirmed still pins a real subprocess (and its
    pooled DB connection), so it must occupy a slot rather than being invisible."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600, max_live=1)

    ws_orphan = uuid.uuid4()
    ws_new = uuid.uuid4()

    # Simulate a teardown that never confirmed: a retiring generation with NO
    # corresponding _entries row (exactly what capacity eviction leaves behind).
    launch_id = adapter._live.register(ws_orphan, client=object())
    adapter._live.mark_retiring(launch_id)
    assert adapter.retiring_count() == 1
    assert registry._entries == {}

    with pytest.raises(DraftingAtCapacity):
        await registry.claim_launch_or_wait(ws_new)

    # Once death is confirmed the slot frees up again.
    adapter._live.confirm_dead(launch_id)
    am_launcher, _ = await registry.claim_launch_or_wait(ws_new)
    assert am_launcher is True
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_drafting_registry_capacity.py -p no:cacheprovider -q -k unconfirmed
```
Expected: FAIL — no `DraftingAtCapacity` raised (the orphan is invisible to the count).

- [ ] **Step 3: Implement**

In `registry.py`, replace the `in_use` computation:

```python
            if self._max_live is not None:
                # Count generations whose death is NOT confirmed: capacity
                # eviction / idle sweep / failed teardown pop the entry but the
                # OS process (and its pooled DB connection) may still be alive.
                # Ignoring them would let real usage exceed the ceiling the cap
                # exists to enforce. Adapters without an inventory report 0.
                unconfirmed = getattr(self._adapter, "retiring_count", None)
                orphaned = unconfirmed() if unconfirmed is not None else 0
                in_use = len(self._entries) + len(self._pending_launches) + orphaned
```

Extend the `claim_launch_or_wait` docstring's Capacity paragraph with:

```
        Unconfirmed-dead generations (retiring records in the adapter's live-client
        inventory) also occupy slots; when an evicted victim's death is unconfirmed
        the replacement launch is refused with DraftingAtCapacity (retriable 429)
        rather than silently exceeding the ceiling.
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_drafting_registry_capacity.py tests/proposal_assist/test_drafting_registry.py \
  -p no:cacheprovider -q && uv run ruff check src/call_manager/proposal_assist/drafting/registry.py
```
Expected: all pass; ruff clean.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/drafting/registry.py \
  tests/proposal_assist/test_drafting_registry_capacity.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
fix(drafting): count unconfirmed generations against the live ceiling

An orphan whose death was never confirmed still pins a subprocess and its pooled
DB connection, but capacity counted only _entries + _pending_launches — so real
usage could exceed DRAFTING_MAX_LIVE_SUBPROCESSES. Replacement launches now
refuse with the existing retriable DraftingAtCapacity instead.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: reconciliation on the sweep tick

**Files:**
- Modify: `src/call_manager/proposal_assist/drafting/controller.py` (add `reconcile_live_clients`)
- Modify: `src/call_manager/scheduler.py:610-640` (run it alongside the idle sweep)
- Test: `tests/proposal_assist/test_drafting_controller.py`, `tests/proposal_assist/test_drafting_scheduler_tick.py`

**Interfaces:**
- Consumes: `adapter.reconcile_live_clients(is_deleted)` (Task 7), `_workspace_is_deleted` (Task 4).
- Produces: `await controller.reconcile_live_clients() -> int`, invoked once per idle-sweep tick.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_drafting_controller.py`:

```python
@pytest.mark.asyncio
async def test_controller_reconcile_passes_soft_delete_predicate(pg_session_factory, tmp_path):
    """The controller supplies the DB predicate; the adapter does the reaping."""
    from datetime import UTC, datetime

    from call_manager.db.models import Workspace
    from tests.proposal_assist.conftest import make_workspace

    seen: list = []

    class _RecordingAdapter(FakeAgentAdapter):
        async def reconcile_live_clients(self, is_deleted):
            seen.append(is_deleted)
            return 0

    adapter = _RecordingAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        session_factory=pg_session_factory, registry=registry, workspaces_dir=tmp_path
    )
    with pg_session_factory() as s:
        live_ws = make_workspace(s)
        dead_ws = make_workspace(s)
        s.commit()
        live_id, dead_id = live_ws.id, dead_ws.id
    with pg_session_factory() as s:
        s.get(Workspace, dead_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    await controller.reconcile_live_clients()

    assert len(seen) == 1
    predicate = seen[0]
    assert predicate(dead_id) is True
    assert predicate(live_id) is False
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_drafting_controller.py -p no:cacheprovider -q -k reconcile_passes
```
Expected: FAIL — `WorkspaceController` has no attribute `reconcile_live_clients`.

- [ ] **Step 3a: Controller method**

Add to `controller.py` next to `sweep_idle`:

```python
    async def reconcile_live_clients(self) -> int:
        """Retry teardown for every unconfirmed subprocess generation.

        Covers BOTH (a) retiring generations on LIVE workspaces — capacity
        eviction, idle sweep and launcher-cleanup orphans — and (b) any generation
        belonging to a soft-deleted workspace. Guarantees a remediation ATTEMPT
        each tick; it does not guarantee the process died. Never raises: a sweep
        tick must not die on one bad record."""
        reconcile = getattr(self._registry._adapter, "reconcile_live_clients", None)
        if reconcile is None:
            return 0
        try:
            return await reconcile(is_deleted=self._workspace_is_deleted)
        except Exception:
            logger.exception("reconcile_live_clients failed")
            return 0
```

- [ ] **Step 3b: Scheduler wiring**

In `scheduler.py`'s `_drafting_idle_sweep`, replace the two `workspace_controller.sweep_idle()` call sites with a coroutine factory that does both:

```python
        async def _sweep_and_reconcile():
            await workspace_controller.sweep_idle()
            # Same tick: retry any subprocess generation whose death was never
            # confirmed (Slice A). Cheap when there are none.
            await workspace_controller.reconcile_live_clients()

        if app_loop is None:
            try:
                asyncio.run(_sweep_and_reconcile())
            except Exception:
                _logger.exception("drafting.idle_sweep failed")
            return

        fut = asyncio.run_coroutine_threadsafe(_sweep_and_reconcile(), app_loop)
```
(The `fut.result(timeout=300)` block below stays unchanged.)

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_drafting_controller.py tests/proposal_assist/test_drafting_scheduler_tick.py \
  tests/proposal_assist/test_claude_adapter_teardown.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/proposal_assist/drafting/controller.py src/call_manager/scheduler.py
```
Expected: all pass; ruff clean.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/drafting/controller.py src/call_manager/scheduler.py \
  tests/proposal_assist/test_drafting_controller.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(drafting): reconcile unconfirmed subprocess generations each sweep tick

Retries teardown for retiring generations (including those on LIVE workspaces —
capacity/idle/launcher-cleanup orphans) and for any generation of a soft-deleted
workspace. Guarantees a remediation ATTEMPT per tick, not confirmed death.

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
Expected: all pass. A `connection refused` from testcontainers PG is an infra flake → re-run the flagged file in isolation.

- [ ] **Step 2: Lint (whole repo, as CI runs it)**

```bash
cd /home/parlar_ai/dev/call-monitor && uv run ruff check .
```
Expected: `All checks passed!`

- [ ] **Step 3: Frontend regression (untouched — must stay green)**

```bash
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm exec tsc --noEmit
```
Expected: silent. `git status` shows ZERO frontend changes and no `schema.gen.ts` diff.
**Re-anchor to the repo root afterwards** (`cd /home/parlar_ai/dev/call-monitor`): a lingering `cd frontend` makes later relative `git`/`pytest` paths resolve wrongly.

- [ ] **Step 4: Diff-scope check**

```bash
git -C /home/parlar_ai/dev/call-monitor diff --stat master...workspace-retirement-slice-a
```
Expected: only the files in the File Structure table. **No migration, no frontend, no `schema.gen.ts`.**

- [ ] **Step 5: Verify the honest-scope claims survived**

```bash
git -C /home/parlar_ai/dev/call-monitor diff master...workspace-retirement-slice-a -- '*.py' | grep -nE "no send after|proves.*dead|guarantees.*death|cannot stream" || echo "no overclaiming language introduced"
```
Expected: `no overclaiming language introduced`. Slice A must not assert containment or confirmed death anywhere in code comments, docstrings, or test names.

---

## Self-Review (completed at plan-writing time)

- **Spec coverage:** A1 → Tasks 1–3 (usage_unknown, stamp helper, delete-txn terminalization); A2 → Task 4; A5 → Task 5; A3 → Tasks 6–9 (inventory, adapter wiring, capacity, reconciliation); A4 → Task 7 (`_disconnect_bounded` + `_reap_subprocess`). Slice-A acceptance gates → Task 10. Slice B/C explicitly out of scope.
- **Type consistency:** `record_terminal_outcome(..., usage_unknown: bool = False) -> bool` (Task 1 def, Task 3 call); `stamp_post_terminal_usage(session, *, turn_job_id, workspace_id, user_turn_id, last_assistant_id, tokens_in, tokens_out, model) -> bool` (Task 2 def, Task 5 call); `LiveClientRegistry.register/mark_retiring/confirm_dead/records/retiring_count` (Task 6 def, Task 7 use, Task 8 test); `retiring_count() -> int` (Task 7 def, Task 8 consumer); `reconcile_live_clients(is_deleted)` keyword-consistent between adapter (Task 7), controller (Task 9), and the recording fake in Task 9's test; `WorkspaceRetired(workspace_id)` (Task 4 def, Tasks 5/7 use); `AgentHandle.state["launch_id"]` set in Task 7's `launch` and read in Task 7's `terminate`.
- **Placeholder scan:** every code step carries real code. The one prose-only spot is Task 5's test preamble (`# ... build workspace + queued turn_job ...`), which defers to the neighbouring processor tests' existing fixture construction rather than inventing a divergent one — the assertions and the monkeypatch target are exact.
- **Known nuance:** several tests reference each module's existing helpers (`make_workspace`, `make_user`, `make_chat_turn`, `_install_handle`, the processor module's controller construction). The executor matches them to the file's conftest during execution; monkeypatch targets and assertions are exact.
- **Ordering:** `WorkspaceRetired` is defined in Task 4, before Task 5 catches it; `stamp_post_terminal_usage` in Task 2, before Task 5 calls it; `LiveClientRegistry` in Task 6, before Tasks 7–9 consume it.

## Execution Handoff

Two execution options:

**1. Subagent-Driven (recommended by the skill)** — a fresh subagent per task with review between tasks.

**2. Inline Execution** — execute in this session via `superpowers:executing-plans`, batching with checkpoints.
