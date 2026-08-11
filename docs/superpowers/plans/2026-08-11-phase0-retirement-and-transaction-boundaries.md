# Phase-0 Retirement and Transaction Boundaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a Phase-0 setup job from writing to a workspace that has already been deleted, by giving Phase 0 real transaction boundaries and a bounded lock.

**Architecture:** `Phase0Worker` stops holding one long-lived session and instead runs three per-milestone transactions off a `session_factory`. Only milestone 1 touches the filesystem, so only it takes a lock — `acquire_commit_mutex_live` followed by the workspace row lock, which is what makes a concurrent DELETE return 429 instead of blocking forever on a row. The two LLM milestones compute outside any transaction and persist under short locks, revalidating a run-local Call source hash so a re-ingestion mid-run fails the job cleanly instead of committing a mixed snapshot.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, Alembic, PostgreSQL 16, pytest + testcontainers, APScheduler, `uv`, `ruff`.

**Spec:** `docs/superpowers/specs/2026-08-11-phase0-retirement-and-transaction-boundaries-design.md`

## Global Constraints

- Python 3.12. Run everything through `uv run` (e.g. `uv run pytest …`, `uv run ruff check .`).
- `ruff` is the linter and formatter. `uv run ruff check .` and `uv run ruff format --check .` must pass before every commit.
- Tests requiring Postgres use the existing `pg_session_factory` fixture (testcontainers). Do not add a second database fixture.
- **Never pipe pytest through `tail`/`head`** — the exit code becomes the pager's. Redirect to a file and echo `$?`:
  `uv run pytest <args> > /tmp/out.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -40 /tmp/out.txt`
- The migration in Task 1 is revision `0061`, `down_revision = "0060"`. Do not renumber.
- Model `__table_args__` in `src/call_manager/db/models.py` must be changed in the same task as the migration, or tests that build tables from metadata will use the stale constraint.
- `MAX_SETUP_ATTEMPTS = 5` and `STUCK_SETUP_AFTER = timedelta(minutes=30)` are unchanged.
- `_RETIREMENT_BARRIER_SECONDS = 6.0` in `src/call_manager/proposal_assist/retirement.py` is unchanged.
- Out of scope (belongs to Slice C): the pass registry, claim-analysis, the settings/rubric mutex routes, the `soft_delete_workspace` wrapper removal, repo-content scrubbing.

---

## File Structure

**Create:**
- `alembic/versions/0061_setup_job_cancelled_status.py` — the `cancelled` status and the partial unique index on `rubric_initialized` events, with a data-converting downgrade.
- `src/call_manager/proposal_assist/jobs_retirement.py` — the `"jobs"` retirement hook and its reconciliation pass. A neutral home so `main.py` and `scheduler.py` both import it without importing the Phase-0 worker. Slice C extends this module.
- `tests/proposal_assist/test_phase0_transaction_boundaries.py` — milestone boundaries, locks, Call pinning, bounded retry.
- `tests/proposal_assist/test_phase0_retirement.py` — cancellation, the hook, the reconciliation tick, the migration's downgrade ordering.

**Modify:**
- `src/call_manager/db/models.py` — `WorkspaceSetupJob.__table_args__` status constraint; the partial unique index on `workspace_events`.
- `src/call_manager/proposal_assist/extractors/refresh.py` — split each `ensure_*_fresh` into a freshness predicate, a pure `compute_*`, and a `persist_*`.
- `src/call_manager/proposal_assist/phases/setup.py` — the bulk of the work: hermetic git, the `live_ws` claim filter, `Phase0Worker` on a `session_factory`, three milestones, `CallSourceChanged`, the failure path.
- `src/call_manager/proposal_assist/retirement.py` — `REQUIRED_HOOKS` gains `"jobs"`; budget comment.
- `src/call_manager/main.py` — register the `"jobs"` hook.
- `src/call_manager/config.py` — `jobs_reconcile_tick_seconds`.
- `src/call_manager/scheduler.py` — register the reconciliation tick, independent of the drafting flags.
- `tests/proposal_assist/test_extractors_refresh.py` — moved to the split shape.
- `tests/proposal_assist/test_retirement_coordinator.py` — the hook set and the budget invariant.

---

### Task 1: Migration 0061 — `cancelled` status and the audit-event index

**Files:**
- Create: `alembic/versions/0061_setup_job_cancelled_status.py`
- Modify: `src/call_manager/db/models.py` (`WorkspaceSetupJob.__table_args__`, `WorkspaceEvent.__table_args__`)
- Test: `tests/proposal_assist/test_phase0_retirement.py`

**Interfaces:**
- Consumes: nothing.
- Produces: status value `'cancelled'` is legal on `workspace_setup_jobs.status`; a partial unique index `uq_workspace_events_rubric_initialized` on `workspace_events(workspace_id) WHERE kind = 'rubric_initialized'`; and a module-level `downgrade_statements() -> list[str]` in the migration for Task 1's ordering test.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_phase0_retirement.py`:

```python
"""Phase-0 retirement: cancelled status, the jobs hook, and its reconciliation."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from call_manager.db.models import Workspace, WorkspaceEvent, WorkspaceSetupJob
from tests.proposal_assist.conftest import make_workspace

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0061_setup_job_cancelled_status.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig0061", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cancelled_is_an_accepted_status(pg_session_factory, current_user):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        job = WorkspaceSetupJob(workspace_id=ws.id, status="cancelled")
        s.add(job)
        s.commit()
        assert s.get(WorkspaceSetupJob, job.id).status == "cancelled"


def test_only_one_rubric_initialized_event_per_workspace(pg_session_factory, current_user):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        s.add(WorkspaceEvent(workspace_id=ws.id, kind="rubric_initialized", payload={}))
        s.commit()
        s.add(WorkspaceEvent(workspace_id=ws.id, kind="rubric_initialized", payload={}))
        with pytest.raises(Exception):
            s.commit()
        s.rollback()
    # A different kind is unaffected by the partial index.
    with pg_session_factory() as s:
        ws2 = make_workspace(s, owner=current_user)
        s.flush()
        s.add(WorkspaceEvent(workspace_id=ws2.id, kind="member_added", payload={}))
        s.add(WorkspaceEvent(workspace_id=ws2.id, kind="member_added", payload={}))
        s.commit()


def test_downgrade_converts_cancelled_rows_before_restoring_the_constraint(
    pg_session_factory, current_user
):
    """The ordering IS the finding: restoring the old CheckConstraint while a
    'cancelled' row exists fails. Runs the migration's own statements, so the
    test cannot drift from the migration."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        job = WorkspaceSetupJob(workspace_id=ws.id, status="cancelled")
        s.add(job)
        s.commit()
        job_id = job.id

    with pg_session_factory() as s:
        for stmt in _load_migration().downgrade_statements():
            s.execute(text(stmt))
        s.commit()
        assert s.get(WorkspaceSetupJob, job_id).status == "failed"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/proposal_assist/test_phase0_retirement.py -v > /tmp/t1.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -40 /tmp/t1.txt
```

Expected: FAIL. `test_cancelled_is_an_accepted_status` raises `IntegrityError` on `ck_workspace_setup_jobs_status`; `test_only_one_rubric_initialized_event_per_workspace` fails because the second commit succeeds; the downgrade test fails at `FileNotFoundError` / `spec` being `None`.

- [ ] **Step 3: Write the migration**

Create `alembic/versions/0061_setup_job_cancelled_status.py`:

```python
"""workspace_setup_jobs: add 'cancelled'; one rubric_initialized event per workspace

Revision ID: 0061
Revises: 0060
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None

_OLD_STATUSES = "'queued','running','waiting_for_rubric_review','done','failed'"
_NEW_STATUSES = f"{_OLD_STATUSES},'cancelled'"


def downgrade_statements() -> list[str]:
    """The downgrade as raw SQL, in order, so a test can prove the ordering.

    The conversion MUST precede the constraint: restoring the old CheckConstraint
    while a 'cancelled' row exists fails against any database where a workspace
    has been deleted mid-setup.
    """
    return [
        "DROP INDEX IF EXISTS uq_workspace_events_rubric_initialized",
        "UPDATE workspace_setup_jobs SET status = 'failed' WHERE status = 'cancelled'",
        "ALTER TABLE workspace_setup_jobs DROP CONSTRAINT ck_workspace_setup_jobs_status",
        "ALTER TABLE workspace_setup_jobs ADD CONSTRAINT ck_workspace_setup_jobs_status "
        f"CHECK (status IN ({_OLD_STATUSES}))",
    ]


def upgrade() -> None:
    op.drop_constraint(
        "ck_workspace_setup_jobs_status", "workspace_setup_jobs", type_="check"
    )
    op.create_check_constraint(
        "ck_workspace_setup_jobs_status",
        "workspace_setup_jobs",
        f"status IN ({_NEW_STATUSES})",
    )
    # Backstop for §7 of the spec: the atomic M3 transaction is the mechanism that
    # prevents a duplicate rubric_initialized event; this index is defence in depth.
    op.create_index(
        "uq_workspace_events_rubric_initialized",
        "workspace_events",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'rubric_initialized'"),
    )


def downgrade() -> None:
    for stmt in downgrade_statements():
        op.execute(stmt)
```

- [ ] **Step 4: Update the models to match**

In `src/call_manager/db/models.py`, in `WorkspaceSetupJob.__table_args__`, replace the status `CheckConstraint` string with:

```python
        CheckConstraint(
            "status IN ('queued','running','waiting_for_rubric_review','done','failed','cancelled')",
            name="ck_workspace_setup_jobs_status",
        ),
```

In the `WorkspaceEvent` class, add to `__table_args__` (create the tuple if the class has none — keep any existing entries):

```python
        Index(
            "uq_workspace_events_rubric_initialized",
            "workspace_id",
            unique=True,
            postgresql_where=text("kind = 'rubric_initialized'"),
        ),
```

Ensure `Index` and `text` are imported at the top of `models.py` (both are already used elsewhere in the file; add to the existing `from sqlalchemy import …` line only if missing).

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/proposal_assist/test_phase0_retirement.py -v > /tmp/t1.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -40 /tmp/t1.txt
```

Expected: PASS (3 passed).

- [ ] **Step 6: Prove the ordering gate is not vacuous**

Temporarily swap the second and third entries of `downgrade_statements()` (constraint before conversion), re-run, and confirm `test_downgrade_converts_cancelled_rows_before_restoring_the_constraint` FAILS with a check-constraint violation. Then restore the correct order and re-run to green.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add alembic/versions/0061_setup_job_cancelled_status.py src/call_manager/db/models.py tests/proposal_assist/test_phase0_retirement.py
git commit -m "feat(phase0): add cancelled setup status and rubric_initialized uniqueness (0061)"
```

---

### Task 2: Split the extractors into compute and persist

