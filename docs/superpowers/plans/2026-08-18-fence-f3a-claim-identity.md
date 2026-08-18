# Fence F3a — Claim Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every actor that calls a tool records the workspace generation it claimed at, carries it immutably for the life of that claim, and hands each tool invocation a snapshot that a later re-stamp cannot change.

**Architecture:** Five actors originate tool calls. Each one's claim statement gains `claimed_at_generation` (the column F1 added and nothing has written since). That value is threaded to the actor's `ToolContext`. For the four background actors the context is built per run, so it carries the generation at construction. The interactive context is retained across turns and re-stamped every turn, so its generation is snapshotted **per tool invocation** into a `contextvars.ContextVar` set by the dispatcher — verified to survive `run_in_threadpool`, which is the thread handoff that makes late reads dangerous.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, PostgreSQL 16, pytest (+ testcontainers).

## Global Constraints

1. **F3a fences nothing and converts no effect site.** No tool changes behaviour. No effect gains a generation check. This slice makes the identity *exist and be carried*; F3b builds the forms that consume it.
2. **The generation is captured BY the claim, never by a read after it.** A separate read reopens the delete/restore race the fence exists to close: the transition lands between claim and read, and the actor adopts the new generation as its own belief.
3. **`claimed_at_generation` is compared for EQUALITY and is NULLABLE with no server_default.** Generation 0 is a real generation — `Workspace.lifecycle_generation` has `server_default=text("0")`, so a fresh workspace genuinely sits at 0. A default of 0 would fence every legacy row against a real epoch instead of exempting it.
4. **`active_turn_job_id` is a NEW field. It must not reuse `origin_job_id`.** `registry.py:87` leaves `origin_job_id` `None` for interactive contexts *by design*, and `tools/proposal.py:169` copies it into durable provenance — populating it would silently change stored data and this slice would stop being additive.
5. **The two F2 inertness guards must be replaced, not deleted.** `test_nothing_imports_the_fencing_primitives_yet` and `test_nothing_reads_or_writes_claimed_at_generation_yet` are unconditional; the second goes red the moment Task 2 lands. Task 1 replaces them with scoped obligations that still fail for any file outside F3a's identities.
6. **`queue.py::claim_queued_jobs` (analysis) and `setup.py::claim_next_setup_job` build no `ToolContext` and originate no tool call.** They stay assigned to F4 and F5. Do not give them a generation in this slice.
7. **A per-invocation snapshot must be proven against the REAL dispatcher.** A test that snapshots at helper level passes while production still hands over mutable context.
8. `uv run ruff check .` is the gate — redirect to a log and read `$?`, never pipe into `tail`. Same for pytest. pytest exits **4** on "no tests ran": confirm a nonzero collected count.
9. Fast test runs: `uv run pytest -n 16 --dist loadfile`. Never `-n auto`, never `--dist loadgroup`.

## Baseline

Master is at `28fc684`. Expected baseline: **`3586 passed, 1 skipped`**.

## File Structure

| File | Responsibility |
|---|---|
| `tests/fencing/inventory.py` | **Modify.** `SLICE_STATUS` gains F3a–F3f, F5; row slice reassignment map. |
| `tests/fencing/fence_inventory.json` | **Modify.** Every row's `slice` reassigned per the table in Task 1. |
| `tests/fencing/test_fence_coverage.py` | **Modify.** Replace both unconditional inertness guards with scoped ones. |
| `src/call_manager/proposal_assist/turns/processor.py` | **Modify.** The interactive claim sets and returns the generation. |
| `src/call_manager/proposal_assist/{coherence,evaluator,review,uncited_claims}/jobs.py` | **Modify.** Four background claims capture the generation. |
| `src/call_manager/proposal_assist/{coherence,evaluator,review,uncited_claims}/pass_runner.py` | **Modify.** Drains carry the generation to their runners. |
| `src/call_manager/proposal_assist/{coherence,evaluator,review,uncited_claims}/agent.py` | **Modify.** One-shot `ToolContext`s carry the generation at construction. |
| `src/call_manager/proposal_assist/tools/registry.py` | **Modify.** Two new `ToolContext` fields; `Invocation`; the dispatcher snapshot. |
| `src/call_manager/proposal_assist/drafting/claude_adapter.py` | **Modify.** `set_turn_provenance` stamps generation + `active_turn_job_id`. |
| `tests/proposal_assist/test_f3a_claim_identity.py` | **New.** Claim-capture tests, all five actors. |
| `tests/proposal_assist/test_f3a_invocation_snapshot.py` | **New.** The overlapping-invocation and two-turn runtime tests. |

---

### Task 1: Migrate the F2 gate to sub-slices

**Files:**
- Modify: `tests/fencing/inventory.py`
- Modify: `tests/fencing/fence_inventory.json`
- Modify: `tests/fencing/test_fence_coverage.py`

**Interfaces:**
- Produces: `SLICE_STATUS` keys `F3a`–`F3f`, `F4`, `F5`; `F3A_GENERATION_FILES` (the allowlist later tasks are checked against).

This task must land first. Task 2 writes `claimed_at_generation` into `processor.py`, and the current gate fails on any such file.

- [ ] **Step 1: Reassign every inventory row's slice**

Edit `tests/fencing/fence_inventory.json`. The 20 rows map as follows — verify each identity exists before changing it:

