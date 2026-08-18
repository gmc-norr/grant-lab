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

Master is at `3566663`. Expected baseline: **`3586 passed, 1 skipped`** (3587 collected).

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
- Produces: `SLICE_STATUS` keys `F3a`–`F3f`, `F4`, `F5`; `F3A_COLUMN_FILES` (the allowlist later tasks are checked against).

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

Also update the defaults **and make `merge()` preserve an existing row's slice.**
Today `inventory.py:150` rebuilds it unconditionally:

```python
"slice": _SLICE_FOR_KIND.get(s.kind, _DEFAULT_SLICE),
```

so the next regeneration silently reverts every assignment made in Step 1 — F3a
claims back to F4, setup back to F4, F3a contexts to F3d, ingest to F3d. Change it
to keep what a human classified, defaulting only for genuinely new identities:

```python
_SLICE_FOR_KIND = {KIND_CLAIM_FN: "F4"}   # a NEW claim fn still defaults to F4
_DEFAULT_SLICE = "F3d"                    # was "F3"

# ... inside merge(), where the row is built:
"slice": (
    prev["slice"] if prev is not None
    else _SLICE_FOR_KIND.get(s.kind, _DEFAULT_SLICE)
),
```

`prev` is the same previous-row lookup the merge already uses for `fenced` and
`OPTIONAL_KEYS`, and it must follow the rename map so a renamed identity carries
its slice across too.

- [ ] **Step 4b: Prove the map survives regeneration**

Add to `tests/fencing/test_inventory_merge.py`:

```python
def test_regeneration_preserves_every_human_slice_assignment():
    """merge() rebuilt `slice` from the kind defaults, so a regeneration would
    silently revert F3a's claims to F4, setup to F5's row to F4, the contexts to
    F3d and ingest to F3d — with every other gate still green."""
    before = {(r["kind"], r["path"], r["symbol"]): r["slice"] for r in load_inventory()}
    merged = merge(load_inventory(), scan_tree(), renames={})
    after = {(r["kind"], r["path"], r["symbol"]): r["slice"] for r in merged}
    assert after == before, "regeneration changed a human slice assignment"
    assert len(after) == 20
```

Run it against the UNFIXED merge first and confirm it fails, then apply the fix.

- [ ] **Step 5: Run and confirm green**

```bash
uv run pytest tests/fencing/ -q > /tmp/t1.log 2>&1; echo "EXIT=$?"; tail -5 /tmp/t1.log
```
Expected: PASS, 67 collected.

- [ ] **Step 6: Replace the two unconditional inertness guards**

Both go red as soon as Tasks 2–6 land. Replace them — do **not** delete them.

In `tests/fencing/inventory.py`, add:

```python
#: The ONLY files that may name `claimed_at_generation`. Scoped successor to F2's
#: unconditional guard, which forbade it everywhere and therefore goes red the
#: moment F3a's first claim writes it. Every path is an F3a identity or the
#: plumbing that carries its value.
F3A_COLUMN_FILES = frozenset({
    "proposal_assist/turns/processor.py",
    "proposal_assist/coherence/jobs.py",
    "proposal_assist/coherence/pass_runner.py",
    "proposal_assist/evaluator/jobs.py",
    "proposal_assist/evaluator/pass_runner.py",
    "proposal_assist/review/jobs.py",
    "proposal_assist/review/pass_runner.py",
    "proposal_assist/uncited_claims/jobs.py",
    "proposal_assist/uncited_claims/pass_runner.py",
    "db/models.py",
})

#: A SEPARATE, EXACT set for importing the F1 fencing primitives. It is EMPTY
#: through F3a — nothing imports them yet — and F3b adds precisely its own port
#: implementation files. Sharing one list with the column check would both permit
#: premature fencing imports across all of F3a's files and block F3b's ports,
#: which live in neither.
FENCING_IMPORT_FILES: frozenset[str] = frozenset()

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
        and str(p.relative_to(SRC)) not in F3A_COLUMN_FILES
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
    """Scoped successor to F2's unconditional guard, on its OWN exact set.
    F3A_COLUMN_FILES is the wrong list here in both directions: it would permit a
    premature fencing import anywhere in F3a's files, and it would block F3b's
    ports, which live in neither set."""
    offenders = []
    for p in sorted(SRC.rglob("*.py")):
        if "__pycache__" in p.parts or p.name == "fencing.py":
            continue
        if str(p.relative_to(SRC)) in FENCING_IMPORT_FILES:
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

Import `F3A_COLUMN_FILES` in the test module's existing `from tests.fencing.inventory import (...)` block.

- [ ] **Step 7: Mutation — put the column in a file outside the allowlist**

Add `# claimed_at_generation` as a comment to `src/call_manager/proposal_assist/analysis/pipeline.py`.