**Files:**
- Modify: `src/call_manager/proposal_assist/extractors/refresh.py`
- Test: `tests/proposal_assist/test_extractors_refresh.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces, all keyword-only:
  - `section_extracts_are_fresh(*, session: Session, call: Call, extractor_version: str) -> bool`
  - `compute_section_extracts(*, call_text: str, client: LlmClient) -> dict[str, str]`
  - `persist_section_extracts(*, session: Session, call: Call, sections: dict[str, str], model: str, extractor_version: str, source_hash: str) -> None`
  - `rubric_is_fresh(*, session: Session, call: Call, extractor_version: str) -> bool`
  - `compute_rubric(*, call_text: str, client: LlmClient) -> dict`
  - `persist_rubric(*, session: Session, call: Call, payload: dict, model: str, extractor_version: str, source_hash: str) -> None`
  - The old `ensure_section_extracts_fresh` / `ensure_rubric_fresh` are **removed**, not kept — a test-only production function is the same foot-gun the spec removes elsewhere.

- [ ] **Step 1: Rewrite the tests to the split shape**

Replace the body of `tests/proposal_assist/test_extractors_refresh.py` with the same five behaviours expressed against the new functions. Each existing test maps one-to-one; the example below is the pair that changes most, and the remaining tests follow the same pattern (build a `Call`, assert the predicate, compute, persist, assert rows).

```python
def test_section_extracts_are_not_fresh_first_time(pg_session_factory):
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        assert section_extracts_are_fresh(
            session=s, call=call, extractor_version="v1"
        ) is False


def test_persist_writes_one_row_per_section(pg_session_factory):
    client = _FakeClient(model="m")
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        sections = compute_section_extracts(call_text=call.full_text, client=client)
        persist_section_extracts(
            session=s,
            call=call,
            sections=sections,
            model=client.model,
            extractor_version="v1",
            source_hash=source_text_hash(call.full_text),
        )
        s.commit()
        rows = s.scalars(
            select(CallSectionExtract).where(CallSectionExtract.call_id == call.id)
        ).all()
        assert len(rows) == len(SECTION_NAMES)
        assert section_extracts_are_fresh(
            session=s, call=call, extractor_version="v1"
        ) is True


def test_fresh_is_false_after_a_version_bump(pg_session_factory):
    client = _FakeClient(model="m")
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        persist_section_extracts(
            session=s,
            call=call,
            sections=compute_section_extracts(call_text=call.full_text, client=client),
            model=client.model,
            extractor_version="v1",
            source_hash=source_text_hash(call.full_text),
        )
        s.commit()
        assert section_extracts_are_fresh(
            session=s, call=call, extractor_version="v2"
        ) is False


def test_fresh_is_false_after_the_source_text_changes(pg_session_factory):
    client = _FakeClient(model="m")
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        persist_section_extracts(
            session=s,
            call=call,
            sections=compute_section_extracts(call_text=call.full_text, client=client),
            model=client.model,
            extractor_version="v1",
            source_hash=source_text_hash(call.full_text),
        )
        s.commit()
        call.full_text = "different body"
        s.commit()
        assert section_extracts_are_fresh(
            session=s, call=call, extractor_version="v1"
        ) is False


def test_a_failing_extractor_cannot_touch_existing_rows(pg_session_factory):
    """Previously an ordering convention inside one function ('EXTRACT FIRST').
    Now structural: compute raises, so persist is never reached."""
    good = _FakeClient(model="m")
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        persist_section_extracts(
            session=s,
            call=call,
            sections=compute_section_extracts(call_text=call.full_text, client=good),
            model=good.model,
            extractor_version="v1",
            source_hash=source_text_hash(call.full_text),
        )
        s.commit()
        before = {
            r.section: r.extract_text
            for r in s.scalars(
                select(CallSectionExtract).where(CallSectionExtract.call_id == call.id)
            )
        }

        boom = _RaisingClient(model="m")
        with pytest.raises(RuntimeError):
            compute_section_extracts(call_text=call.full_text, client=boom)

        after = {
            r.section: r.extract_text
            for r in s.scalars(
                select(CallSectionExtract).where(CallSectionExtract.call_id == call.id)
            )
        }
        assert after == before
```

Then the rubric half — written out rather than described, because a task may be read out of order:

```python
def test_rubric_is_not_fresh_first_time(pg_session_factory):
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        assert rubric_is_fresh(session=s, call=call, extractor_version="v1") is False


def test_persist_rubric_writes_then_reads_fresh(pg_session_factory):
    client = _FakeClient(model="m")
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        persist_rubric(
            session=s,
            call=call,
            payload=compute_rubric(call_text=call.full_text, client=client),
            model=client.model,
            extractor_version="v1",
            source_hash=source_text_hash(call.full_text),
        )
        s.commit()
        assert s.get(CallRubric, call.id) is not None
        assert rubric_is_fresh(session=s, call=call, extractor_version="v1") is True


def test_rubric_is_not_fresh_after_a_version_bump(pg_session_factory):
    client = _FakeClient(model="m")
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        persist_rubric(
            session=s,
            call=call,
            payload=compute_rubric(call_text=call.full_text, client=client),
            model=client.model,
            extractor_version="v1",
            source_hash=source_text_hash(call.full_text),
        )
        s.commit()
        assert rubric_is_fresh(session=s, call=call, extractor_version="v2") is False


def test_rubric_is_not_fresh_after_the_source_text_changes(pg_session_factory):
    client = _FakeClient(model="m")
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        persist_rubric(
            session=s,
            call=call,
            payload=compute_rubric(call_text=call.full_text, client=client),
            model=client.model,
            extractor_version="v1",
            source_hash=source_text_hash(call.full_text),
        )
        s.commit()
        call.full_text = "different body"
        s.commit()
        assert rubric_is_fresh(session=s, call=call, extractor_version="v1") is False


def test_a_failing_rubric_extractor_cannot_touch_the_existing_row(pg_session_factory):
    good = _FakeClient(model="m")
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        persist_rubric(
            session=s,
            call=call,
            payload=compute_rubric(call_text=call.full_text, client=good),
            model=good.model,
            extractor_version="v1",
            source_hash=source_text_hash(call.full_text),
        )
        s.commit()
        before = (s.get(CallRubric, call.id).criteria, s.get(CallRubric, call.id).extracted_by_model)

        with pytest.raises(RuntimeError):
            compute_rubric(call_text=call.full_text, client=_RaisingClient(model="m"))

        row = s.get(CallRubric, call.id)
        assert (row.criteria, row.extracted_by_model) == before
```

The file's imports become:

```python
import pytest
from dataclasses import dataclass
from sqlalchemy import select

from call_manager.db.models import CallRubric, CallSectionExtract
from call_manager.proposal_assist.extractors.refresh import (
    compute_rubric,
    compute_section_extracts,
    persist_rubric,
    persist_section_extracts,
    rubric_is_fresh,
    section_extracts_are_fresh,
)
from call_manager.proposal_assist.extractors.sections import SECTION_NAMES
from call_manager.proposal_assist.hashing import source_text_hash
```

Keep the file's existing `_make_call` and `_FakeClient` helpers; add `_RaisingClient` if the file does not already have one:

```python
@dataclass
class _RaisingClient:
    model: str

    def chat(self, *, messages, response_format=None):
        raise RuntimeError("extractor exploded")
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/proposal_assist/test_extractors_refresh.py -v > /tmp/t2.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -40 /tmp/t2.txt
```

Expected: FAIL with `ImportError: cannot import name 'section_extracts_are_fresh'`.

- [ ] **Step 3: Implement the split**

In `src/call_manager/proposal_assist/extractors/refresh.py`, replace `ensure_section_extracts_fresh` and `ensure_rubric_fresh` with:

```python
def section_extracts_are_fresh(
    *, session: Session, call: Call, extractor_version: str
) -> bool:
    """True when every section row matches the call's current text hash AND the
    given extractor version — i.e. no LLM call is needed."""
    current_hash = source_text_hash(call.full_text or "")
    existing = session.scalars(
        select(CallSectionExtract).where(CallSectionExtract.call_id == call.id)
    ).all()
    return (
        len(existing) == len(SECTION_NAMES)
        and all(r.source_text_hash == current_hash for r in existing)
        and all(r.extractor_version == extractor_version for r in existing)
    )


def compute_section_extracts(*, call_text: str, client: LlmClient) -> dict[str, str]:
    """Pure: one LLM round-trip, no session, no writes. Raising here can never
    damage cached rows because nothing has been touched."""
    return extract_sections(call_text=call_text, client=client)


def persist_section_extracts(
    *,
    session: Session,
    call: Call,
    sections: dict[str, str],
    model: str,
    extractor_version: str,
    source_hash: str,
) -> None:
    """Replace the call's section rows. No LLM. Caller commits."""
    existing = session.scalars(
        select(CallSectionExtract).where(CallSectionExtract.call_id == call.id)
    ).all()
    if existing:
        session.execute(
            delete(CallSectionExtract).where(CallSectionExtract.call_id == call.id)
        )
        # Flush so the DELETE lands before our INSERTs hit the PK constraint.
        session.flush()

    now = datetime.now(UTC)
    for section_name in SECTION_NAMES:
        session.add(
            CallSectionExtract(
                call_id=call.id,
                section=section_name,
                extract_text=sections[section_name],
                extracted_at=now,
                extracted_by_model=model,
                extractor_version=extractor_version,
                source_text_hash=source_hash,
            )
        )


def rubric_is_fresh(*, session: Session, call: Call, extractor_version: str) -> bool:
    current_hash = source_text_hash(call.full_text or "")
    existing = session.get(CallRubric, call.id)
    return (
        existing is not None
        and existing.source_text_hash == current_hash
        and existing.extractor_version == extractor_version
    )


def compute_rubric(*, call_text: str, client: LlmClient) -> dict:
    """Pure: one LLM round-trip, no session, no writes."""
    return extract_rubric(call_text=call_text, client=client)


def persist_rubric(
    *,
    session: Session,
    call: Call,
    payload: dict,
    model: str,
    extractor_version: str,
    source_hash: str,
) -> None:
    """Upsert the call's rubric row. No LLM. Caller commits."""
    now = datetime.now(UTC)
    existing = session.get(CallRubric, call.id)
    if existing is None:
        session.add(
            CallRubric(
                call_id=call.id,
                criteria=payload["criteria"],
                funder_notes=payload["funder_notes"],
                extracted_at=now,
                extracted_by_model=model,
                extractor_version=extractor_version,
                source_text_hash=source_hash,
            )
        )
    else:
        existing.criteria = payload["criteria"]
        existing.funder_notes = payload["funder_notes"]
        existing.extracted_at = now
        existing.extracted_by_model = model
        existing.extractor_version = extractor_version
        existing.source_text_hash = source_hash
```

The module docstring's reference to "Phase 0 calls these" stays accurate; update its second sentence to read "Phase 0 checks freshness, computes outside any transaction, then persists under a lock."

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/proposal_assist/test_extractors_refresh.py -v > /tmp/t2.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -40 /tmp/t2.txt
```