| Rows | Now | Becomes |
|---|---|---|
| `coherence/jobs.py::claim_next_coherence_job` | F4 | **F3a** |
| `evaluator/jobs.py::claim_next_evaluator_job` | F4 | **F3a** |
| `review/jobs.py::claim_next_review_run` | F4 | **F3a** |
| `uncited_claims/jobs.py::claim_next_uncited_claims_job` | F4 | **F3a** |
| `analysis/queue.py::claim_queued_jobs` | F4 | **F4** (unchanged) |
| `phases/setup.py::claim_next_setup_job` | F4 | **F5** |
| all 5 `tool_context` rows | F3 | **F3a** |
| 8 `tool_commit` rows (5 semantic + 2 accounting + `_write_citekey_cache`) | F3 | **F3d** |
| `references.py::_do_fetch_and_ingest` | F3 | **F3e** |

- [ ] **Step 2: Write the failing test for the new slice map**

Add to `tests/fencing/test_fence_coverage.py`:

```python
def test_every_slice_in_the_inventory_is_known():
    """A row naming a slice SLICE_STATUS does not know is unreachable: no gate
    would ever require it to be fenced."""
    unknown = sorted({r["slice"] for r in load_inventory()} - set(SLICE_STATUS))
    assert not unknown, f"inventory names slices with no status: {unknown}"


def test_the_four_tool_originating_claims_are_f3a():
    """Analysis and setup build no ToolContext and originate no tool call, so
    they stay with their architecture slices. Pulling them into F3a would
    demand a generation for actors that never call a tool."""
    by_symbol = {r["symbol"]: r["slice"] for r in load_inventory() if r["kind"] == "claim_fn"}
    assert by_symbol == {
        "claim_next_coherence_job": "F3a",
        "claim_next_evaluator_job": "F3a",
        "claim_next_review_run": "F3a",
        "claim_next_uncited_claims_job": "F3a",
        "claim_queued_jobs": "F4",
        "claim_next_setup_job": "F5",
    }
```

- [ ] **Step 3: Run it and watch it fail**

```bash
uv run pytest tests/fencing/test_fence_coverage.py -q > /tmp/t1.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t1.log
```
Expected: FAIL — `SLICE_STATUS` has no `F3a`.

- [ ] **Step 4: Widen `SLICE_STATUS`**

In `tests/fencing/inventory.py`, replace `SLICE_STATUS = {"F3": "pending", "F4": "pending"}` with:

```python
#: Per-slice activation. `pending` = that slice has not converted its sites, so
#: every row must be `fenced: false`. `complete` = every row must be `true`.
#: F3 is split into sub-slices (see the F3 mechanism spec §7); F4-F9 keep the
#: meanings the architecture spec gives them.
SLICE_STATUS = {
    "F3a": "pending",   # claim identity for every tool-originating actor
    "F3b": "pending",   # the ports
    "F3c": "pending",   # the import gate
    "F3d": "pending",   # convert the tool sites
    "F3e": "pending",   # the ingest pipeline
    "F3f": "pending",   # capability holders, then the handle
    "F4": "pending",    # background job families
    "F5": "pending",    # setup (Phase 0)
}
```

Also update `_SLICE_FOR_KIND` so a regeneration assigns new rows correctly:

```python
_SLICE_FOR_KIND = {KIND_CLAIM_FN: "F4"}   # unchanged: a NEW claim fn defaults to F4
_DEFAULT_SLICE = "F3d"                    # was "F3"
```

- [ ] **Step 5: Run and confirm green**

```bash
uv run pytest tests/fencing/ -q > /tmp/t1.log 2>&1; echo "EXIT=$?"; tail -5 /tmp/t1.log
```
Expected: PASS, 67 collected.

- [ ] **Step 6: Replace the two unconditional inertness guards**

Both go red as soon as Tasks 2–6 land. Replace them — do **not** delete them.

In `tests/fencing/inventory.py`, add:

```python
#: The ONLY files F3a may touch with `claimed_at_generation` or the fencing
#: primitives. Scoped successor to F2's two unconditional inertness guards,
#: which forbade both everywhere. Every path here is an F3a identity or the
#: context/dispatch plumbing that carries its value.
F3A_GENERATION_FILES = frozenset({
    "proposal_assist/turns/processor.py",
    "proposal_assist/coherence/jobs.py",
    "proposal_assist/coherence/pass_runner.py",
    "proposal_assist/coherence/agent.py",
    "proposal_assist/evaluator/jobs.py",
    "proposal_assist/evaluator/pass_runner.py",
    "proposal_assist/evaluator/agent.py",
    "proposal_assist/review/jobs.py",
    "proposal_assist/review/pass_runner.py",
    "proposal_assist/review/agent.py",
    "proposal_assist/uncited_claims/jobs.py",
    "proposal_assist/uncited_claims/pass_runner.py",
    "proposal_assist/uncited_claims/agent.py",
    "proposal_assist/tools/registry.py",
    "proposal_assist/drafting/claude_adapter.py",
    "db/models.py",
})
```

In `tests/fencing/test_fence_coverage.py`, replace `test_nothing_reads_or_writes_claimed_at_generation_yet` with:

```python
def test_only_f3a_identities_touch_claimed_at_generation():
    """Scoped successor to F2's unconditional guard, which forbade the column
    everywhere and therefore goes red the moment F3a's first claim writes it.
    The obligation it carried is real and is kept: a file outside F3a's
    identities reaching for the column is wiring F4-F9 early."""
    offenders = [
        str(p.relative_to(SRC))
        for p in sorted(SRC.rglob("*.py"))
        if "__pycache__" not in p.parts
        and str(p.relative_to(SRC)) not in F3A_GENERATION_FILES
        and "claimed_at_generation" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "claimed_at_generation outside F3a's identities:\n  " + "\n  ".join(offenders)
    )
```