```bash
uv run pytest tests/fencing/test_fence_coverage.py -q > /tmp/t1m.log 2>&1; echo "EXIT=$?"; grep -E '^(FAILED|[0-9]+ (passed|failed))' /tmp/t1m.log
```
**Required outcome:** exactly `test_only_f3a_identities_touch_claimed_at_generation`
goes RED. Revert the comment and confirm green.

- [ ] **Step 7b: Mutation — import a fencing primitive**

Add `from call_manager.proposal_assist import fencing  # noqa: F401` to
`src/call_manager/proposal_assist/analysis/pipeline.py`.

**Required outcome:** exactly `test_only_f3a_identities_import_the_fencing_primitives`
goes RED. The two guards have separate lists and must fail independently — if this
mutation reddens the column test too, the lists have been merged. Revert.

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
- Consumes: `SLICE_STATUS`, `F3A_COLUMN_FILES` from Task 1.
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

Read the generation **after** the claim — the unsafe direction. Reading *before*
can only produce an older, fail-closed belief; reading after is how an actor adopts
a generation it never claimed at:

```python
    with session_factory() as s:
        row = s.execute(update(TurnJob).where(...).values(status="running")...).first()
        s.commit()
        gen = s.scalar(select(Workspace.lifecycle_generation).where(Workspace.id == row.workspace_id))
        s.execute(update(TurnJob).where(TurnJob.id == turn_job_id).values(claimed_at_generation=gen))
        s.commit()
```

**Required outcome:** both tests stay GREEN — a single-threaded test cannot observe
the race. So add the check that *is* mechanical, and require it to go RED:

```python
def test_the_claim_writes_the_generation_in_ONE_statement(pg_session_factory):
    """Global Constraint 2, made testable. A separate SELECT takes a FRESH
    snapshot under READ COMMITTED and can therefore see a NEWER generation —
    the actor then adopts an epoch it never claimed at and passes its fence."""
    from sqlalchemy import event

    statements: list[str] = []

    with pg_session_factory() as s:
        job_id = _seed_queued_turn(s, generation=2)

    from call_manager.proposal_assist.turns.processor import claim_turn_job

    engine = pg_session_factory.kw["bind"]

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, params, context, executemany):
        statements.append(" ".join(statement.split()).upper())

    try:
        claim_turn_job(pg_session_factory, job_id)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    writes = [q for q in statements if "CLAIMED_AT_GENERATION" in q]
    assert len(writes) == 1, f"expected one claim statement, got {len(writes)}"
    assert writes[0].startswith("UPDATE TURN_JOBS"), writes[0]
    assert "RETURNING" in writes[0]
```

If `pg_session_factory.kw["bind"]` is not how this fixture exposes its engine, read
`tests/conftest.py:70` and use whatever it does bind. Revert the mutation and
confirm all three tests green.

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

- [ ] **Step 3: Capture the generation IN the claim SELECT**

**Not a second statement.** A separate `SELECT` after the lock takes a *fresh*
snapshot under READ COMMITTED, so it can see a **newer** generation — and the
actor would adopt a generation it never claimed at, then pass its fence. That is
Global Constraint 2, and the job row's `FOR UPDATE` does not help: the delete path
changes `Workspace`, not the job. Today `cancel_on_delete` touches the job row and
masks it; step 3 of the architecture removes that.