Expected: PASS (10 passed).

`setup.py` still imports the removed names, so the wider suite is red until Task 5. That is expected; do not "fix" it by keeping the old functions.

- [ ] **Step 5: Commit**

```bash
uv run ruff check src/call_manager/proposal_assist/extractors/refresh.py tests/proposal_assist/test_extractors_refresh.py
git add src/call_manager/proposal_assist/extractors/refresh.py tests/proposal_assist/test_extractors_refresh.py
git commit -m "refactor(extractors): split ensure_*_fresh into freshness/compute/persist"
```

---

### Task 3: Hermetic git with real timeouts

**Files:**
- Modify: `src/call_manager/proposal_assist/phases/setup.py`
- Test: `tests/proposal_assist/test_phase0_transaction_boundaries.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_GIT_TIMEOUT_SECONDS: float`, and
  `_run_git(args: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]`
  used by every git invocation in this module.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_phase0_transaction_boundaries.py`:

```python
"""Phase-0 milestone boundaries: locks, Call pinning, bounded retry."""

import subprocess
from pathlib import Path

import pytest

from call_manager.proposal_assist.phases import setup as setup_mod


def test_git_runs_hermetically_and_with_a_timeout(tmp_path, monkeypatch):
    """Site git config, hooks and signing must not reach into M1's locked region,
    and every command must be able to time out rather than hang under the lock."""
    seen: dict = {}

    def _fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(setup_mod.subprocess, "run", _fake_run)
    setup_mod._run_git(["init", "-q", "-b", "main"], cwd=tmp_path)

    assert seen["kwargs"]["timeout"] == setup_mod._GIT_TIMEOUT_SECONDS
    assert "core.hooksPath=/dev/null" in seen["args"]
    assert "commit.gpgsign=false" in seen["args"]
    env = seen["kwargs"]["env"]
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_a_hung_git_raises_rather_than_blocking(tmp_path, monkeypatch):
    def _boom(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(setup_mod.subprocess, "run", _boom)
    with pytest.raises(subprocess.TimeoutExpired):
        setup_mod._run_git(["init"], cwd=tmp_path)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/proposal_assist/test_phase0_transaction_boundaries.py -v > /tmp/t3.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/t3.txt
```

Expected: FAIL with `AttributeError: module … has no attribute '_run_git'`.

- [ ] **Step 3: Implement `_run_git`**

In `src/call_manager/proposal_assist/phases/setup.py`, below `STUCK_SETUP_AFTER`, add:

```python
# Every git command in M1 runs inside a database lock, so each one needs a real
# ceiling: subprocess.run without a timeout can block forever and no CI duration
# assertion can prevent that in production.
_GIT_TIMEOUT_SECONDS = 30.0


def _git_env() -> dict[str, str]:
    """Author identity plus a hermetic environment: no site/global config, no
    credential prompt. Combined with the -c flags in _run_git, nothing an
    operator configures can introduce a hook, a signing helper or an
    interactive prompt into M1's locked region."""
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "call-manager")
    env.setdefault("GIT_AUTHOR_EMAIL", "call-manager@local")
    env.setdefault("GIT_COMMITTER_NAME", "call-manager")
    env.setdefault("GIT_COMMITTER_EMAIL", "call-manager@local")
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run_git(
    args: list[str], *, cwd: Path, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run one git command hermetically and bounded. Raises
    subprocess.TimeoutExpired on overrun, which M1 turns into a rollback that
    releases both the mutex and the row lock."""
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=check,
        env=_git_env(),
        timeout=_GIT_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
    )
```

Then replace the four existing git invocations inside `_snapshot_and_init_repo` with `_run_git` calls, and delete the now-unused local `env = os.environ.copy()` block:

```python
        if not (repo_dir / ".git").is_dir():
            _run_git(["init", "-q", "-b", "main"], cwd=repo_dir)

        commit_count_proc = _run_git(
            ["rev-list", "--count", "HEAD"], cwd=repo_dir, check=False
        )
        has_initial_commit = (
            commit_count_proc.returncode == 0
            and commit_count_proc.stdout.strip() not in ("", "0")
        )
        if not has_initial_commit:
            for fname in self._PHASE_ARTIFACT_FILES:
                (repo_dir / fname).write_text("")
            _run_git(["add", "-A"], cwd=repo_dir)
            _run_git(["commit", "-q", "-m", "phase0: initialize empty artifacts"], cwd=repo_dir)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/proposal_assist/test_phase0_transaction_boundaries.py tests/proposal_assist/test_phase0_setup.py -v > /tmp/t3.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -40 /tmp/t3.txt
```

Expected: the two new tests PASS. `test_phase0_setup.py` may error at import because of Task 2's removed names; that is expected until Task 5.

- [ ] **Step 5: Commit**

```bash
uv run ruff check src/call_manager/proposal_assist/phases/setup.py
git add src/call_manager/proposal_assist/phases/setup.py tests/proposal_assist/test_phase0_transaction_boundaries.py
git commit -m "fix(phase0): run git hermetically with a real timeout"
```

---

### Task 4: Refuse to claim a deleted workspace's setup job

**Files:**
- Modify: `src/call_manager/proposal_assist/phases/setup.py` (`claim_next_setup_job`)
- Test: `tests/proposal_assist/test_phase0_retirement.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `claim_next_setup_job` keeps its signature `(*, session: Session) -> WorkspaceSetupJob | None` and now skips jobs whose workspace is soft-deleted.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_phase0_retirement.py`:

```python
from datetime import UTC, datetime

from call_manager.proposal_assist.phases.setup import claim_next_setup_job


def test_a_deleted_workspaces_job_is_never_claimed(pg_session_factory, current_user):
    with pg_session_factory() as s:
        live = make_workspace(s, owner=current_user)
        dead = make_workspace(s, owner=current_user)
        dead.soft_deleted_at = datetime.now(UTC)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=dead.id, status="queued"))
        s.commit()
        live_id = live.id

    # Only the deleted workspace has a queued job -> nothing claimable.
    with pg_session_factory() as s:
        assert claim_next_setup_job(session=s) is None
        s.commit()

    with pg_session_factory() as s:
        s.add(WorkspaceSetupJob(workspace_id=live_id, status="queued"))
        s.commit()

    with pg_session_factory() as s:
        job = claim_next_setup_job(session=s)
        assert job is not None
        assert job.workspace_id == live_id
        s.commit()
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/proposal_assist/test_phase0_retirement.py::test_a_deleted_workspaces_job_is_never_claimed -v > /tmp/t4.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/t4.txt
```

Expected: FAIL — the first `claim_next_setup_job` returns the deleted workspace's job instead of `None`.

- [ ] **Step 3: Add the live-workspace filter**

Replace the body of `claim_next_setup_job` in `src/call_manager/proposal_assist/phases/setup.py`:

```python
def claim_next_setup_job(*, session: Session) -> WorkspaceSetupJob | None:
    """Claim the oldest queued job under SELECT FOR UPDATE SKIP LOCKED.
    Sets status='running' and started_at. Caller commits.

    Jobs for soft-deleted workspaces are excluded in SQL, mirroring the four
    analysis-pass claim queries. This is the durable half of retirement: it
    survives a restart, needs no in-process tombstone, and holds even when the
    "jobs" hook could not reach the row."""
    live_ws = select(Workspace.id).where(Workspace.soft_deleted_at.is_(None))
    stmt = (
        select(WorkspaceSetupJob)
        .where(
            WorkspaceSetupJob.status == "queued",
            WorkspaceSetupJob.workspace_id.in_(live_ws),
        )
        .order_by(WorkspaceSetupJob.queued_at)  # FIFO
        .limit(1)
        .with_for_update(skip_locked=True, of=WorkspaceSetupJob)
    )
    job = session.scalar(stmt)
    if job is None:
        return None
    job.status = "running"
    job.started_at = datetime.now(UTC)
    return job
```

`of=WorkspaceSetupJob` is required: without it Postgres tries to lock the joined `workspaces` rows too, which both widens the lock and conflicts with `FOR UPDATE` on a subquery target.

- [ ] **Step 4: Run the test to verify it passes**

```bash
uv run pytest tests/proposal_assist/test_phase0_retirement.py -v > /tmp/t4.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/t4.txt
```

Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
uv run ruff check src/call_manager/proposal_assist/phases/setup.py
git add src/call_manager/proposal_assist/phases/setup.py tests/proposal_assist/test_phase0_retirement.py
git commit -m "feat(phase0): never claim a setup job for a soft-deleted workspace"
```

---

### Task 5: Phase0Worker on a session_factory, with M1 under the mutex

**Files:**
- Modify: `src/call_manager/proposal_assist/phases/setup.py` (`Phase0Worker`, `run_phase0_tick`)
- Test: `tests/proposal_assist/test_phase0_transaction_boundaries.py`, `tests/proposal_assist/test_phase0_setup.py`, `tests/proposal_assist/test_phase0_e2e.py`

**Interfaces:**
- Consumes: `_run_git` (Task 3); `claim_next_setup_job` (Task 4).
- Produces:
  - `Phase0Worker(session_factory: sessionmaker, client: _AnthropicClientLike, extractor_version: str, rubric_extractor_version: str, workspaces_dir: str)` — the `session` field is gone.
  - `Phase0Worker._m1(job_id: uuid.UUID, workspace_id: uuid.UUID) -> str` returning the pinned Call source hash.
  - `Phase0Worker._fail(job_id: uuid.UUID, exc: BaseException) -> None`.
  - `run_phase0_tick` keeps its signature; it no longer passes a `session`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/test_phase0_transaction_boundaries.py`:

```python
import threading
import time
import uuid
from datetime import UTC, datetime

from call_manager.db.models import Workspace, WorkspaceSetupJob
from call_manager.proposal_assist.drafting.locking import (
    CommitMutexTimeout,
    acquire_commit_mutex,
)
from call_manager.proposal_assist.phases.setup import Phase0Worker
from tests.proposal_assist.conftest import make_workspace


def _worker(pg_session_factory, tmp_path, client):
    return Phase0Worker(
        session_factory=pg_session_factory,
        client=client,
        extractor_version="v1",
        rubric_extractor_version="v1",
        workspaces_dir=str(tmp_path),
    )


def test_m1_creates_the_repo_and_pins_the_call_hash(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = WorkspaceSetupJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    pinned = _worker(pg_session_factory, tmp_path, phase0_client)._m1(job_id, ws_id)

    assert isinstance(pinned, str) and pinned
    assert (tmp_path / str(ws_id) / "repo" / ".git").is_dir()
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).snapshot_refreshed_at is not None


def test_m1_refuses_a_deleted_workspace_and_creates_nothing(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    """The headline gate: no post-delete repo creation."""
    from call_manager.proposal_assist.drafting.locking import WorkspaceDeleted

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        ws.soft_deleted_at = datetime.now(UTC)
        s.flush()
        job = WorkspaceSetupJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    with pytest.raises(WorkspaceDeleted):
        _worker(pg_session_factory, tmp_path, phase0_client)._m1(job_id, ws_id)

    assert not (tmp_path / str(ws_id)).exists()


def test_a_wedged_m1_makes_delete_time_out_at_the_mutex_not_the_row(
    pg_session_factory, current_user, tmp_path, phase0_client, monkeypatch
):
    """Gate 3b. The wedge is a FILESYSTEM call, not a subprocess: if it were a
    subprocess, _GIT_TIMEOUT_SECONDS could release the row before the mutex
    timeout and the result would depend on which timeout fired first."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="running"))
        s.commit()
        ws_id = ws.id
        job_id = s.scalars(
            select(WorkspaceSetupJob.id).where(WorkspaceSetupJob.workspace_id == ws_id)
        ).one()

    inside = threading.Event()
    release = threading.Event()
    real_mkdir = Path.mkdir

    def _blocking_mkdir(self, *a, **kw):
        real_mkdir(self, *a, **kw)
        if self.name == "repo":
            inside.set()
            release.wait(timeout=30)

    monkeypatch.setattr(Path, "mkdir", _blocking_mkdir)

    worker = _worker(pg_session_factory, tmp_path, phase0_client)
    t = threading.Thread(target=lambda: worker._m1(job_id, ws_id))
    t.start()
    try:
        assert inside.wait(timeout=10)
        started = time.monotonic()
        with pg_session_factory() as s2:
            with pytest.raises(CommitMutexTimeout):
                acquire_commit_mutex(s2, ws_id, timeout_seconds=2)
            s2.rollback()
        # It returned at the mutex timeout, not by hanging on the row lock.
        assert time.monotonic() - started < 10
    finally:
        release.set()
        t.join(timeout=30)
```

Add a `phase0_client` fixture at the top of the file (a deterministic stand-in for the Anthropic client, so no test needs a key):

```python
from dataclasses import dataclass

from call_manager.proposal_assist.extractors.sections import SECTION_NAMES


@dataclass
class _StubClient:
    model: str = "stub-model"

    def chat(self, *, messages, response_format=None):
        return {
            "sections": {name: f"{name} text" for name in SECTION_NAMES},
            "criteria": [{"name": "Excellence", "weight": 1}],
            "funder_notes": "notes",
        }


@pytest.fixture
def phase0_client():
    return _StubClient()
```

If `tests/proposal_assist/conftest.py`'s `make_workspace` has no `with_call` parameter, add one that creates and links a `Call` with `full_text="call body"`; every Phase-0 test needs it and `_snapshot_and_init_repo` raises without a `call_id`.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/proposal_assist/test_phase0_transaction_boundaries.py -v > /tmp/t5.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -40 /tmp/t5.txt
```

Expected: FAIL with `TypeError: Phase0Worker.__init__() got an unexpected keyword argument 'session_factory'`.

- [ ] **Step 3: Convert the worker and implement M1**

In `src/call_manager/proposal_assist/phases/setup.py`, add to the imports:

```python
import uuid

from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex_live
from call_manager.proposal_assist.hashing import source_text_hash
```

Replace the `Phase0Worker` dataclass fields and `run_one`, and add `_m1`:

```python
@dataclass
class Phase0Worker:
    """Single-threaded worker; one instance per process.

    Holds a session_factory, never a session: each milestone opens, uses and
    closes its own short transaction. The previous long-lived session is what
    made a row lock unsafe here — it outlived two LLM calls — so removing it is
    the point, not an incidental tidy-up.
    """

    session_factory: sessionmaker
    client: _AnthropicClientLike
    extractor_version: str
    rubric_extractor_version: str
    workspaces_dir: str

    def run_one(self) -> bool:
        """Claim one job and drive its three milestones. Returns True if a job
        was processed (success or failure), False if the queue was empty."""
        with self.session_factory() as s:
            job = claim_next_setup_job(session=s)
            if job is None:
                return False
            job_id = job.id
            workspace_id = job.workspace_id
            s.commit()

        try:
            pinned = self._m1(job_id, workspace_id)
            self._m2(job_id, workspace_id, pinned)
            self._m3(job_id, workspace_id, pinned)
        except Exception as exc:  # noqa: BLE001 — every outcome is recorded on the row
            self._fail(job_id, exc)
        return True

    def _m1(self, job_id: uuid.UUID, workspace_id: uuid.UUID) -> str:
        """Milestone 1: filesystem init + call/profile snapshots. Returns the
        pinned Call source hash for the rest of the run.

        Takes the commit mutex BEFORE the workspace row lock. That order is what
        bounds a concurrent DELETE: soft_delete_workspace_db takes the same mutex
        first, so a delete blocked behind a wedged M1 raises CommitMutexTimeout
        (the route's 429) instead of waiting on a row lock that has no timeout.
        acquire_commit_mutex_live also supplies M1's liveness check — it raises
        WorkspaceDeleted for an already-deleted workspace, before anything is
        created on disk.
        """
        with self.session_factory() as s:
            acquire_commit_mutex_live(s, workspace_id)
            ws = s.get(Workspace, workspace_id, with_for_update=True)
            if ws is None:
                raise RuntimeError(f"workspace {workspace_id} vanished")
            if ws.call_id is None:
                raise RuntimeError(f"workspace {ws.id} has no call_id — Phase 0 requires one")
            call = s.get(Call, ws.call_id)
            if call is None:
                raise RuntimeError(f"call {ws.call_id} not found")

            pinned = source_text_hash(call.full_text or "")

            self._init_repo(ws.id)

            ws.call_body_snapshot = (call.summary or "") + "\n\n" + (call.full_text or "")
            if ws.profile_id is not None:
                profile = s.get(Profile, ws.profile_id)
                if profile is not None:
                    ws.profile_desc_snapshot = profile.description or ""
            ws.snapshot_refreshed_at = datetime.now(UTC)
            s.commit()
            return pinned

    def _init_repo(self, workspace_id: uuid.UUID) -> None:
        """The filesystem half of M1, unchanged in behaviour and still
        recovery-safe: git init is skipped when .git exists, and the
        rev-list check finishes a crash between init and the first commit."""
        repo_dir = self._workspace_repo_dir(workspace_id)
        repo_dir.mkdir(parents=True, exist_ok=True)
        if not (repo_dir / ".git").is_dir():
            _run_git(["init", "-q", "-b", "main"], cwd=repo_dir)

        commit_count_proc = _run_git(
            ["rev-list", "--count", "HEAD"], cwd=repo_dir, check=False
        )
        has_initial_commit = (
            commit_count_proc.returncode == 0
            and commit_count_proc.stdout.strip() not in ("", "0")
        )
        if not has_initial_commit:
            for fname in self._PHASE_ARTIFACT_FILES:
                (repo_dir / fname).write_text("")
            _run_git(["add", "-A"], cwd=repo_dir)
            _run_git(
                ["commit", "-q", "-m", "phase0: initialize empty artifacts"], cwd=repo_dir
            )
```

Delete the old `_snapshot_and_init_repo` and `_run_steps`; `_m2` and `_m3` arrive in Tasks 6 and 7, and `_fail` in Task 8. To keep the tree importable between tasks, add these three temporary stubs now and replace them in their own tasks:

```python
    def _m2(self, job_id: uuid.UUID, workspace_id: uuid.UUID, pinned: str) -> None:
        raise NotImplementedError  # Task 6

    def _m3(self, job_id: uuid.UUID, workspace_id: uuid.UUID, pinned: str) -> None:
        raise NotImplementedError  # Task 7

    def _fail(self, job_id: uuid.UUID, exc: BaseException) -> None:
        raise NotImplementedError  # Task 8
```

Finally, update `run_phase0_tick` to build the worker from the factory. Replace the `Phase0Worker(...)` construction and the surrounding session use:

```python
    with session_factory() as session:
        requeue_stuck_setup_jobs(session)
        session.commit()
        has_work = session.scalar(
            select(WorkspaceSetupJob.id).where(WorkspaceSetupJob.status == "queued").limit(1)
        )
    if has_work is None:
        return

    worker = Phase0Worker(
        session_factory=session_factory,
        client=client_factory(),
        extractor_version=extractor_version,
        rubric_extractor_version=rubric_extractor_version,
        workspaces_dir=workspaces_dir,
    )
    worker.run_one()
```

- [ ] **Step 4: Run the tests to verify M1 passes**

```bash
uv run pytest tests/proposal_assist/test_phase0_transaction_boundaries.py -v > /tmp/t5.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -40 /tmp/t5.txt
```

Expected: PASS (5 passed).

- [ ] **Step 5: Prove the mutex gate is not vacuous**

Temporarily delete the `acquire_commit_mutex_live(s, workspace_id)` line from `_m1`, re-run `test_a_wedged_m1_makes_delete_time_out_at_the_mutex_not_the_row`, and confirm it FAILS (the `acquire_commit_mutex` call now succeeds immediately rather than raising `CommitMutexTimeout`, because nothing holds the mutex — the DELETE would instead block on the row lock in production). Restore the line and re-run to green.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src/call_manager/proposal_assist/phases/setup.py tests/proposal_assist/test_phase0_transaction_boundaries.py
git add src/call_manager/proposal_assist/phases/setup.py tests/proposal_assist/test_phase0_transaction_boundaries.py tests/proposal_assist/conftest.py
git commit -m "feat(phase0): per-milestone sessions and M1 under the commit mutex"
```

---

### Task 6: M2 — compute outside the transaction, persist under both locks

**Files:**
- Modify: `src/call_manager/proposal_assist/phases/setup.py`
- Test: `tests/proposal_assist/test_phase0_transaction_boundaries.py`

**Interfaces:**
- Consumes: `_m1` (Task 5); `section_extracts_are_fresh` / `compute_section_extracts` / `persist_section_extracts` (Task 2).
- Produces: `Phase0Worker._m2(job_id, workspace_id, pinned) -> None`; `CallSourceChanged(RuntimeError)`; `Phase0Worker._assert_live(job_id, workspace_id) -> None` used by M2 and M3.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/test_phase0_transaction_boundaries.py`:

```python
from call_manager.db.models import CallSectionExtract
from call_manager.proposal_assist.phases.setup import CallSourceChanged


def test_m2_snapshots_sections_onto_the_workspace(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)
    pinned = w._m1(job_id, ws_id)
    w._m2(job_id, ws_id, pinned)

    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        assert set(ws.section_extracts) == set(SECTION_NAMES)
        assert s.scalars(
            select(CallSectionExtract).where(CallSectionExtract.call_id == ws.call_id)
        ).all()


def test_m2_raises_when_the_call_changed_under_it(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)
    pinned = w._m1(job_id, ws_id)

    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        s.get(Call, ws.call_id).full_text = "re-ingested body"
        s.commit()

    with pytest.raises(CallSourceChanged):
        w._m2(job_id, ws_id, pinned)

    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).section_extracts in (None, {})


def test_m2_does_not_start_a_cloud_call_for_a_deleted_workspace(
    pg_session_factory, current_user, tmp_path
):
    """Pre-compute liveness: the delete lands after M1, before M2's LLM call."""
    calls = {"n": 0}

    @dataclass
    class _CountingClient:
        model: str = "stub-model"

        def chat(self, *, messages, response_format=None):
            calls["n"] += 1
            return _StubClient().chat(messages=messages)

    client = _CountingClient()
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, client)
    pinned = w._m1(job_id, ws_id)

    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    with pytest.raises(Exception):
        w._m2(job_id, ws_id, pinned)

    assert calls["n"] == 0


def test_m2_discards_a_payload_another_worker_made_redundant(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    """Two workspaces share a Call. If the cache went fresh while we computed,
    snapshot the canonical rows rather than overwriting them."""
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)
    pinned = w._m1(job_id, ws_id)

    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        call = s.get(Call, ws.call_id)
        persist_section_extracts(
            session=s,
            call=call,
            sections={n: f"canonical {n}" for n in SECTION_NAMES},
            model="other-worker",
            extractor_version="v1",
            source_hash=pinned,
        )
        s.commit()

    w._m2(job_id, ws_id, pinned)

    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        assert ws.section_extracts["background"] == "canonical background"
        rows = s.scalars(
            select(CallSectionExtract).where(CallSectionExtract.call_id == ws.call_id)
        ).all()
        assert {r.extracted_by_model for r in rows} == {"other-worker"}
```

Add the shared helper near `_worker`:

```python
def _queued_job(pg_session_factory, current_user):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = WorkspaceSetupJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        return job.id, ws.id
```

Adjust `background` in the assertion to whichever name is first in `SECTION_NAMES` if it differs.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/proposal_assist/test_phase0_transaction_boundaries.py -k m2 -v > /tmp/t6.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -40 /tmp/t6.txt
```

Expected: FAIL with `NotImplementedError` (and `ImportError` for `CallSourceChanged`).

- [ ] **Step 3: Implement `CallSourceChanged`, `_assert_live` and `_m2`**

Add near `MAX_SETUP_ATTEMPTS` in `setup.py`:

```python
class CallSourceChanged(RuntimeError):
    """The Call's text was re-ingested mid-run, so the milestones would otherwise
    commit snapshots taken from different revisions.

    Deliberately a typed failure rather than an internal retry loop: a Call being
    re-scraped repeatedly would spin forever. It goes through the normal failure
    path, consuming an attempt and dead-lettering at MAX_SETUP_ATTEMPTS."""


class WorkspaceGoneDuringSetup(RuntimeError):
    """The workspace was soft-deleted, or the job was cancelled, between
    milestones. Routed to a terminal status, never a requeue."""
```

Add to `Phase0Worker`, replacing the `_m2` stub:

```python
    def _assert_live(self, job_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        """Short, lock-free liveness read. Used BEFORE computing so a deleted
        workspace costs no cloud spend; the locked persist re-checks for the
        race that opens after this returns."""
        with self.session_factory() as s:
            deleted = s.scalar(
                select(Workspace.soft_deleted_at).where(Workspace.id == workspace_id)
            )
            status = s.scalar(
                select(WorkspaceSetupJob.status).where(WorkspaceSetupJob.id == job_id)
            )
        if deleted is not None or status != "running":
            raise WorkspaceGoneDuringSetup(
                f"workspace {workspace_id} deleted or job {job_id} no longer running"
            )

    def _m2(self, job_id: uuid.UUID, workspace_id: uuid.UUID, pinned: str) -> None:
        """Milestone 2: section extracts. The LLM call happens with NO
        transaction open; only the write is under lock."""
        self._assert_live(job_id, workspace_id)

        with self.session_factory() as s:
            ws = s.get(Workspace, workspace_id)
            call = s.get(Call, ws.call_id) if ws is not None else None
            if call is None:
                raise RuntimeError("workspace or call missing for section extracts")
            call_id = call.id
            call_text = call.full_text or ""
            fresh = section_extracts_are_fresh(
                session=s, call=call, extractor_version=self.extractor_version
            )

        sections = (
            None if fresh else compute_section_extracts(call_text=call_text, client=self.client)
        )

        with self.session_factory() as s:
            ws = s.get(Workspace, workspace_id, with_for_update=True)
            if ws is None or ws.soft_deleted_at is not None:
                raise WorkspaceGoneDuringSetup(f"workspace {workspace_id} deleted")
            # Workspace row THEN Call row — the normative order. Two workspaces
            # sharing a Call therefore queue rather than deadlock, and neither
            # inverts against the delete transaction's workspace-row update.
            call = s.get(Call, call_id, with_for_update=True)
            if call is None:
                raise RuntimeError(f"call {call_id} not found")
            if source_text_hash(call.full_text or "") != pinned:
                raise CallSourceChanged(
                    f"call {call_id} was re-ingested during setup of workspace {workspace_id}"
                )

            # Re-check under the lock: another worker may have made the cache
            # fresh while we computed. Snapshot the canonical rows instead of
            # overwriting them with our now-redundant payload.
            if sections is not None and not section_extracts_are_fresh(
                session=s, call=call, extractor_version=self.extractor_version
            ):
                persist_section_extracts(
                    session=s,
                    call=call,
                    sections=sections,
                    model=self.client.model,
                    extractor_version=self.extractor_version,
                    source_hash=pinned,
                )
                s.flush()

            rows = s.scalars(
                select(CallSectionExtract).where(CallSectionExtract.call_id == call_id)
            ).all()
            snapshot = {r.section: r.extract_text for r in rows}
            for name in SECTION_NAMES:
                snapshot.setdefault(name, "")
            ws.section_extracts = snapshot
            s.commit()
```

Update the module imports to pull the new extractor names:

```python
from call_manager.proposal_assist.extractors.refresh import (
    compute_rubric,
    compute_section_extracts,
    persist_rubric,
    persist_section_extracts,
    rubric_is_fresh,
    section_extracts_are_fresh,
)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/proposal_assist/test_phase0_transaction_boundaries.py -v > /tmp/t6.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -40 /tmp/t6.txt
```

Expected: PASS (9 passed).

- [ ] **Step 5: Prove the pre-compute check is not vacuous**

Temporarily remove the `self._assert_live(job_id, workspace_id)` line from `_m2`, re-run `test_m2_does_not_start_a_cloud_call_for_a_deleted_workspace`, confirm it FAILS on `calls["n"] == 0`. Restore and re-run to green.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src/call_manager/proposal_assist/phases/setup.py tests/proposal_assist/test_phase0_transaction_boundaries.py
git add src/call_manager/proposal_assist/phases/setup.py tests/proposal_assist/test_phase0_transaction_boundaries.py
git commit -m "feat(phase0): M2 computes outside the txn and persists under workspace+call locks"
```

---

### Task 7: M3 — rubric, audit event and terminal status in one transaction

**Files:**
- Modify: `src/call_manager/proposal_assist/phases/setup.py`
- Test: `tests/proposal_assist/test_phase0_transaction_boundaries.py`

**Interfaces:**
- Consumes: `_assert_live`, `CallSourceChanged`, `WorkspaceGoneDuringSetup` (Task 6); `rubric_is_fresh` / `compute_rubric` / `persist_rubric` (Task 2).
- Produces: `Phase0Worker._m3(job_id, workspace_id, pinned) -> None`, which is the only place the job reaches `waiting_for_rubric_review`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/test_phase0_transaction_boundaries.py`:

```python
from call_manager.db.models import WorkspaceEvent


def test_m3_writes_rubric_event_and_status_atomically(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)
    pinned = w._m1(job_id, ws_id)
    w._m2(job_id, ws_id, pinned)
    w._m3(job_id, ws_id, pinned)

    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        assert ws.rubric_criteria
        assert ws.rubric_updated_at is not None
        events = s.scalars(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "rubric_initialized",
            )
        ).all()
        assert len(events) == 1
        assert s.get(WorkspaceSetupJob, job_id).status == "waiting_for_rubric_review"


def test_a_cancel_beats_m3_and_is_not_overwritten(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    """The compare-and-set: a job cancelled while M3 computed must stay
    cancelled, not be resurrected into waiting_for_rubric_review."""
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)
    pinned = w._m1(job_id, ws_id)
    w._m2(job_id, ws_id, pinned)

    with pg_session_factory() as s:
        s.get(WorkspaceSetupJob, job_id).status = "cancelled"
        s.commit()

    with pytest.raises(WorkspaceGoneDuringSetup):
        w._m3(job_id, ws_id, pinned)

    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "cancelled"
        assert (
            s.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == ws_id,
                    WorkspaceEvent.kind == "rubric_initialized",
                )
            ).all()
            == []
        )