Replace `test_nothing_imports_the_fencing_primitives_yet` the same way. Keep its
existing body verbatim — including the two-spelling detection, which catches both
`from …proposal_assist.fencing import x` and `from …proposal_assist import fencing`
(that second spelling was an open Minor in F1, closed in F2) — and change only the
skip condition and the name:

```python
def test_only_f3a_identities_import_the_fencing_primitives():
    """Scoped successor to F2's unconditional guard. F3a itself imports nothing
    from fencing — but F3b's ports will, and they live in registry.py and the
    agent modules, so the allowlist is shared with the column check."""
    offenders = []
    for p in sorted(SRC.rglob("*.py")):
        if "__pycache__" in p.parts or p.name == "fencing.py":
            continue
        if str(p.relative_to(SRC)) in F3A_GENERATION_FILES:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.endswith("proposal_assist.fencing") or any(
                    a.name == "fencing" for a in node.names
                ):
                    offenders.append(f"{p}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith("proposal_assist.fencing"):
                        offenders.append(f"{p}: import {alias.name}")
    assert not offenders, (
        "fencing primitives imported outside F3a's identities:\n  " + "\n  ".join(offenders)
    )
```

Import `F3A_GENERATION_FILES` in the test module's existing `from tests.fencing.inventory import (...)` block.

- [ ] **Step 7: Mutation — put the column in a file outside the allowlist**

Add `# claimed_at_generation` as a comment to `src/call_manager/proposal_assist/analysis/pipeline.py`.

```bash
uv run pytest tests/fencing/test_fence_coverage.py -q > /tmp/t1m.log 2>&1; echo "EXIT=$?"; grep -E '^(FAILED|[0-9]+ (passed|failed))' /tmp/t1m.log
```
**Required outcome:** exactly `test_only_f3a_identities_touch_claimed_at_generation` goes RED. Revert the comment and confirm green.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check . > /tmp/t1r.log 2>&1; echo "RUFF_EXIT=$?"; tail -5 /tmp/t1r.log
git add tests/fencing/
git commit -m "test(fencing): migrate the gate to F3 sub-slices (F3a Task 1)"
```

---

### Task 2: The interactive claim records and returns its generation

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/processor.py:215-231`
- Test: `tests/proposal_assist/test_f3a_claim_identity.py` (create)

**Interfaces:**
- Consumes: `SLICE_STATUS`, `F3A_GENERATION_FILES` from Task 1.
- Produces: the claim's `RETURNING` gains `TurnJob.claimed_at_generation`, so `process_turn_once` holds `claimed_generation: int` after the claim.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_f3a_claim_identity.py`:

```python
"""F3a — every tool-originating actor records the generation it claimed at."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update

from call_manager.db.models import ChatTurn, TurnJob, Workspace
from tests.proposal_assist.conftest import make_workspace


def _seed_queued_turn(session, *, generation: int):
    """make_workspace is a FUNCTION taking a session (tests/proposal_assist/
    conftest.py), not a fixture. TurnJob needs a ChatTurn — see the canonical
    seed at tests/test_workspaces_service.py:33-54."""
    ws = make_workspace(session)
    session.flush()
    session.execute(
        update(Workspace).where(Workspace.id == ws.id).values(lifecycle_generation=generation)
    )
    turn = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
    session.add(turn)
    session.flush()
    job = TurnJob(
        workspace_id=ws.id,
        user_turn_id=turn.id,
        submitted_by_user_id=ws.owner_id,
        status="queued",
        queued_at=datetime.now(UTC),
    )
    session.add(job)
    session.commit()
    return job.id


def test_the_interactive_claim_stores_the_workspaces_current_generation(pg_session_factory):
    """The claim is the ONLY safe capture point. A read after it lets a
    delete/restore land in between, and the actor then adopts a generation it
    never claimed at."""
    with pg_session_factory() as s:
        job_id = _seed_queued_turn(s, generation=7)

    from call_manager.proposal_assist.turns.processor import claim_turn_job

    claimed = claim_turn_job(pg_session_factory, job_id)

    assert claimed is not None
    assert claimed.claimed_at_generation == 7
    with pg_session_factory() as s:
        stored = s.scalar(select(TurnJob.claimed_at_generation).where(TurnJob.id == job_id))
    assert stored == 7, "the generation must be persisted by the claim, not just returned"


def test_a_second_claim_of_the_same_turn_job_returns_none(pg_session_factory):
    """The claim's status guard is unchanged by F3a. Without this, adding a
    column to .values() could quietly widen what the statement matches."""
    with pg_session_factory() as s:
        job_id = _seed_queued_turn(s, generation=0)

    from call_manager.proposal_assist.turns.processor import claim_turn_job

    assert claim_turn_job(pg_session_factory, job_id) is not None
    assert claim_turn_job(pg_session_factory, job_id) is None
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest tests/proposal_assist/test_f3a_claim_identity.py -q > /tmp/t2.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t2.log
```
Expected: FAIL — `cannot import name 'claim_turn_job'`.

- [ ] **Step 3: Extract the claim into a named function that captures the generation**

In `src/call_manager/proposal_assist/turns/processor.py`, add near the other module-level helpers:

```python
class ClaimedTurn(NamedTuple):
    """What the interactive claim returns. `claimed_at_generation` is the
    actor's BELIEF for the whole turn — captured by the claim statement itself,
    because a read after it lets a delete/restore land in the gap and the actor
    would adopt a generation it never claimed at."""

    workspace_id: uuid.UUID
    user_turn_id: uuid.UUID
    submitted_by_user_id: int | None
    claimed_at_generation: int


def claim_turn_job(session_factory: sessionmaker, turn_job_id: uuid.UUID) -> ClaimedTurn | None:
    """Atomically claim a queued turn and capture the workspace generation.

    The generation is a CORRELATED SUBQUERY inside the same UPDATE, so the
    capture and the claim are one statement and one snapshot. Splitting them
    reopens the race this exists to close.
    """
    current_generation = (
        select(Workspace.lifecycle_generation)
        .where(Workspace.id == TurnJob.workspace_id)
        .scalar_subquery()
    )
    with session_factory() as s:
        row = s.execute(
            update(TurnJob)
            .where(TurnJob.id == turn_job_id, TurnJob.status == "queued")
            .values(
                status="running",
                started_at=datetime.now(UTC),
                claimed_at_generation=current_generation,
            )
            .returning(
                TurnJob.workspace_id,
                TurnJob.user_turn_id,
                TurnJob.submitted_by_user_id,
                TurnJob.claimed_at_generation,
            )
        ).first()
        s.commit()
    return None if row is None else ClaimedTurn(*row)
```

Add `NamedTuple` to the `typing` import and `Workspace` to the models import if absent.

- [ ] **Step 4: Call it from `process_turn_once`**

Replace the inline claim block (`with session_factory() as s:` through `workspace_id, user_turn_id, submitted_by_user_id = result`) with:

```python
    claimed = claim_turn_job(session_factory, turn_job_id)
    if claimed is None:
        logger.info("turn_job %s already claimed or completed; skipping", turn_job_id)
        return
    workspace_id = claimed.workspace_id
    user_turn_id = claimed.user_turn_id
    submitted_by_user_id = claimed.submitted_by_user_id
    claimed_generation = claimed.claimed_at_generation
```

Leave the comment about `RETURNING` bringing back `submitted_by_user_id` attached to the new function's docstring or above the call — do not drop it.

- [ ] **Step 5: Run and confirm green**

```bash
uv run pytest tests/proposal_assist/test_f3a_claim_identity.py tests/proposal_assist/test_turn_processor.py -q > /tmp/t2.log 2>&1; echo "EXIT=$?"; tail -5 /tmp/t2.log
```
Expected: PASS, both new tests plus the existing processor suite.

- [ ] **Step 6: Mutation — capture the generation with a separate read**

Replace the correlated subquery with a read before the UPDATE:

```python
    with session_factory() as s:
        gen = s.scalar(select(Workspace.lifecycle_generation).where(...))
        row = s.execute(update(TurnJob)...values(claimed_at_generation=gen)...)
```

**Required outcome:** both new tests must still PASS — this mutation is not observable by a single-threaded test, which is exactly why Global Constraint 2 is a *constraint* and not a test. Record this honestly in the task report: **the one-statement capture is enforced by review, not by these tests.** Revert.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check . > /tmp/t2r.log 2>&1; echo "RUFF_EXIT=$?"; tail -5 /tmp/t2r.log
git add src/call_manager/proposal_assist/turns/processor.py tests/proposal_assist/test_f3a_claim_identity.py
git commit -m "feat(turns): the interactive claim captures its workspace generation (F3a Task 2)"
```

---

### Task 3: The four background claims capture their generation

**Files:**
- Modify: `src/call_manager/proposal_assist/coherence/jobs.py`
- Modify: `src/call_manager/proposal_assist/evaluator/jobs.py`
- Modify: `src/call_manager/proposal_assist/review/jobs.py`
- Modify: `src/call_manager/proposal_assist/uncited_claims/jobs.py`
- Test: `tests/proposal_assist/test_f3a_claim_identity.py` (extend)

**Interfaces:**
- Produces: each `claim_next_*` sets `job.claimed_at_generation` before returning; the caller reads it off the returned ORM object.

Each of the four has the same shape: a `SELECT … FOR UPDATE SKIP LOCKED` that already filters on `Workspace.soft_deleted_at.is_(None)`, then mutates the job and flushes.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_f3a_claim_identity.py`:

```python
import uuid

import pytest

from call_manager.db.models import CoherenceJob, EvaluatorJob, ReviewRun, UncitedClaimsJob
from call_manager.proposal_assist.coherence.jobs import claim_next_coherence_job
from call_manager.proposal_assist.evaluator.jobs import claim_next_evaluator_job
from call_manager.proposal_assist.review.jobs import claim_next_review_run
from call_manager.proposal_assist.uncited_claims.jobs import claim_next_uncited_claims_job

#: ReviewRun.draft_sha is NOT NULL with no server_default — the other three need
#: only workspace_id and status. Constructors verified against the canonical seed
#: at tests/test_workspaces_service.py:37-42.
_BACKGROUND_CLAIMS = [
    ("coherence", lambda ws_id: CoherenceJob(workspace_id=ws_id, status="queued"), claim_next_coherence_job),
    ("evaluator", lambda ws_id: EvaluatorJob(workspace_id=ws_id, status="queued"), claim_next_evaluator_job),
    ("review", lambda ws_id: ReviewRun(workspace_id=ws_id, status="queued", draft_sha="a" * 64), claim_next_review_run),
    ("uncited", lambda ws_id: UncitedClaimsJob(workspace_id=ws_id, status="queued"), claim_next_uncited_claims_job),
]


@pytest.mark.parametrize("name,make_job,claim_fn", _BACKGROUND_CLAIMS, ids=[c[0] for c in _BACKGROUND_CLAIMS])
def test_every_background_claim_captures_the_generation(
    pg_session_factory, name, make_job, claim_fn
):
    """All four build a ToolContext and therefore originate tool calls. A
    context without a generation cannot be fenced by F3b, so the claim is where
    the value has to come from."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.execute(
            update(Workspace).where(Workspace.id == ws.id).values(lifecycle_generation=4)
        )
        s.add(make_job(ws.id))
        s.commit()

    with pg_session_factory() as s:
        job = claim_fn(s)
        assert job is not None
        assert job.claimed_at_generation == 4
        s.commit()
```