Each of the four claims gains a join and returns both. For `evaluator/jobs.py`:

```python
    row = session.execute(
        select(EvaluatorJob, Workspace.lifecycle_generation)
        .join(Workspace, Workspace.id == EvaluatorJob.workspace_id)
        .where(
            EvaluatorJob.status == "queued",
            EvaluatorJob.workspace_id.not_in(running_ws),
            Workspace.soft_deleted_at.is_(None),
        )
        .order_by(EvaluatorJob.queued_at)
        .limit(1)
        # `of=` keeps FOR UPDATE on the JOB only. Without it PostgreSQL would
        # also lock the Workspace row, putting this claim in the delete path's
        # way and inverting the lock order the architecture spec fixes in §5.
        .with_for_update(of=EvaluatorJob, skip_locked=True)
    ).first()
    if row is None:
        return None
    job, generation = row
    job.status = "running"
    job.started_at = datetime.now(UTC)
    job.claimed_at_generation = generation
    session.flush()
    return job
```

The existing `live_ws` subquery is replaced by the join's
`Workspace.soft_deleted_at.is_(None)` clause — the same filter, now also yielding
the generation. Delete the now-unused `live_ws` binding.

Apply the same shape to `coherence/jobs.py`, `uncited_claims/jobs.py`, and
`review/jobs.py`. **`review/jobs.py` differs**: its variable is `run`, not `job`,
it orders by `ReviewRun.triggered_at`, and it sets **no** `started_at` — do not add
one. Its tail becomes:

```python
    run, generation = row
    run.status = "running"
    run.claimed_at_generation = generation
    session.flush()
    return run
```

- [ ] **Step 4: Run and confirm green**

```bash
uv run pytest tests/proposal_assist/test_f3a_claim_identity.py -q > /tmp/t3.log 2>&1; echo "EXIT=$?"; tail -5 /tmp/t3.log
```
Expected: PASS, 6 collected.

- [ ] **Step 5: Mutation A — capture in only three of the four**

Remove the `claimed_at_generation` assignment from `review/jobs.py`.

```bash
uv run pytest tests/proposal_assist/test_f3a_claim_identity.py -q > /tmp/t3m.log 2>&1; echo "EXIT=$?"; grep -E '^(FAILED|[0-9]+ (passed|failed))' /tmp/t3m.log
```
**Required outcome:** exactly the `review` parametrisation goes RED. If more fail,
the parametrisation is sharing state between cases. Revert and confirm green.

- [ ] **Step 5b: Mutation B — split the capture into a second statement**

In `evaluator/jobs.py`, revert to the pre-join `select(EvaluatorJob)` and capture
with a separate `session.scalar(select(Workspace.lifecycle_generation)...)`.

**Required outcome:** all four tests stay GREEN — a single-threaded test cannot
distinguish one statement from two. Record this in the task report: **the
one-statement capture is enforced by Global Constraint 2 and review, not by these
tests.** This mutation exists to document that honestly, not to pass. Revert.

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
def test_a_one_shot_context_carries_the_generation_at_construction(tmp_path, monkeypatch):
    """One-shot contexts KNOW their generation when they are built, so they must
    carry it then. Only the retained interactive context may omit it, because
    launch precedes the claim."""
    from call_manager.proposal_assist.evaluator import agent as evaluator_agent

    captured: dict = {}

    # tools_factory returns build_proposal_assist_mcp_server(ctx) — an MCP
    # server config, NOT the ToolContext. Capture the factory's INPUT.
    import call_manager.proposal_assist.evaluator.agent as agent_mod

    def _capture(ctx):
        captured["ctx"] = ctx
        return object()

    monkeypatch.setattr(agent_mod, "build_proposal_assist_mcp_server", _capture)

    class _FakeSDK:
        def __init__(self, *, tools_factory=None, **kwargs):
            tools_factory(tmp_path, uuid.uuid4())

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

- [ ] **Step 4b: Update every existing direct caller**