def test_m3_raises_when_the_call_changed_under_it(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)
    pinned = w._m1(job_id, ws_id)
    w._m2(job_id, ws_id, pinned)

    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        s.get(Call, ws.call_id).full_text = "re-ingested body"
        s.commit()

    with pytest.raises(CallSourceChanged):
        w._m3(job_id, ws_id, pinned)

    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "running"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/proposal_assist/test_phase0_transaction_boundaries.py -k m3 -v > /tmp/t7.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -40 /tmp/t7.txt
```

Expected: FAIL with `NotImplementedError`.

- [ ] **Step 3: Implement `_m3`**

Replace the `_m3` stub in `Phase0Worker`:

```python
    def _m3(self, job_id: uuid.UUID, workspace_id: uuid.UUID, pinned: str) -> None:
        """Milestone 3: rubric snapshot, the rubric_initialized audit event, and
        the job's terminal transition — all in ONE transaction.

        The status write lives here, not in a later step, for two reasons: a
        crash between them would re-run M3 and append a second audit event
        (which corrupts forward-replay of rubric edits), and a cancel landing in
        that gap would be overwritten by the success finalizer. The status write
        is a compare-and-set on 'running' so a concurrent cancel wins.
        """
        self._assert_live(job_id, workspace_id)

        with self.session_factory() as s:
            ws = s.get(Workspace, workspace_id)
            call = s.get(Call, ws.call_id) if ws is not None else None
            if call is None:
                raise RuntimeError("workspace or call missing for rubric")
            call_id = call.id
            call_text = call.full_text or ""
            fresh = rubric_is_fresh(
                session=s, call=call, extractor_version=self.rubric_extractor_version
            )

        payload = None if fresh else compute_rubric(call_text=call_text, client=self.client)

        with self.session_factory() as s:
            ws = s.get(Workspace, workspace_id, with_for_update=True)
            if ws is None or ws.soft_deleted_at is not None:
                raise WorkspaceGoneDuringSetup(f"workspace {workspace_id} deleted")
            call = s.get(Call, call_id, with_for_update=True)
            if call is None:
                raise RuntimeError(f"call {call_id} not found")
            if source_text_hash(call.full_text or "") != pinned:
                raise CallSourceChanged(
                    f"call {call_id} was re-ingested during setup of workspace {workspace_id}"
                )

            job = s.get(WorkspaceSetupJob, job_id, with_for_update=True)
            if job is None or job.status != "running":
                raise WorkspaceGoneDuringSetup(
                    f"job {job_id} is no longer running (a cancel won)"
                )

            if payload is not None and not rubric_is_fresh(
                session=s, call=call, extractor_version=self.rubric_extractor_version
            ):
                persist_rubric(
                    session=s,
                    call=call,
                    payload=payload,
                    model=self.client.model,
                    extractor_version=self.rubric_extractor_version,
                    source_hash=pinned,
                )
                s.flush()

            template = s.get(CallRubric, call_id)
            if template is None:
                raise RuntimeError(f"rubric template for call {call_id} not found after fresh")

            ws.rubric_criteria = template.criteria
            ws.rubric_funder_notes = template.funder_notes
            ws.rubric_updated_at = datetime.now(UTC)

            s.add(
                WorkspaceEvent(
                    workspace_id=ws.id,
                    kind="rubric_initialized",
                    payload={
                        "criteria": template.criteria,
                        "funder_notes": template.funder_notes,
                        "seeded_from_call_rubrics_extracted_at": (
                            template.extracted_at.isoformat()
                        ),
                    },
                )
            )

            # Yield to the owner's rubric review. No completed_at — not terminal.
            job.status = "waiting_for_rubric_review"
            s.commit()