If a model's required columns differ (for example `ReviewRun` may need `requested_by`)- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest tests/proposal_assist/test_f3a_claim_identity.py -q -k background > /tmp/t3.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t3.log
```
Expected: 4 FAIL — `assert None == 4`.

- [ ] **Step 3: Capture the generation in each claim**

In each of the four `jobs.py` files, immediately after `job.started_at = datetime.now(UTC)` and **before** `session.flush()`, insert:

```python
    # F3a: the actor's belief for this run. Read inside the claim transaction
    # while the job row is held FOR UPDATE. A value captured here can only be
    # STALE relative to a concurrent transition, never newer — and a stale
    # belief fails its fence closed, which is the safe direction.
    job.claimed_at_generation = session.scalar(
        select(Workspace.lifecycle_generation).where(Workspace.id == job.workspace_id)
    )
```

`Workspace` and `select` are already imported in all four modules (each builds a `live_ws` subquery). Verify before adding an import.

- [ ] **Step 4: Run and confirm green**

```bash
uv run pytest tests/proposal_assist/test_f3a_claim_identity.py -q > /tmp/t3.log 2>&1; echo "EXIT=$?"; tail -5 /tmp/t3.log
```
Expected: PASS, 6 collected.

- [ ] **Step 5: Mutation — capture in only three of the four**

Remove the new lines from `review/jobs.py`.

```bash
uv run pytest tests/proposal_assist/test_f3a_claim_identity.py -q > /tmp/t3m.log 2>&1; echo "EXIT=$?"; grep -E '^(FAILED|[0-9]+ (passed|failed))' /tmp/t3m.log
```
**Required outcome:** exactly the `ReviewRun` parametrisation goes RED. If more fail, the parametrisation is sharing state between cases. Revert and confirm green.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check . > /tmp/t3r.log 2>&1; echo "RUFF_EXIT=$?"; tail -5 /tmp/t3r.log
git add src/call_manager/proposal_assist/*/jobs.py tests/proposal_assist/test_f3a_claim_identity.py
git commit -m "feat(jobs): the four background claims capture their generation (F3a Task 3)"
```

---

### Task 4: Carry the generation to the one-shot contexts

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/registry.py` (the `ToolContext` dataclass)
- Modify: `src/call_manager/proposal_assist/{coherence,evaluator,review,uncited_claims}/pass_runner.py`
- Modify: `src/call_manager/proposal_assist/{coherence,evaluator,review,uncited_claims}/agent.py`
- Test: `tests/proposal_assist/test_f3a_claim_identity.py` (extend)

**Interfaces:**
- Consumes: `job.claimed_at_generation` from Task 3.
- Produces: `ToolContext.lifecycle_generation: int | None` and `ToolContext.active_turn_job_id: uuid.UUID | None`; `build_*_adapter(..., lifecycle_generation: int)` on all four builders.

The drains discard everything but the job id — `coherence/pass_runner.py:189` is `job = claim_next_coherence_job(s); s.commit()` returning `None`. The generation has to be threaded through `drain_* → run_* → adapter_builder → tools_factory`.

- [ ] **Step 1: Add the two fields to `ToolContext`**

In `src/call_manager/proposal_assist/tools/registry.py`, after the `origin_job_id` field:

```python
    # F3a: the generation this actor CLAIMED at — its belief for the whole run.
    # One-shot contexts (the four background agents) receive it at construction.
    # The retained interactive context cannot: it is built during
    # controller.launch(), which deliberately precedes the claim
    # (turns/processor.py — the orphan sweep must run first), so the value does
    # not exist yet. It is stamped per turn by set_turn_provenance instead, and
    # snapshotted per INVOCATION by the dispatcher (see Invocation).
    lifecycle_generation: int | None = field(default=None)
    # F3a: the turn job that owns THIS turn's tool calls, for owner-bound
    # accounting. NOT origin_job_id — that is deliberately None for interactive
    # contexts (see the origin_kind comment above) and is copied into durable
    # provenance by the propose_* tools, so reusing it would change stored data.
    active_turn_job_id: uuid.UUID | None = field(default=None)
```

- [ ] **Step 2: Write the failing test**

Append to `tests/proposal_assist/test_f3a_claim_identity.py`:

```python
def test_a_one_shot_context_carries_the_generation_at_construction(tmp_path):
    """One-shot contexts KNOW their generation when they are built, so they must
    carry it then. Only the retained interactive context may omit it, because
    launch precedes the claim."""
    from call_manager.proposal_assist.evaluator import agent as evaluator_agent

    captured: dict = {}

    class _FakeSDK:
        def __init__(self, *, tools_factory=None, **kwargs):
            captured["ctx"] = tools_factory(tmp_path, uuid.uuid4())

    evaluator_agent.build_evaluator_adapter(
        settings=None,
        session_factory=lambda: None,
        workspace_id=uuid.uuid4(),
        requested_by=1,
        job_id=uuid.uuid4(),
        draft_section_count=0,
        spec_section_count=0,
        rubric_criteria=[],
        lifecycle_generation=11,
        _sdk_factory=_FakeSDK,
    )
    assert captured["ctx"].lifecycle_generation == 11