`lifecycle_generation` is required on all four builders and `claimed_generation` on
all four `run_*_pass`, so every existing direct caller breaks. There are **8 test
files** that call them:

```bash
grep -rln 'run_evaluator_pass\|run_coherence_pass\|run_review_pass\|run_uncited_claims_pass\|build_evaluator_adapter\|build_review_adapter\|build_keeper_adapter\|build_uncited_claims_adapter' tests/
```

Pass an explicit generation in each — do not add a default to the production
signature to make them compile. Step 6 pins that.

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

- [ ] **Step 4: Update ALL FIVE layers, not just the adapter**

`set_turn_provenance` is declared or forwarded in five places. Changing only
`claude_adapter.py` fails immediately, because `controller.py` rejects the unknown
keywords before the adapter ever sees them. Add the two keyword-only parameters —
`lifecycle_generation: int` and `active_turn_job_id: uuid.UUID`, both required — to
each, and forward them:

| File:line | What to change |
|---|---|
| `drafting/contract.py:189` | the Protocol / ABC signature |
| `drafting/controller.py:263` | signature **and** the forward at `:276` |
| `drafting/registry.py:526` | signature **and** the forward at `:539` |
| `drafting/fake_adapter.py:128` | signature; record both values so tests can assert them |
| `drafting/claude_adapter.py:725` | signature and the two `ctx.` assignments (Step 3) |

Then in `turns/processor.py:320`, add to the existing call:

```python
            lifecycle_generation=claimed_generation,
            active_turn_job_id=turn_job_id,
```

- [ ] **Step 4b: Update the existing callers and their tests**

`grep -rn 'set_turn_provenance' tests/` and add both keywords to every call. Run
the contract, gating, fake-adapter and registry suites specifically — they exercise
these signatures directly and will fail on an unfilled required parameter.

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

- [ ] **Step 3: Add `Invocation` and pass it EXPLICITLY**

Revision 8 requires `_safe_tool` to hand the impl an immutable `Invocation`, and
effect ports to take an explicit stamp. **A `contextvars` ambient lookup is not
that contract** — it would make `current_invocation()` a hidden argument to every
F3b effect operation, and it leaks across calls in a task unless every path resets
its token. Explicit passing also survives the thread handoff for free: the value is
bound in the impl's own frame.

In F3a no production impl consumes it yet, so `_safe_tool` gains an opt-in that
F3d flips per tool as it converts them.

In `src/call_manager/proposal_assist/tools/registry.py`:

```python
@dataclass(frozen=True)
class Invocation:
    """One tool call's immutable identity and per-turn provenance.

    Snapshotted at DISPATCH, before any await or thread handoff. It exists
    because ToolContext is MUTABLE and shared: set_turn_provenance restamps the
    retained context every turn, so a tool reading these values when its effect
    happens can read a LATER turn's. A stale generation paired with a newer
    turn's prompt is worse than either alone.

    Every mutable per-turn field an effect consumes belongs here. The provenance
    fields are copied into durable proposed-change rows by the propose_* tools
    (tools/proposal.py), so a late read misattributes stored data.
    """

    workspace_id: uuid.UUID
    lifecycle_generation: int | None
    active_turn_job_id: uuid.UUID | None
    origin_kind: str
    origin_job_id: uuid.UUID | None
    triggering_turn_id: uuid.UUID | None
    triggering_prompt: str | None
    created_by: int | None
    origin_session_id: str | None
    turn_selection: dict | None

    @classmethod
    def snapshot(cls, ctx: "ToolContext") -> "Invocation":
        return cls(
            workspace_id=ctx.workspace_id,
            lifecycle_generation=ctx.lifecycle_generation,
            active_turn_job_id=ctx.active_turn_job_id,
            origin_kind=ctx.origin_kind,
            origin_job_id=ctx.origin_job_id,
            triggering_turn_id=ctx.triggering_turn_id,
            triggering_prompt=ctx.triggering_prompt,
            created_by=ctx.created_by,
            origin_session_id=ctx.origin_session_id,
            turn_selection=ctx.turn_selection,
        )
```

Change `_safe_tool`'s signature and its impl call:

```python
async def _safe_tool(
    tool_name: str,
    ctx: ToolContext,
    impl: Callable[..., Awaitable[dict[str, Any]]],
    *args: Any,
    pass_invocation: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
```

and where it currently calls `await impl(*args, **kwargs)`:

```python
        if pass_invocation:
            kwargs["invocation"] = Invocation.snapshot(ctx)
        result = await impl(*args, **kwargs)
```

Take the snapshot **before** `_deny_if_out_of_turn` returns, so a denied call
cannot leave a half-built identity anywhere. No production call site passes
`pass_invocation=True` in F3a.

- [ ] **Step 4: Run and confirm green**

The Step 1 test must now pass `pass_invocation=True` and accept `invocation`:

```python
    async def slow_impl(_ctx, *, invocation):
        entered.set()
        await release.wait()                 # the restamp happens in this gap
        seen["generation"] = invocation.lifecycle_generation
        seen["prompt"] = invocation.triggering_prompt
        return {"ok": True}

    task = asyncio.create_task(
        _safe_tool("read_proposal", ctx, slow_impl, ctx, pass_invocation=True)
    )
```

and assert **both** fields, because a late read of either misattributes durable
data:

```python
    ctx.lifecycle_generation = 5             # what set_turn_provenance does
    ctx.triggering_prompt = "turn two"
    release.set()
    await task

    assert seen["generation"] == 3
    assert seen["prompt"] == "turn one", (
        "the invocation read provenance LATE and would stamp turn two's prompt "
        "onto a proposed change made during turn one"
    )
```

Set `ctx.triggering_prompt = "turn one"` in the test's setup.

```bash
uv run pytest tests/proposal_assist/test_f3a_invocation_snapshot.py -q > /tmp/t6.log 2>&1; echo "EXIT=$?"; tail -5 /tmp/t6.log
```
Expected: PASS.

- [ ] **Step 5: Write the thread-handoff test**

The async test proves the snapshot survives an `await`. The dangerous shape is a
thread — `tools/references.py:527` hands ingestion to `run_in_threadpool`, and a
Python thread cannot be interrupted:

```python
@pytest.mark.asyncio
async def test_the_snapshot_survives_run_in_threadpool():
    from starlette.concurrency import run_in_threadpool

    from call_manager.proposal_assist.tools.registry import _safe_tool

    ctx = _ctx()
    ctx.lifecycle_generation = 3
    seen: dict = {}

    async def impl(_ctx, *, invocation):
        def in_thread():
            return invocation.lifecycle_generation
        seen["generation"] = await run_in_threadpool(in_thread)
        return {"ok": True}

    await _safe_tool("read_proposal", ctx, impl, ctx, pass_invocation=True)
    assert seen["generation"] == 3
```

- [ ] **Step 6: Mutation — read from the context instead of the snapshot**

Change both impls to read `_ctx.lifecycle_generation` and `_ctx.triggering_prompt`.

**Required outcome:** `test_an_in_flight_invocation_keeps_its_generation_across_a_restamp`
goes RED on both assertions. This proves the snapshot is what makes the difference,
not the plumbing around it. Revert.

- [ ] **Step 7: Mutation — snapshot inside the impl rather than the dispatcher**

Move `Invocation.snapshot(ctx)` out of `_safe_tool` and have the test build it
itself before dispatching.

**Required outcome:** the in-flight test goes RED, because the test would then
snapshot before `ctx` is mutated only by luck of ordering — and more importantly
the production dispatcher no longer produces one. This is Global Constraint 7: a
helper-level snapshot test passes while production still hands over mutable
context. Revert.

- [ ] **Step 8: Full suite, lint, commit**

```bash
uv run pytest -n 16 --dist loadfile -q > /tmp/t6full.log 2>&1; echo "EXIT=$?"; tail -3 /tmp/t6full.log
uv run ruff check . > /tmp/t6r.log 2>&1; echo "RUFF_EXIT=$?"; tail -5 /tmp/t6r.log
git add src/call_manager/proposal_assist/tools/registry.py tests/proposal_assist/test_f3a_invocation_snapshot.py
git commit -m "feat(tools): per-invocation identity snapshot at dispatch (F3a Task 6)"
```