```

Remove the now-dead `_ensure_section_extracts_snapshot` and `_ensure_rubric_snapshot` methods.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/proposal_assist/test_phase0_transaction_boundaries.py -v > /tmp/t7.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -40 /tmp/t7.txt
```

Expected: PASS (12 passed).

- [ ] **Step 5: Prove the atomicity gate is not vacuous**

Temporarily move `job.status = "waiting_for_rubric_review"` out of `_m3` into a separate session opened after `s.commit()`, re-run `test_a_cancel_beats_m3_and_is_not_overwritten`, and confirm it FAILS (the status becomes `waiting_for_rubric_review`). Restore and re-run to green.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src/call_manager/proposal_assist/phases/setup.py tests/proposal_assist/test_phase0_transaction_boundaries.py
git add src/call_manager/proposal_assist/phases/setup.py tests/proposal_assist/test_phase0_transaction_boundaries.py
git commit -m "feat(phase0): M3 writes rubric, audit event and status atomically"
```

---

### Task 8: The failure path — terminal for cancelled, bounded retry for everything else

**Files:**
- Modify: `src/call_manager/proposal_assist/phases/setup.py` (`Phase0Worker._fail`)
- Test: `tests/proposal_assist/test_phase0_transaction_boundaries.py`

**Interfaces:**
- Consumes: `CallSourceChanged`, `WorkspaceGoneDuringSetup` (Task 6).
- Produces: `Phase0Worker._fail(job_id, exc)` — the single place a job leaves `running` on any error path.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/test_phase0_transaction_boundaries.py`:

```python
def test_a_changed_call_consumes_attempts_and_dead_letters(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    """Gate 5b: bounded retry, not a spin. Attempts 1-4 requeue, 5 dead-letters."""
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)

    for expected_attempt in range(1, 5):
        w._fail(job_id, CallSourceChanged("call re-ingested"))
        with pg_session_factory() as s:
            job = s.get(WorkspaceSetupJob, job_id)
            assert job.attempts == expected_attempt
            assert job.status == "queued"
            assert job.started_at is None
            assert "re-ingested" in job.last_error
        with pg_session_factory() as s:
            s.get(WorkspaceSetupJob, job_id).status = "running"
            s.commit()

    w._fail(job_id, CallSourceChanged("call re-ingested"))
    with pg_session_factory() as s:
        job = s.get(WorkspaceSetupJob, job_id)
        assert job.attempts == 5
        assert job.status == "failed"
        assert job.completed_at is not None


def test_a_deleted_workspace_takes_the_terminal_path_not_the_retry_path(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    _worker(pg_session_factory, tmp_path, phase0_client)._fail(
        job_id, WorkspaceGoneDuringSetup("deleted")
    )

    with pg_session_factory() as s:
        job = s.get(WorkspaceSetupJob, job_id)
        assert job.status == "cancelled"
        assert job.completed_at is not None


def test_fail_never_resurrects_an_already_terminal_job(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    with pg_session_factory() as s:
        s.get(WorkspaceSetupJob, job_id).status = "cancelled"
        s.commit()

    _worker(pg_session_factory, tmp_path, phase0_client)._fail(job_id, RuntimeError("boom"))

    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "cancelled"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/proposal_assist/test_phase0_transaction_boundaries.py -k "attempts or terminal or resurrect" -v > /tmp/t8.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -40 /tmp/t8.txt
```

Expected: FAIL with `NotImplementedError`.

- [ ] **Step 3: Implement `_fail`**

Replace the `_fail` stub in `Phase0Worker`:

```python
    def _fail(self, job_id: uuid.UUID, exc: BaseException) -> None:
        """Record the outcome of a failed run in its own short transaction.

        Order matters: a job that is already terminal is left alone, and a
        deleted/cancelled job goes terminal rather than back to 'queued'. The
        old code requeued unconditionally while attempts < MAX, which would send
        a cancelled job straight back into the queue.
        """
        now = datetime.now(UTC)
        with self.session_factory() as s:
            job = s.get(WorkspaceSetupJob, job_id, with_for_update=True)
            if job is None:
                logger.error("phase0 setup job %s vanished before failure write", job_id)
                return
            if job.status in ("cancelled", "failed", "done", "waiting_for_rubric_review"):
                return  # already terminal (or already yielded) — never resurrect

            deleted = s.scalar(
                select(Workspace.soft_deleted_at).where(Workspace.id == job.workspace_id)
            )

            job.attempts += 1
            job.last_error = f"{type(exc).__name__}: {exc}"

            if deleted is not None or isinstance(exc, WorkspaceGoneDuringSetup):
                job.status = "cancelled"
                job.completed_at = now
                logger.info(
                    "phase0.cancelled",
                    extra={
                        "event": "phase0_cancelled",
                        "job_id": str(job_id),
                        "workspace_id": str(job.workspace_id),
                    },
                )
            elif job.attempts >= MAX_SETUP_ATTEMPTS:
                job.status = "failed"
                job.completed_at = now
                logger.exception(
                    "phase0 setup job %s failed permanently after %d attempts",
                    job_id,
                    job.attempts,
                )
            else:
                job.status = "queued"
                job.started_at = None
                logger.warning(
                    "phase0 setup job %s transient failure (attempt %d/%d): %s",
                    job_id,
                    job.attempts,
                    MAX_SETUP_ATTEMPTS,
                    exc,
                )
            s.commit()
```

- [ ] **Step 4: Run the full Phase-0 suite**

```bash
uv run pytest tests/proposal_assist/test_phase0_transaction_boundaries.py tests/proposal_assist/test_phase0_setup.py tests/proposal_assist/test_phase0_e2e.py tests/proposal_assist/test_phase0_scheduler_tick.py tests/proposal_assist/test_setup_retry.py -v > /tmp/t8.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -60 /tmp/t8.txt
```

Expected: PASS. The pre-existing Phase-0 tests construct `Phase0Worker(session=…)` and monkeypatch `_run_steps`; update each to `session_factory=…` and to patch `_m1`/`_m2`/`_m3` instead. Keep every behaviour they assert — do not delete a test because its seam moved.

- [ ] **Step 5: Prove the requeue gate is not vacuous**

Temporarily change the `deleted is not None or isinstance(...)` branch to fall through to the `else`, re-run `test_a_deleted_workspace_takes_the_terminal_path_not_the_retry_path`, and confirm it FAILS with `status == "queued"`. Restore and re-run to green.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src/call_manager/proposal_assist/phases/setup.py tests/
git add -u src/call_manager/proposal_assist/phases/setup.py tests/proposal_assist/
git commit -m "feat(phase0): terminal failure path for cancelled jobs, bounded retry otherwise"
```

---

### Task 9: The `"jobs"` retirement hook

**Files:**
- Create: `src/call_manager/proposal_assist/jobs_retirement.py`
- Modify: `src/call_manager/proposal_assist/retirement.py`, `src/call_manager/main.py`
- Test: `tests/proposal_assist/test_phase0_retirement.py`, `tests/proposal_assist/test_retirement_coordinator.py`

**Interfaces:**
- Consumes: the `cancelled` status (Task 1).
- Produces:
  - `_JOBS_CANCEL_SECONDS: float = 1.0`
  - `cancel_setup_jobs_for_workspace(session: Session, workspace_id: uuid.UUID) -> int` — SKIP LOCKED, caller commits.
  - `build_jobs_retirement_hook(session_factory) -> Callable[[uuid.UUID], Awaitable[int]]`
  - `REQUIRED_HOOKS == ("turns", "collab", "jobs")`

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/test_phase0_retirement.py`:

```python
import asyncio

from call_manager.proposal_assist.jobs_retirement import (
    _JOBS_CANCEL_SECONDS,
    build_jobs_retirement_hook,
    cancel_setup_jobs_for_workspace,
)


def test_the_hook_cancels_queued_and_running_setup_jobs(pg_session_factory, current_user):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="running"))
        s.commit()
        ws_id = ws.id

    hook = build_jobs_retirement_hook(pg_session_factory)
    assert asyncio.run(hook(ws_id)) == 1

    with pg_session_factory() as s:
        job = s.scalars(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id)
        ).one()
        assert job.status == "cancelled"
        assert job.completed_at is not None


def test_a_locked_row_is_skipped_not_waited_on(pg_session_factory, current_user):
    """SKIP LOCKED is what bounds the hook: without it the hook inherits the
    worker's transaction lifetime and the DELETE route blocks behind it."""
    import threading

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="running"))
        s.commit()
        ws_id = ws.id

    holding = threading.Event()
    release = threading.Event()

    def _hold():
        with pg_session_factory() as s:
            s.scalars(
                select(WorkspaceSetupJob)
                .where(WorkspaceSetupJob.workspace_id == ws_id)
                .with_for_update()
            ).one()
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    t = threading.Thread(target=_hold)
    t.start()
    try:
        assert holding.wait(timeout=5)
        started = time.monotonic()
        assert asyncio.run(build_jobs_retirement_hook(pg_session_factory)(ws_id)) == 0
        assert time.monotonic() - started < _JOBS_CANCEL_SECONDS + 1
    finally:
        release.set()
        t.join(timeout=30)


def test_the_hook_is_bounded_by_wait_for(pg_session_factory, current_user, monkeypatch):
    """Declaring a constant bounds nothing; the wait_for is the bound."""
    import call_manager.proposal_assist.jobs_retirement as jr

    def _never_returns(session, workspace_id):
        time.sleep(30)
        return 0

    monkeypatch.setattr(jr, "cancel_setup_jobs_for_workspace", _never_returns)
    monkeypatch.setattr(jr, "_JOBS_CANCEL_SECONDS", 0.2)

    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(jr.build_jobs_retirement_hook(pg_session_factory)(uuid.uuid4()))
    assert time.monotonic() - started < 5
```