```

`build_evaluator_adapter` currently has **no** injection seam and its real
keyword-only parameters are `settings, session_factory, workspace_id,
requested_by, job_id, draft_section_count, spec_section_count, rubric_criteria`
(`evaluator/agent.py:29-38`). Step 4 adds `lifecycle_generation` and
`_sdk_factory=ClaudeAgentSDKAdapter` — the same test-injection pattern
`tools/references.py` uses for `embedder`.

- [ ] **Step 3: Run it and watch it fail**

```bash
uv run pytest tests/proposal_assist/test_f3a_claim_identity.py -q -k one_shot > /tmp/t4.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t4.log
```
Expected: FAIL — unexpected keyword argument `lifecycle_generation`.

- [ ] **Step 4: Thread the generation through all four chains**

For each of coherence, evaluator, review, uncited_claims:

In `pass_runner.py`, change the drain to keep the generation:

```python
    with session_factory() as s:
        job = claim_next_evaluator_job(s)
        s.commit()
        job_id = job.id if job is not None else None
        claimed_generation = job.claimed_at_generation if job is not None else None
    if job_id is None:
        return
    await run_evaluator_pass(
        job_id=job_id,
        claimed_generation=claimed_generation,
        ...
    )
```

Add `claimed_generation: int` as a keyword-only parameter to `run_*_pass`, and pass it into the `adapter_builder(...)` call as `lifecycle_generation=claimed_generation`.

In `agent.py`, add `lifecycle_generation: int` to the builder's keyword-only parameters and set `lifecycle_generation=lifecycle_generation` in the `ToolContext(...)` construction.

- [ ] **Step 5: Run and confirm green**

```bash
uv run pytest tests/proposal_assist/ -q > /tmp/t4.log 2>&1; echo "EXIT=$?"; tail -5 /tmp/t4.log
```
Expected: PASS.

- [ ] **Step 6: Mutation — default the parameter instead of requiring it**

Change one builder's signature to `lifecycle_generation: int | None = None`.

**Required outcome:** `test_a_one_shot_context_carries_the_generation_at_construction` stays GREEN (it passes a value), but a NEW test must catch it. Add:

```python
def test_a_one_shot_builder_REQUIRES_the_generation():
    """A default would let a caller silently build a context with no belief,
    which is the failure F3b cannot detect and the fence cannot survive."""
    import inspect

    from call_manager.proposal_assist.evaluator.agent import build_evaluator_adapter

    param = inspect.signature(build_evaluator_adapter).parameters["lifecycle_generation"]
    assert param.default is inspect.Parameter.empty
```

Re-run with the mutation still applied; this new test must go RED. Revert both and confirm green.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check . > /tmp/t4r.log 2>&1; echo "RUFF_EXIT=$?"; tail -5 /tmp/t4r.log
git add src/call_manager/proposal_assist/ tests/proposal_assist/test_f3a_claim_identity.py
git commit -m "feat(agents): one-shot contexts carry their claim generation (F3a Task 4)"
```

---

### Task 5: `set_turn_provenance` stamps the retained context

**Files:**
- Modify: `src/call_manager/proposal_assist/drafting/claude_adapter.py:725-748`
- Modify: `src/call_manager/proposal_assist/turns/processor.py` (the call site)
- Test: `tests/proposal_assist/test_f3a_invocation_snapshot.py` (create)

**Interfaces:**
- Consumes: `claimed_generation` from Task 2, `ToolContext.lifecycle_generation` / `.active_turn_job_id` from Task 4.
- Produces: `set_turn_provenance(..., lifecycle_generation: int, active_turn_job_id: uuid.UUID)`.

- [ ] **Step 1: Write the failing two-turn test**

Create `tests/proposal_assist/test_f3a_invocation_snapshot.py`:

```python
"""F3a — the retained context is re-stamped every turn, and each tool
invocation gets an immutable snapshot of it."""

from __future__ import annotations

import uuid

import pytest

from call_manager.proposal_assist.tools.registry import ToolContext


def _ctx() -> ToolContext:
    return ToolContext(
        workspace_id=uuid.uuid4(),
        repo_dir=None,
        session_factory=None,
        settings=None,
    )


@pytest.mark.asyncio
async def test_two_turns_through_one_retained_context_carry_different_generations():
    """The retained subprocess and its ctx are reused across turns, so a stamp
    missed once leaks the previous turn's value. registry.py documents the same
    trap for provenance. No static check can see this; only a runtime one can."""
    from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter

    adapter = ClaudeAgentSDKAdapter.__new__(ClaudeAgentSDKAdapter)
    ctx = _ctx()
    adapter._tool_context_sink = {ctx.workspace_id: ctx}

    job_a, job_b = uuid.uuid4(), uuid.uuid4()
    await adapter.set_turn_provenance(
        ctx.workspace_id,
        triggering_turn_id=uuid.uuid4(),
        triggering_prompt="one",
        created_by=1,
        origin_session_id=None,
        lifecycle_generation=3,
        active_turn_job_id=job_a,
    )
    assert (ctx.lifecycle_generation, ctx.active_turn_job_id) == (3, job_a)

    await adapter.set_turn_provenance(
        ctx.workspace_id,
        triggering_turn_id=uuid.uuid4(),
        triggering_prompt="two",
        created_by=1,
        origin_session_id=None,
        lifecycle_generation=4,
        active_turn_job_id=job_b,
    )
    assert (ctx.lifecycle_generation, ctx.active_turn_job_id) == (4, job_b)
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest tests/proposal_assist/test_f3a_invocation_snapshot.py -q > /tmp/t5.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t5.log
```
Expected: FAIL — unexpected keyword argument `lifecycle_generation`.

- [ ] **Step 3: Extend `set_turn_provenance`**

Add two keyword-only parameters and two assignments:

```python
    async def set_turn_provenance(
        self,
        workspace_id: uuid.UUID,
        *,
        triggering_turn_id: uuid.UUID | None,
        triggering_prompt: str | None,
        created_by: int | None,
        origin_session_id: str | None,
        lifecycle_generation: int,
        active_turn_job_id: uuid.UUID,
        allowed_tool_names: set[str] | None = None,
        turn_selection: dict | None = None,
    ) -> None:
```