---

### Task 7: Activate the F3a gate

**Files:**
- Modify: `tests/fencing/fence_inventory.json`
- Modify: `tests/fencing/inventory.py`

**Interfaces:**
- Consumes: everything Tasks 2–6 built.

While F3a's nine rows stay `fenced: false`, `test_every_fenced_entry_really_fences`
**skips all of them** — so nothing the slice built is checked by the gate the slice
exists to feed. This task closes that, and must be the last commit.

- [ ] **Step 1: Flip the nine F3a rows**

In `fence_inventory.json`, set `"fenced": true` on the 4 `claim_fn` rows now
assigned F3a and the 5 `tool_context` rows. Leave every F3d/F3e/F4/F5 row `false`.

- [ ] **Step 2: Run and watch the gate fail**

```bash
uv run pytest tests/fencing/ -q > /tmp/t7.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t7.log
```
Expected: FAIL — `test_each_slice_matches_its_declared_status` says F3a is
`pending` but its rows are `true`.

- [ ] **Step 3: Mark F3a complete**

In `tests/fencing/inventory.py`, change `"F3a": "pending"` to `"F3a": "complete"`.

- [ ] **Step 4: Run the full suite**

```bash
uv run pytest -n 16 --dist loadfile -q > /tmp/t7full.log 2>&1; echo "EXIT=$?"; tail -3 /tmp/t7full.log
```
Expected: PASS.

- [ ] **Step 5: Mutation — flip one row back**

Set `claim_next_review_run` to `"fenced": false`.

**Required outcome:** `test_each_slice_matches_its_declared_status` goes RED — F3a
is `complete` so every one of its rows must be `true`. This is what makes a
forgotten conversion fail instead of passing silently. Revert.

- [ ] **Step 6: Commit**

```bash
uv run ruff check . > /tmp/t7r.log 2>&1; echo "RUFF_EXIT=$?"; tail -5 /tmp/t7r.log
git add tests/fencing/
git commit -m "test(fencing): activate the F3a gate (F3a Task 7)"
```

---

## Verification checklist

- [ ] `uv run pytest -n 16 --dist loadfile` → all green, `1 skipped`
- [ ] `uv run ruff check .` (redirected, exit read) → `All checks passed!`
- [ ] `SLICE_STATUS["F3a"] == "complete"` with its 9 rows `fenced: true`; F3b–F3f, F4, F5 `pending` with every row `false`
- [ ] No `src/` file outside `F3A_COLUMN_FILES` mentions `claimed_at_generation`
- [ ] `git diff --stat 3566663..HEAD` touches only `tests/fencing/`, the five actor packages, `tools/registry.py`, the five drafting layers (`contract`, `controller`, `registry`, `fake_adapter`, `claude_adapter`), the existing tests that call the changed signatures, and the two new test files
- [ ] No migration, no route, no frontend, no OpenAPI regeneration

## What F3a deliberately leaves undone

- **Nothing is fenced.** No effect checks a generation. `Invocation` is produced and carried, and nothing consumes it until F3b's operations do.
- **The one-statement capture is proven by SQL SHAPE, not by the race.** A
  single-threaded test cannot observe the interleaving, so Task 2 asserts that
  exactly one statement writes the column and that it is the claim's
  `UPDATE … RETURNING`. Task 3's equivalent has no such check and is recorded as
  review-enforced — its four claims are ORM mutations inside one flush, where the
  statement shape is not a stable thing to assert on.
- **`queue.py::claim_queued_jobs` and `setup.py::claim_next_setup_job` gain nothing.** They originate no tool call; they belong to F4 and F5.
- **`review/pass_runner.py:344` still writes git with no mutex.** It is not a tool, so no boundary in the F3 sequence reaches it; `2026-08-17` F4 owns it, and its operation must take the mutex *before* loading its locked `ReviewRun` row or it creates a J→M inversion.