And in `tests/proposal_assist/test_retirement_coordinator.py`, extend the two existing assertions:

```python
def test_jobs_is_a_required_hook():
    from call_manager.proposal_assist import retirement

    assert "jobs" in retirement.REQUIRED_HOOKS
    with pytest.raises(retirement.MissingRetirementHook):
        retirement.WorkspaceRetirement(
            hooks={"turns": _noop, "collab": _noop}  # no "jobs"
        )
```

In `test_the_barrier_exceeds_every_hooks_own_worst_case`, add the jobs bound to the `max(...)`:

```python
    from call_manager.proposal_assist.jobs_retirement import _JOBS_CANCEL_SECONDS

    jobs_worst = _JOBS_CANCEL_SECONDS
    assert max(collab_worst, turns_worst, jobs_worst) < retirement._RETIREMENT_BARRIER_SECONDS, (
        f"barrier {retirement._RETIREMENT_BARRIER_SECONDS}s does not cover "
        f"collab {collab_worst}s / turns {turns_worst}s / jobs {jobs_worst}s"
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/proposal_assist/test_phase0_retirement.py tests/proposal_assist/test_retirement_coordinator.py -v > /tmp/t9.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -40 /tmp/t9.txt
```

Expected: FAIL with `ModuleNotFoundError: … jobs_retirement`.

- [ ] **Step 3: Create the module**

Create `src/call_manager/proposal_assist/jobs_retirement.py`:

```python
"""The "jobs" retirement hook: terminalize a deleted workspace's job rows.

A neutral home so main.py and scheduler.py can both reach it without importing
the Phase-0 worker. This slice handles setup jobs; workspace-retirement Slice C
extends BOTH the hook and reconcile_cancelled_jobs to claim-analysis.

Why after the delete commits rather than inside its transaction: a worker's
heartbeat UPDATE holds its job row for the whole per-job transaction, so a
cancel inside the delete transaction would block on that row while holding the
uncommitted workspace deletion — stalling DELETE and hiding the deletion from
the worker's own liveness re-read.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from call_manager.db.models import WorkspaceSetupJob

logger = logging.getLogger(__name__)

# The hook's ceiling. Enforced with asyncio.wait_for in build_jobs_retirement_hook —
# a constant on its own bounds nothing, least of all a to_thread call. Every query
# below uses SKIP LOCKED, so this is a timeout for pathology, not for waiting.
_JOBS_CANCEL_SECONDS = 1.0

_CANCELLABLE = ("queued", "running")


def cancel_setup_jobs_for_workspace(session: Session, workspace_id: uuid.UUID) -> int:
    """Mark this workspace's active setup jobs 'cancelled'. Caller commits.

    SKIP LOCKED: a row a live worker holds is SKIPPED, never waited on, so this
    can never inherit that worker's transaction lifetime. Skipped rows converge
    via reconcile_cancelled_jobs.
    """
    ids = session.scalars(
        select(WorkspaceSetupJob.id)
        .where(
            WorkspaceSetupJob.workspace_id == workspace_id,
            WorkspaceSetupJob.status.in_(_CANCELLABLE),
        )
        .with_for_update(skip_locked=True)
    ).all()
    if not ids:
        return 0
    session.execute(
        update(WorkspaceSetupJob)
        .where(WorkspaceSetupJob.id.in_(ids))
        .values(
            status="cancelled",
            completed_at=datetime.now(UTC),
            last_error="cancelled: workspace deleted",
        )
    )
    return len(ids)


def build_jobs_retirement_hook(
    session_factory: sessionmaker,
) -> Callable[[uuid.UUID], Awaitable[int]]:
    """Build the coordinator hook. The coordinator is async and these are sync DB
    writes, so the work runs in a thread — bounded by wait_for."""

    async def _hook(workspace_id: uuid.UUID) -> int:
        def _work() -> int:
            with session_factory() as session:
                n = cancel_setup_jobs_for_workspace(session, workspace_id)
                session.commit()
                return n

        return await asyncio.wait_for(
            asyncio.to_thread(_work), timeout=_JOBS_CANCEL_SECONDS
        )

    return _hook
```

Note the monkeypatch in the third test replaces the module attribute, so `_hook` must resolve `cancel_setup_jobs_for_workspace` and `_JOBS_CANCEL_SECONDS` through the module at call time. Reference them as module globals (as written above) — do not bind them to locals at build time.

- [ ] **Step 4: Register the hook**

In `src/call_manager/proposal_assist/retirement.py`:

```python
REQUIRED_HOOKS = ("turns", "collab", "jobs")
```

and extend the budget comment above `_RETIREMENT_BARRIER_SECONDS` with a third line:

```python
#   jobs:   _JOBS_CANCEL_SECONDS (1.0), and every query is SKIP LOCKED so it
#           never waits on a live worker's row = 1.0
```

In `src/call_manager/main.py`, extend the hooks dict:

```python
    from call_manager.proposal_assist.collab.room import MANAGER as _COLLAB_MANAGER
    from call_manager.proposal_assist.jobs_retirement import build_jobs_retirement_hook
    from call_manager.proposal_assist.retirement import WorkspaceRetirement

    app.state.workspace_retirement = WorkspaceRetirement(
        hooks={
            "turns": workspace_controller.registry.retire_workspace,
            "collab": _COLLAB_MANAGER.retire_workspace,
            "jobs": build_jobs_retirement_hook(app.state.session_factory),
        }
    )
```

If `app.state.session_factory` is not yet assigned at that point in the lifespan, use the same local the surrounding code uses to build other components.

- [ ] **Step 5: Write the end-to-end wiring test**

Append to `tests/proposal_assist/test_phase0_retirement.py`:

```python
def test_delete_reaches_the_jobs_hook(client_authed_owner, current_user, pg_session_factory):
    """Layer tests do not prove wiring: replacing main.py's "jobs" hook with a
    no-op must fail HERE. This is the mutation that went undetected for turns."""
    from call_manager.proposal_assist.collab.room import MANAGER

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="running"))
        s.commit()
        ws_id = ws.id

    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204
    try:
        with pg_session_factory() as s:
            job = s.scalars(
                select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id)
            ).one()
            assert job.status == "cancelled"
    finally:
        MANAGER._retired_ws.discard(ws_id)  # process-global; release even on failure
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
uv run pytest tests/proposal_assist/test_phase0_retirement.py tests/proposal_assist/test_retirement_coordinator.py -v > /tmp/t9.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -40 /tmp/t9.txt
```

Expected: PASS.

- [ ] **Step 7: Prove the wiring gate is not vacuous**

Temporarily replace `"jobs": build_jobs_retirement_hook(...)` in `main.py` with an async no-op lambda, re-run `test_delete_reaches_the_jobs_hook`, and confirm it FAILS. Restore and re-run to green.

- [ ] **Step 8: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/call_manager/proposal_assist/jobs_retirement.py src/call_manager/proposal_assist/retirement.py src/call_manager/main.py tests/proposal_assist/
git commit -m "feat(retirement): add the required jobs hook, bounded and SKIP LOCKED"
```

---

### Task 10: The reconciliation tick, scheduled independently

**Files:**
- Modify: `src/call_manager/proposal_assist/jobs_retirement.py`, `src/call_manager/config.py`, `src/call_manager/scheduler.py`
- Test: `tests/proposal_assist/test_phase0_retirement.py`

**Interfaces:**
- Consumes: `cancel_setup_jobs_for_workspace` (Task 9).
- Produces: `reconcile_cancelled_jobs(session: Session) -> int` (caller commits); config field `jobs_reconcile_tick_seconds`; APScheduler job id `jobs_retirement_reconcile_tick`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/test_phase0_retirement.py`:

```python
def test_a_row_the_hook_skipped_is_converged_by_the_tick(pg_session_factory, current_user):
    """Full path: the hook skips a locked row, the worker dies WITHOUT
    terminalizing it (so the cooperative check cannot be what saves us), and a
    later tick cancels it."""
    import threading

    from call_manager.proposal_assist.jobs_retirement import reconcile_cancelled_jobs

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.soft_deleted_at = datetime.now(UTC)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="running"))
        s.commit()
        ws_id = ws.id

    holding = threading.Event()
    release = threading.Event()

    def _hold():
        with pg_session_factory() as s:
            s.scalars(
                select(WorkspaceSetupJob)
                .where(WorkspaceSetupJob.workspace_id == ws_id)
                .with_for_update()
            ).one()
            holding.set()
            release.wait(timeout=30)
            s.rollback()  # worker dies without terminalizing

    t = threading.Thread(target=_hold)
    t.start()
    try:
        assert holding.wait(timeout=5)
        assert asyncio.run(build_jobs_retirement_hook(pg_session_factory)(ws_id)) == 0
    finally:
        release.set()
        t.join(timeout=30)

    with pg_session_factory() as s:
        assert s.scalars(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id)
        ).one().status == "running"

    with pg_session_factory() as s:
        assert reconcile_cancelled_jobs(s) == 1
        s.commit()

    with pg_session_factory() as s:
        assert s.scalars(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id)
        ).one().status == "cancelled"


def test_reconcile_leaves_live_workspaces_alone(pg_session_factory, current_user):
    from call_manager.proposal_assist.jobs_retirement import reconcile_cancelled_jobs

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="running"))
        s.commit()

    with pg_session_factory() as s:
        assert reconcile_cancelled_jobs(s) == 0
        s.commit()


def test_the_reconcile_tick_is_registered_without_an_llm_client(monkeypatch, pg_session_factory):
    """A deployment with drafting off must still converge skipped cancellations.
    The Phase-0 tick returns early when DRAFTING_ENABLED is false or the key is
    unset, so reconciliation must not live behind it."""
    from call_manager.config import Settings
    from call_manager.scheduler import build_scheduler

    settings = Settings(DRAFTING_ENABLED=False, ANTHROPIC_API_KEY=None)
    sched = build_scheduler(
        settings=settings, session_factory=pg_session_factory, start=False
    )
    ids = {j.id for j in sched.get_jobs()}
    assert "jobs_retirement_reconcile_tick" in ids
```