and in the body, alongside the existing assignments:

```python
        # F3a: stamped UNCONDITIONALLY every turn for the same reason the
        # provenance fields are — the subprocess and this ctx are reused across
        # turns, so a missed stamp leaks the PREVIOUS turn's generation, and a
        # stale belief wearing the fence's uniform is worse than none.
        ctx.lifecycle_generation = lifecycle_generation
        ctx.active_turn_job_id = active_turn_job_id
```

Both are **required**, not defaulted — a default would let a caller skip the stamp silently.

- [ ] **Step 4: Update the call site**

In `turns/processor.py`, add to the existing `await controller.set_turn_provenance(...)` call:

```python
            lifecycle_generation=claimed_generation,
            active_turn_job_id=turn_job_id,
```

- [ ] **Step 5: Run and confirm green**

```bash
uv run pytest tests/proposal_assist/ -q > /tmp/t5.log 2>&1; echo "EXIT=$?"; tail -5 /tmp/t5.log
```
Expected: PASS.

- [ ] **Step 6: Mutation — stamp only when the value changes**

Wrap the two new assignments in `if ctx.lifecycle_generation != lifecycle_generation:`.

**Required outcome:** the two-turn test stays GREEN (the values do differ), so add the case that catches it:

```python
@pytest.mark.asyncio
async def test_the_stamp_is_unconditional_even_when_the_generation_repeats():
    """A conditional stamp leaks active_turn_job_id across turns whenever the
    generation happens NOT to change — which is the common case, since most
    turns run against a workspace nobody deleted."""
    from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter

    adapter = ClaudeAgentSDKAdapter.__new__(ClaudeAgentSDKAdapter)
    ctx = _ctx()
    adapter._tool_context_sink = {ctx.workspace_id: ctx}
    job_a, job_b = uuid.uuid4(), uuid.uuid4()

    for job_id in (job_a, job_b):
        await adapter.set_turn_provenance(
            ctx.workspace_id,
            triggering_turn_id=uuid.uuid4(),
            triggering_prompt="t",
            created_by=1,
            origin_session_id=None,
            lifecycle_generation=3,      # UNCHANGED across both turns
            active_turn_job_id=job_id,
        )

    assert ctx.active_turn_job_id == job_b, "the second turn's job id was dropped"
```

Re-run with the mutation applied; the new test must go RED. Revert and confirm green.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check . > /tmp/t5r.log 2>&1; echo "RUFF_EXIT=$?"; tail -5 /tmp/t5r.log
git add src/call_manager/proposal_assist/ tests/proposal_assist/test_f3a_invocation_snapshot.py
git commit -m "feat(drafting): set_turn_provenance stamps generation and turn job (F3a Task 5)"
```

---

### Task 6: The per-invocation snapshot

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/registry.py` (`Invocation`, `_safe_tool`)
- Test: `tests/proposal_assist/test_f3a_invocation_snapshot.py` (extend)

**Interfaces:**
- Consumes: `ToolContext.lifecycle_generation` / `.active_turn_job_id` from Tasks 4 and 5.
- Produces: `Invocation` (frozen), `current_invocation() -> Invocation | None`. F3b's effect operations take their stamp from `current_invocation()`.

This is the task the spec calls the highest-risk silent failure. The retained context is **mutated** by `set_turn_provenance`, so a tool that reads the generation *late* reads a newer one — and its stale write then fences perfectly against the wrong epoch. The window is real because `tools/references.py:527` hands work to `run_in_threadpool`, and a Python thread cannot be interrupted.

`contextvars` is the mechanism: verified that `run_in_threadpool` copies the context into the worker thread, and that a later `set()` in the outer task does **not** reach it.

- [ ] **Step 1: Write the failing overlapping-invocation test**

Append to `tests/proposal_assist/test_f3a_invocation_snapshot.py`:

```python
@pytest.mark.asyncio
async def test_an_in_flight_invocation_keeps_its_generation_across_a_restamp():
    """THE test for this slice. A tool blocked in run_in_threadpool must not
    observe a generation stamped after it started: its write would then be
    fenced perfectly against the WRONG epoch, which is worse than unfenced
    because it looks correct."""
    import asyncio

    from call_manager.proposal_assist.tools.registry import _safe_tool, current_invocation

    ctx = _ctx()
    ctx.lifecycle_generation = 3
    ctx.active_turn_job_id = uuid.uuid4()

    entered = asyncio.Event()
    release = asyncio.Event()
    seen: dict = {}

    async def slow_impl(_ctx):
        entered.set()
        await release.wait()                 # the restamp happens in this gap
        seen["generation"] = current_invocation().lifecycle_generation
        return {"ok": True}

    task = asyncio.create_task(_safe_tool("read_proposal", ctx, slow_impl, ctx))
    await entered.wait()

    ctx.lifecycle_generation = 5             # what set_turn_provenance does
    release.set()
    await task

    assert seen["generation"] == 3, (
        "the invocation read the generation LATE and adopted a newer one"
    )
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest tests/proposal_assist/test_f3a_invocation_snapshot.py -q -k in_flight > /tmp/t6.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t6.log
```
Expected: FAIL — `cannot import name 'current_invocation'`.

- [ ] **Step 3: Add `Invocation` and set it in the dispatcher**

In `src/call_manager/proposal_assist/tools/registry.py`:

```python
import contextvars
from dataclasses import dataclass


@dataclass(frozen=True)
class Invocation:
    """One tool call's immutable identity.

    Snapshotted at DISPATCH, before any await or thread handoff. It exists
    because ToolContext is MUTABLE and shared: set_turn_provenance restamps the
    retained context every turn, so a tool that reads the generation when its
    effect happens can read a value stamped after it began. Its write would then
    be fenced against the wrong generation — which looks correct and is not.
    """

    workspace_id: uuid.UUID
    lifecycle_generation: int | None
    active_turn_job_id: uuid.UUID | None
    origin_kind: str
    origin_job_id: uuid.UUID | None
    triggering_turn_id: uuid.UUID | None


_CURRENT_INVOCATION: contextvars.ContextVar[Invocation | None] = contextvars.ContextVar(
    "call_manager_current_invocation", default=None
)


def current_invocation() -> Invocation | None:
    """The snapshot for the tool call on this task. Propagates into
    run_in_threadpool (anyio copies the context), and a later set() in the
    outer task does not reach an already-running worker."""
    return _CURRENT_INVOCATION.get()
```

At the top of `_safe_tool`, before `_deny_if_out_of_turn`:

```python
    _CURRENT_INVOCATION.set(
        Invocation(
            workspace_id=ctx.workspace_id,
            lifecycle_generation=ctx.lifecycle_generation,
            active_turn_job_id=ctx.active_turn_job_id,
            origin_kind=ctx.origin_kind,
            origin_job_id=ctx.origin_job_id,
            triggering_turn_id=ctx.triggering_turn_id,
        )
    )
```

Each MCP tool handler runs as its own task, so `set()` binds to that task's context and does not leak between concurrent invocations.

- [ ] **Step 4: Run and confirm green**

```bash
uv run pytest tests/proposal_assist/test_f3a_invocation_snapshot.py -q > /tmp/t6.log 2>&1; echo "EXIT=$?"; tail -5 /tmp/t6.log
```
Expected: PASS.

- [ ] **Step 5: Write the thread-handoff test**

The async test above proves the snapshot survives an `await`. The dangerous shape is a thread:

```python
@pytest.mark.asyncio
async def test_the_snapshot_survives_run_in_threadpool():
    """tools/references.py hands ingestion to run_in_threadpool, and a Python
    thread cannot be interrupted — so this is the widest window in which a
    restamp can overtake a running tool."""
    from starlette.concurrency import run_in_threadpool

    from call_manager.proposal_assist.tools.registry import _safe_tool, current_invocation

    ctx = _ctx()
    ctx.lifecycle_generation = 3
    seen: dict = {}

    async def impl(_ctx):
        def in_thread():
            return current_invocation().lifecycle_generation
        seen["generation"] = await run_in_threadpool(in_thread)
        return {"ok": True}

    await _safe_tool("read_proposal", ctx, impl, ctx)
    assert seen["generation"] == 3
```

- [ ] **Step 6: Mutation — read from the context instead of the snapshot**

Change both tests' impls to read `_ctx.lifecycle_generation` rather than `current_invocation().lifecycle_generation`.

**Required outcome:** `test_an_in_flight_invocation_keeps_its_generation_across_a_restamp` goes RED (it reads 5). This is the proof that the snapshot is what makes the difference, not the plumbing around it. Revert.

- [ ] **Step 7: Mutation — snapshot inside the impl rather than the dispatcher**

Move the `_CURRENT_INVOCATION.set(...)` call out of `_safe_tool` and into a helper the test calls directly.

**Required outcome:** both tests must go RED, because they drive `_safe_tool` and nothing sets the var. This is Global Constraint 7: a helper-level snapshot test passes while production still hands over mutable context. Revert.

- [ ] **Step 8: Full suite, lint, commit**

```bash
uv run pytest -n 16 --dist loadfile -q > /tmp/t6full.log 2>&1; echo "EXIT=$?"; tail -3 /tmp/t6full.log
uv run ruff check . > /tmp/t6r.log 2>&1; echo "RUFF_EXIT=$?"; tail -5 /tmp/t6r.log
git add src/call_manager/proposal_assist/tools/registry.py tests/proposal_assist/test_f3a_invocation_snapshot.py
git commit -m "feat(tools): per-invocation identity snapshot at dispatch (F3a Task 6)"
```

---

## Verification checklist

- [ ] `uv run pytest -n 16 --dist loadfile` → all green, `1 skipped`
- [ ] `uv run ruff check .` (redirected, exit read) → `All checks passed!`
- [ ] `SLICE_STATUS` has F3a–F3f, F4, F5, all `pending`; every inventory row `fenced: false`
- [ ] No `src/` file outside `F3A_GENERATION_FILES` mentions `claimed_at_generation`
- [ ] `git diff --stat 28fc684..HEAD` touches only `tests/fencing/`, the five actor packages, `tools/registry.py`, `drafting/claude_adapter.py`, and the two new test files
- [ ] No migration, no route, no frontend, no OpenAPI regeneration

## What F3a deliberately leaves undone

- **Nothing is fenced.** No effect checks a generation. `Invocation` is produced and carried, and nothing consumes it until F3b's operations do.
- **The one-statement capture in Task 2 is enforced by review, not by a test.** Task 2 Step 6 records this honestly: a single-threaded test cannot distinguish a correlated subquery from a separate read. Global Constraint 2 is the control.
- **`queue.py::claim_queued_jobs` and `setup.py::claim_next_setup_job` gain nothing.** They originate no tool call; they belong to F4 and F5.
- **`review/pass_runner.py:344` still writes git with no mutex.** It is not a tool, so no boundary in the F3 sequence reaches it; `2026-08-17` F4 owns it, and its operation must take the mutex *before* loading its locked `ReviewRun` row or it creates a J→M inversion.