If `build_scheduler`'s signature in this codebase differs, construct it exactly as `tests/proposal_assist/test_phase0_scheduler_tick.py` already does and keep the assertion.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/proposal_assist/test_phase0_retirement.py -k "reconcile or tick" -v > /tmp/t10.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -40 /tmp/t10.txt
```

Expected: FAIL with `ImportError: cannot import name 'reconcile_cancelled_jobs'`.

- [ ] **Step 3: Implement the reconciliation pass**

Append to `src/call_manager/proposal_assist/jobs_retirement.py`:

```python
def reconcile_cancelled_jobs(session: Session) -> int:
    """Terminalize active job rows belonging to soft-deleted workspaces.

    Convergence for rows the hook SKIP LOCKED past. Deliberately owns no LLM
    client and is scheduled independently of the Phase-0 tick, which returns
    early when DRAFTING_ENABLED is false or ANTHROPIC_API_KEY is unset — hiding
    reconciliation behind that path would strand every skipped cancellation on a
    deployment with drafting off.

    Slice C extends this to ClaimAnalysisJob (terminalizing to 'failed', which
    that table's CheckConstraint already allows).

    Returns rows touched. Caller commits.
    """
    from call_manager.db.models import Workspace

    dead_ws = select(Workspace.id).where(Workspace.soft_deleted_at.is_not(None))
    ids = session.scalars(
        select(WorkspaceSetupJob.id)
        .where(
            WorkspaceSetupJob.status.in_(_CANCELLABLE),
            WorkspaceSetupJob.workspace_id.in_(dead_ws),
        )
        .with_for_update(skip_locked=True)
    ).all()
    if not ids:
        return 0
    session.execute(
        update(WorkspaceSetupJob)
        .where(WorkspaceSetupJob.id.in_(ids))
        .values(
            status="cancelled",
            completed_at=datetime.now(UTC),
            last_error="cancelled: workspace deleted (reconciled)",
        )
    )
    logger.info(
        "jobs_retirement.reconciled",
        extra={"event": "jobs_retirement_reconciled", "rows": len(ids)},
    )
    return len(ids)
```

- [ ] **Step 4: Add the setting**

In `src/call_manager/config.py`, beside `phase0_tick_seconds`:

```python
    jobs_reconcile_tick_seconds: int = Field(
        default=60,
        alias="JOBS_RECONCILE_TICK_SECONDS",
        description=(
            "How often to converge job rows the retirement hook skipped because a "
            "live worker held them. Independent of DRAFTING_ENABLED — a deployment "
            "with drafting off must still terminalize a deleted workspace's jobs."
        ),
    )
```

- [ ] **Step 5: Register the tick**

In `src/call_manager/scheduler.py`, directly after the `sched.add_job(_phase0_tick, …)` block:

```python
    # Jobs-retirement reconciliation. NOT gated on drafting_enabled or the
    # Anthropic key: it needs no LLM client, and rows the "jobs" hook skipped
    # would otherwise never converge on a deployment with drafting off.
    def _jobs_reconcile_tick() -> None:
        from call_manager.proposal_assist.jobs_retirement import reconcile_cancelled_jobs

        with session_factory() as session:
            n = reconcile_cancelled_jobs(session)
            session.commit()
        if n:
            _logger.info("jobs retirement reconcile: %d row(s)", n)

    sched.add_job(
        _jobs_reconcile_tick,
        trigger=IntervalTrigger(seconds=settings.jobs_reconcile_tick_seconds),
        id="jobs_retirement_reconcile_tick",
        max_instances=1,
        coalesce=True,
    )
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
uv run pytest tests/proposal_assist/test_phase0_retirement.py -v > /tmp/t10.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -40 /tmp/t10.txt
```

Expected: PASS.

- [ ] **Step 7: Prove the independence gate is not vacuous**

Temporarily move the `sched.add_job(_jobs_reconcile_tick, …)` call inside `_phase0_tick`'s early-return guards (i.e. register it only when `settings.drafting_enabled and settings.anthropic_api_key`), re-run `test_the_reconcile_tick_is_registered_without_an_llm_client`, and confirm it FAILS. Restore and re-run to green.

- [ ] **Step 8: Run the whole proposal-assist suite**

```bash
uv run pytest tests/proposal_assist -q > /tmp/t10full.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -40 /tmp/t10full.txt
```

Expected: PASS, no failures. Investigate anything red before committing — do not assume "pre-existing".

- [ ] **Step 9: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/call_manager/proposal_assist/jobs_retirement.py src/call_manager/config.py src/call_manager/scheduler.py tests/proposal_assist/test_phase0_retirement.py
git commit -m "feat(retirement): reconcile skipped job cancellations on an independent tick"
```

---

### Task 11: The two gates the milestone tasks cannot carry

Spec gates 2 and 15 span the whole worker, so they belong to neither M1 nor M3. Gate 2 must be measured at the **route**, not at the mutex helper — "the delete transaction committed" is not the same claim as "the DELETE request returned".

**Files:**
- Test: `tests/proposal_assist/test_phase0_transaction_boundaries.py`

**Interfaces:**
- Consumes: `Phase0Worker` and all three milestones (Tasks 5–7); the `"jobs"` hook wiring (Task 9).
- Produces: no production code. Tests only.

- [ ] **Step 1: Write the route-latency test**

Append to `tests/proposal_assist/test_phase0_transaction_boundaries.py`:

```python
def test_delete_route_returns_promptly_while_phase0_runs_its_extractors(
    client_authed_owner, current_user, pg_session_factory, tmp_path, monkeypatch
):
    """Spec gate 2, measured as ROUTE wall-clock. A slow extractor must not be
    on the DELETE route's critical path: M2/M3 compute with no transaction open
    and hold no lock while the LLM call runs."""
    from call_manager.proposal_assist.collab.room import MANAGER

    started = threading.Event()
    release = threading.Event()

    @dataclass
    class _SlowClient:
        model: str = "slow"

        def chat(self, *, messages, response_format=None):
            started.set()
            release.wait(timeout=30)
            return _StubClient().chat(messages=messages)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = WorkspaceSetupJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    w = _worker(pg_session_factory, tmp_path, _SlowClient())
    pinned = w._m1(job_id, ws_id)
    t = threading.Thread(target=lambda: w._m2(job_id, ws_id, pinned))
    t.start()
    try:
        assert started.wait(timeout=10)          # the LLM call is in flight
        began = time.monotonic()
        resp = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
        elapsed = time.monotonic() - began
        assert resp.status_code == 204
        assert elapsed < 6.0, f"DELETE took {elapsed:.1f}s during a Phase-0 extractor call"
    finally:
        release.set()
        t.join(timeout=30)
        MANAGER._retired_ws.discard(ws_id)
```

- [ ] **Step 2: Write the recovery tests**

```python
def test_recovery_after_a_crash_between_m1_and_m2(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    """Spec gate 15. M1 committed, then the process died. Re-running from M1 must
    converge: git init is skipped, the initial commit is detected, and no work is
    duplicated."""
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)

    pinned_first = w._m1(job_id, ws_id)
    head_before = _run_git_head(tmp_path / str(ws_id) / "repo")

    pinned_again = w._m1(job_id, ws_id)          # the re-run
    assert pinned_again == pinned_first
    assert _run_git_head(tmp_path / str(ws_id) / "repo") == head_before

    w._m2(job_id, ws_id, pinned_again)
    w._m3(job_id, ws_id, pinned_again)
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "waiting_for_rubric_review"


def test_recovery_after_a_crash_between_m2_and_m3_makes_no_second_llm_call(
    pg_session_factory, current_user, tmp_path
):
    """M2 committed, then the process died. The re-run must skip the LLM entirely
    (the (hash, version) guard) and still produce exactly one audit event."""
    calls = {"n": 0}

    @dataclass
    class _CountingClient:
        model: str = "stub-model"

        def chat(self, *, messages, response_format=None):
            calls["n"] += 1
            return _StubClient().chat(messages=messages)

    client = _CountingClient()
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, client)
    pinned = w._m1(job_id, ws_id)
    w._m2(job_id, ws_id, pinned)
    after_m2 = calls["n"]
    assert after_m2 > 0

    w._m2(job_id, ws_id, pinned)                 # the re-run
    assert calls["n"] == after_m2                # no second cloud call

    w._m3(job_id, ws_id, pinned)
    with pg_session_factory() as s:
        events = s.scalars(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "rubric_initialized",
            )
        ).all()
        assert len(events) == 1
```

Add the helper used above near `_queued_job`:

```python
def _run_git_head(repo_dir: Path) -> str:
    return setup_mod._run_git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()
```

- [ ] **Step 3: Run the tests**

```bash
uv run pytest tests/proposal_assist/test_phase0_transaction_boundaries.py -v > /tmp/t11.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -60 /tmp/t11.txt
```

Expected: PASS.

- [ ] **Step 4: Prove the route-latency gate is not vacuous**

Temporarily move `_m2`'s `compute_section_extracts(...)` call inside the persist transaction (so the LLM runs while the workspace row is locked), re-run `test_delete_route_returns_promptly_while_phase0_runs_its_extractors`, and confirm it FAILS on the elapsed-time assertion. Restore and re-run to green.

- [ ] **Step 5: Commit**

```bash
uv run ruff check tests/proposal_assist/test_phase0_transaction_boundaries.py
git add tests/proposal_assist/test_phase0_transaction_boundaries.py
git commit -m "test(phase0): route-latency and per-milestone recovery gates"
```

---

## Final verification

- [ ] **Full suite**

```bash
uv run pytest -q > /tmp/full.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -60 /tmp/full.txt
```

The whole-suite run is known to be flaky under load (testcontainers Postgres drops connections); a `connection refused` is infrastructure, not a regression. Re-run any failure in isolation before treating it as real.

- [ ] **Lint and format**

```bash
uv run ruff check . && uv run ruff format --check .
```

- [ ] **Migration round-trip against a real database**

```bash
uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head
```

Expected: all three succeed.

- [ ] **OpenAPI drift** (no API shape changed here, so this must be a no-op)

```bash
EMBEDDING_MODEL=bge-m3 ./scripts/gen_openapi_types.sh && git diff --exit-code frontend/src/api/schema.gen.ts
```

Expected: exit 0, no diff.

---

## Spec coverage

| Spec section | Task |
|---|---|
| §4 M1 lock, mutex ordering, hermetic git, timeouts | 3, 5 |
| §5 compute/persist split | 2 |
| §6 Call pinning, workspace-then-Call lock order, stale-payload discard | 6, 7 |
| §6.1 `CallSourceChanged` bounded retry | 6, 8 |
| §7 M3 atomic with status; unique-index backstop | 1, 7 |
| §8 pre-compute liveness | 6, 7 |
| §9 `cancelled` status, migration, downgrade conversion, claim filter, failure path | 1, 4, 8 |
| §10 `"jobs"` hook, `wait_for`, SKIP LOCKED, independent reconciliation tick | 9, 10 |
| §11 recovery without a progress column | 11 (crash-after-M1 and crash-after-M2) |
| §14 gate 2 (route wall-clock) and gate 15 (recovery) | 11 |
| §14 remaining gates | distributed; each task's "prove the gate is not vacuous" step |

Not implemented here, by design: §1.1 repo-content scrub (a tracked policy decision, not engineering scope) and everything listed under Slice C.
