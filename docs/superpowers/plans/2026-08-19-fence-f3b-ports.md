# Fence F3b — The Ports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the narrow query, effect and accounting ports that tool code will
receive instead of a `Session`, each effect owning its own predicate, transaction,
commit and declared locking mechanism — additive, with no production caller converted.

**Architecture:** A `Session` is ambient authority: it exposes transactions,
connections, engines, raw SQL and ORM back-references, and four wrapper designs were
defeated empirically before this one. So tools stop receiving one. `ports/` holds
stdlib-only contracts — `FenceStamp`, commands, DTOs, `Protocol`s, the error taxonomy
— and `port_impl/` holds the SQLAlchemy-backed implementations that `tools/` will be
forbidden to import (the gate itself lands in F3c). Every semantic effect takes a
shared advisory lease on the workspace's commit-mutex key and then performs exactly
one predicate-bearing write; every lifecycle transition takes the same key
exclusively, so the two are serialized without the shared-then-exclusive upgrade that
would deadlock.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0.49, PostgreSQL 16 (testcontainers), pytest.

**Spec:** `docs/superpowers/specs/2026-08-18-fence-session-ownership-design.md`
(revision 8) — §3 the three port kinds, §3.4 `FenceStamp`, §3.5 the mechanism table,
§7 F3b's scope, §9 how F3b is proven.

**Companion spec:** `docs/superpowers/specs/2026-08-17-lifecycle-fence-design.md` —
the architecture. Task 9 corrects its §3.1.

## Global Constraints

Every task's requirements implicitly include this section.

- **GC-1 — Tools receive capabilities, never handles.** No port method may accept or
  return a `Session`, `Engine`, `Connection`, `sessionmaker`, or an attached ORM
  instance. There is no generic `fenced_write(...) as session` and no public
  `fenced_execute(ctx, statement)`; both hand back the authority this design removes.
- **GC-2 — One statement authorizes each write.** The generation predicate governs
  the WRITE, inside the statement that performs it. A predicate evaluated in its own
  statement guards nothing: both statements take fresh READ COMMITTED snapshots and a
  transition committing between them is invisible to the check and fully effective on
  the write. That defect shipped twice (`28abbc4`, `701fbb7`). A follow-up `SELECT`
  may only **classify** a failure for the error message — never authorize a write.
- **GC-3 — Each effect selects exactly ONE mechanism (spec §3.5) and never upgrades
  between them.** Taking the shared lease and later attempting the exclusive mutex is
  the one deadlock shape this protocol has. Every effect in F3b is DB-only and uses
  mechanism 1: shared lease, then the predicate-bearing write.
- **GC-4 — `ports/` is stdlib-only.** No `sqlalchemy`, no `call_manager.db.models`, no
  project module that imports either. Implementations live in `port_impl/`. Task 8
  installs the test that enforces this.
- **GC-5 — Every query result is a frozen dataclass, recursively free of ORM
  instances.** An attached instance carries `object_session(instance)`, which is the
  full `Session` again — the exact leak that defeated the forwarding-proxy design.
- **GC-6 — Accounting is owner-bound and deliberately unfenced.** Bound to
  `active_turn_job_id`, never "whichever turn is running". One conditional statement;
  a read-modify-write (`job.fetch_count += 1`) is prohibited here as everywhere.
- **GC-7 — Every lock acquisition is bounded, and the tail stays bounded.** Mirror
  `acquire_commit_mutex`: `SET LOCAL lock_timeout` before acquiring, and re-apply
  `MUTEX_TAIL_LOCK_TIMEOUT_SECONDS` after winning. Restoring `DEFAULT` is Slice C3's
  named defect.
- **GC-8 — Additive only.** No production caller is converted. No site sets
  `pass_invocation=True`. No existing tool module changes behaviour. The branch is
  inert in production.
- **GC-10 — Mutation testing happens AFTER the task's commit, never before.**
  `git checkout <path>` on a file the task itself created fails with
  `pathspec … did not match any file(s) known to git` — the mutation survives,
  and if the command was piped the non-zero exit is masked. Reverting to the
  *previous* commit is worse: it silently discards the current task's
  implementation. So every task commits its green implementation first, then
  mutates, then restores with `git checkout` and confirms
  `git status --porcelain` is empty before moving on.

- **GC-9 — Test command.** `uv run pytest -n 16 --dist loadfile`. Never `-n auto`
  (resolves to 72 here) and never `--dist loadgroup`. Baseline on master before this
  branch: **3608 passed, 1 skipped**. The `1 skipped` is a real-SDK test gated on
  `ANTHROPIC_API_KEY` — leave it skipped.

## Verified against the real database before planning

Both load-bearing assumptions were run against this project's testcontainers
PostgreSQL 16, not reasoned about:

- **`pg_advisory_xact_lock_shared` does not block another shared acquisition, and
  does block `pg_advisory_xact_lock`.** A three-connection probe: one holder in
  shared mode, a second shared acquire succeeded, an exclusive
  `acquire_commit_mutex(timeout_seconds=1)` timed out. This is the entire lease
  protocol, so it was checked rather than assumed.
- **`INSERT … FROM SELECT … WHERE live_at_generation(...) RETURNING id` works with
  bare `literal(None)` values.** No explicit `type_=` is needed for the nullable
  provenance columns (the "could not determine data type of parameter" risk does
  not materialise here), `status` and `created_at` server defaults apply, and
  `live_at_generation(ws_id, 0)` matches a fresh workspace at generation 0.

## Deviation from the spec, stated explicitly

Spec §9 assigns the **accounting interleaving test** to F3d, at the converted call
site. This plan additionally proves it at the port in Task 6, because the ownership
fix (binding to `active_turn_job_id` instead of "whichever turn is running") lives in
F3b, and an operation should not ship unproven for the exact defect it exists to fix.
F3d still re-proves it at the call site.

## File Structure

**Created — `src/call_manager/proposal_assist/ports/` (stdlib-only, tool-facing):**

| File | Responsibility |
|---|---|
| `__init__.py` | The `Ports` bundle. Re-exports nothing else. |
| `errors.py` | `WorkspaceGenerationStale` (moved here from `fencing.py`), `EffectRejected`, `UnfencedInvocation`. |
| `stamp.py` | `FenceStamp`, `Provenance`. |
| `queries.py` | `ReferenceView`, `ReferenceChunkView`, `Queries` Protocol. |
| `effects.py` | The command dataclasses, `Effects` Protocol, `EFFECT_MECHANISMS`. |
| `accounting.py` | `MAX_FETCHES_PER_TURN`, `Accounting` Protocol. |

**Created — `src/call_manager/proposal_assist/port_impl/` (SQLAlchemy; `tools/` may not import):**

| File | Responsibility |
|---|---|
| `__init__.py` | `build_ports(session_factory) -> Ports`. |
| `effects.py` | `SqlEffects` — the five semantic operations and the failure classifier. |
| `accounting.py` | `SqlAccounting` — reserve/refund, owner-bound. |
| `queries.py` | `SqlQueries` — column-level selects returning DTOs. |

**Modified:**

| File | Change |
|---|---|
| `src/call_manager/proposal_assist/fencing.py` | Add `acquire_shared_lease` (Primitive C). Re-export `WorkspaceGenerationStale` from `ports.errors`. Correct the Primitive A docstring. |
| `src/call_manager/proposal_assist/tools/registry.py` | `Invocation.stamp()` and `Invocation.provenance()`. |
| `tests/fencing/inventory.py` | `FENCING_IMPORT_FILES` gains `port_impl/effects.py`; `SLICE_STATUS["F3b"] -> "complete"`. |
| `docs/superpowers/specs/2026-08-17-lifecycle-fence-design.md` | §3.1 corrected. |

**Test files created:**

| File | Covers |
|---|---|
| `tests/proposal_assist/test_f3b_stamp.py` | Task 1 |
| `tests/proposal_assist/test_fencing_lease.py` | Task 2 |
| `tests/proposal_assist/f3b_seeds.py` | Shared seeding helpers (not a test module) |
| `tests/proposal_assist/test_f3b_effects.py` | Tasks 3, 4 |
| `tests/proposal_assist/test_f3b_lock_cases.py` | Task 5 |
| `tests/proposal_assist/test_f3b_accounting.py` | Task 6 |
| `tests/proposal_assist/test_f3b_queries.py` | Task 7 |
| `tests/proposal_assist/test_f3b_ports_boundary.py` | Task 8 |

---

### Task 1: The tool-facing foundation — `FenceStamp`, `Provenance`, the error taxonomy

**Why the errors move.** A tool must be able to *catch* `WorkspaceGenerationStale`.
It currently lives in `fencing.py`, which imports `sqlalchemy` and
`call_manager.db.models` — so under F3c's boundary no tool could name it. It moves to
the stdlib-only `ports/errors.py`; `fencing.py` re-exports it so all existing import
sites keep working unchanged.

**Files:**
- Create: `src/call_manager/proposal_assist/ports/__init__.py`
- Create: `src/call_manager/proposal_assist/ports/errors.py`
- Create: `src/call_manager/proposal_assist/ports/stamp.py`
- Modify: `src/call_manager/proposal_assist/fencing.py` (remove the class body, re-export)
- Modify: `src/call_manager/proposal_assist/tools/registry.py` (add two methods to `Invocation`)
- Test: `tests/proposal_assist/test_f3b_stamp.py`

**Interfaces:**
- Consumes: `Invocation` (frozen dataclass, `tools/registry.py`) with fields
  `workspace_id`, `lifecycle_generation: int | None`, `active_turn_job_id`,
  `origin_kind`, `origin_job_id`, `triggering_turn_id`, `triggering_prompt`,
  `created_by`, `origin_session_id`, `turn_selection`.
- Produces:
  - `FenceStamp(workspace_id: uuid.UUID, generation: int, active_turn_job_id: uuid.UUID | None, origin_job_id: uuid.UUID | None)` — frozen.
  - `Provenance(origin_kind: str, origin_session_id: str | None, origin_job_id: uuid.UUID | None, triggering_turn_id: uuid.UUID | None, triggering_prompt: str | None, created_by: int | None)` — frozen.
  - `Invocation.stamp() -> FenceStamp`, `Invocation.provenance() -> Provenance`.
  - `WorkspaceGenerationStale`, `EffectRejected(reason)`, `UnfencedInvocation`.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_f3b_stamp.py`:

```python
"""F3b — the stdlib-only foundation the tool layer is allowed to name."""

from __future__ import annotations

import uuid

import pytest

from call_manager.proposal_assist.ports.errors import (
    EffectRejected,
    UnfencedInvocation,
    WorkspaceGenerationStale,
)
from call_manager.proposal_assist.ports.stamp import FenceStamp, Provenance
from call_manager.proposal_assist.tools.registry import Invocation, ToolContext


def _ctx(**over) -> ToolContext:
    base = dict(
        workspace_id=uuid.uuid4(),
        repo_dir=None,
        session_factory=None,
        settings=None,
    )
    ctx = ToolContext(**base)
    for k, v in over.items():
        setattr(ctx, k, v)
    return ctx


def test_stamp_carries_the_four_identity_fields():
    turn_job = uuid.uuid4()
    origin_job = uuid.uuid4()
    ctx = _ctx(lifecycle_generation=7, active_turn_job_id=turn_job, origin_job_id=origin_job)
    stamp = Invocation.snapshot(ctx).stamp()
    assert stamp == FenceStamp(
        workspace_id=ctx.workspace_id,
        generation=7,
        active_turn_job_id=turn_job,
        origin_job_id=origin_job,
    )


def test_generation_zero_is_a_real_generation():
    """Workspace.lifecycle_generation has server_default 0, so a fresh workspace
    IS at generation 0. A falsy-check instead of an is-None check would refuse it."""
    stamp = Invocation.snapshot(_ctx(lifecycle_generation=0)).stamp()
    assert stamp.generation == 0


def test_an_invocation_without_a_claimed_generation_cannot_produce_a_stamp():
    """An actor that never claimed a generation has no belief to fence against.
    Refusing loudly beats defaulting to a generation it did not claim at."""
    ctx = _ctx(lifecycle_generation=None)
    with pytest.raises(UnfencedInvocation) as e:
        Invocation.snapshot(ctx).stamp()
    assert e.value.workspace_id == ctx.workspace_id


def test_the_stamp_is_frozen():
    stamp = Invocation.snapshot(_ctx(lifecycle_generation=1)).stamp()
    with pytest.raises(Exception):
        stamp.generation = 2  # type: ignore[misc]


def test_provenance_carries_the_row_data_the_stamp_deliberately_does_not():
    """origin_job_id appears in BOTH: the stamp uses it to target the actor's own
    job row, and provenance copies it into durable proposed-change rows."""
    turn_id, origin_job = uuid.uuid4(), uuid.uuid4()
    ctx = _ctx(
        lifecycle_generation=3,
        origin_kind="coherence_session",
        origin_session_id="sess-1",
        origin_job_id=origin_job,
        triggering_turn_id=turn_id,
        triggering_prompt="do the thing",
        created_by=42,
    )
    assert Invocation.snapshot(ctx).provenance() == Provenance(
        origin_kind="coherence_session",
        origin_session_id="sess-1",
        origin_job_id=origin_job,
        triggering_turn_id=turn_id,
        triggering_prompt="do the thing",
        created_by=42,
    )


def test_the_stale_error_still_imports_from_its_original_home():
    """fencing.py re-exports it so every pre-F3b import site keeps working."""
    from call_manager.proposal_assist.fencing import (
        WorkspaceGenerationStale as ReExported,
    )

    assert ReExported is WorkspaceGenerationStale


def test_effect_rejected_carries_the_message_the_tool_will_return():
    e = EffectRejected("a scorecard has already been submitted for this run")
    assert e.reason == "a scorecard has already been submitted for this run"
    assert str(e) == e.reason
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_f3b_stamp.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'call_manager.proposal_assist.ports'`

- [ ] **Step 3: Create the ports package**

`src/call_manager/proposal_assist/ports/__init__.py` — **the module docstring and
nothing else** in this task. Task 8 adds the `Ports` bundle, once the three port
modules it names exist:

```python
"""Tool-facing capability contracts — stdlib only.

Spec: docs/superpowers/specs/2026-08-18-fence-session-ownership-design.md §3.

Tools receive capabilities, never handles. NOTHING in this package may import
sqlalchemy, call_manager.db.models, or any project module that imports either:
an attached ORM instance carries object_session(instance), which is the full
Session again — the leak that defeated the forwarding-proxy design. The
implementations live in call_manager.proposal_assist.port_impl, which tool code
may not import (the gate lands in F3c).
"""
```

`src/call_manager/proposal_assist/ports/errors.py`:

```python
"""The error taxonomy a tool is allowed to catch — stdlib only.

WorkspaceGenerationStale lived in fencing.py through F1-F3a. It moves here
because a tool must be able to NAME it, and fencing.py imports sqlalchemy and
db.models. fencing.py re-exports it, so every pre-F3b import site is unchanged.
"""

from __future__ import annotations

import uuid


class WorkspaceGenerationStale(RuntimeError):
    """The workspace has moved past the generation the caller carried.

    NOT an error condition. It means "your epoch is over; produce no further
    semantic effect." It never reaches a user as a 5xx.

    It does NOT mean "write nothing". An actor that catches this must still
    record cleanup and accounting — tokens, dollars, terminal status, releasing
    an active-unique slot — via conditional writes only (spec 7). Abandoning
    silently strands the job holding its slot and destroys deliberate late token
    accounting (see review/pass_runner.py, which keeps the tokens a cancelled
    run's sub-call spent on purpose).
    """

    def __init__(
        self, workspace_id: uuid.UUID, *, expected: int, actual: int | None
    ) -> None:
        super().__init__(
            f"workspace {workspace_id} moved from generation {expected} to {actual}"
        )
        self.workspace_id = workspace_id
        self.expected = expected
        self.actual = actual


class EffectRejected(RuntimeError):
    """The effect was refused for a reason that is NOT staleness — a wrong or
    finished target, or a second submission where the contract is exactly-once.

    `reason` is the exact string the tool returns to the agent, so the messages
    cannot drift when F3d converts the call sites.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnfencedInvocation(RuntimeError):
    """A tool invocation reached an effect without a claimed generation.

    Never expected in production: F3a gives all five actors one. Refusing beats
    defaulting, which would submit an effect against a generation nobody claimed.
    """

    def __init__(self, workspace_id: uuid.UUID) -> None:
        super().__init__(f"invocation for workspace {workspace_id} carries no generation")
        self.workspace_id = workspace_id
```

`src/call_manager/proposal_assist/ports/stamp.py`:

```python
"""FenceStamp and Provenance — an actor's immutable identity, stdlib only.

Spec §3.4. Snapshotted per tool INVOCATION at dispatch, never read from the
retained ToolContext at the moment of the effect: set_turn_provenance restamps
that context every turn, so a late read reads a NEWER generation and the stale
actor's write is then perfectly fenced against the wrong generation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FenceStamp:
    """What an effect needs to fence itself and find its own target row.

    `origin_job_id` targets the actor's own background job. `active_turn_job_id`
    is separate and cannot be folded into it: registry.py leaves origin_job_id
    None for interactive contexts BY DESIGN, and tools/proposal.py copies it into
    durable provenance, so populating it would change stored data.
    """

    workspace_id: uuid.UUID
    generation: int
    active_turn_job_id: uuid.UUID | None
    origin_job_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class Provenance:
    """Row data, not identity: copied verbatim into durable proposed_changes."""

    origin_kind: str
    origin_session_id: str | None
    origin_job_id: uuid.UUID | None
    triggering_turn_id: uuid.UUID | None
    triggering_prompt: str | None
    created_by: int | None
```

- [ ] **Step 4: Move the exception and re-export it from `fencing.py`**

In `src/call_manager/proposal_assist/fencing.py`, delete the whole
`class WorkspaceGenerationStale(RuntimeError): ...` block (lines 44-64) and add the
import beside the existing ones:

```python
from call_manager.proposal_assist.drafting.locking import (
    WorkspaceDeleted,
    acquire_commit_mutex,
)
from call_manager.proposal_assist.ports.errors import WorkspaceGenerationStale

__all__ = [
    "WorkspaceGenerationStale",
    "acquire_commit_mutex_at_generation",
    "live_at_generation",
]
```

The `__all__` is load-bearing: without it `ruff` reports the re-export as an
unused import (F401) and a later cleanup would delete it.

- [ ] **Step 4b: Correct the paragraph that says the exception lives in `fencing.py`**

The module docstring's closing paragraph documents the location Step 4 just
changed. Replace it:

```python
`WorkspaceGenerationStale` lives here rather than beside `WorkspaceDeleted` in
drafting/locking.py because it is raised only by fencing code, and F2's coverage
gate keys on this module. The asymmetry is deliberate.
```

with:

```python
`WorkspaceGenerationStale` is DEFINED in ports/errors.py and re-exported here.
It moved in F3b: a tool has to be able to CATCH it, and this module imports
sqlalchemy and db.models, so under the F3c import boundary no tool could name it.
Every pre-F3b import site keeps working through the re-export.
```

- [ ] **Step 5: Add the two methods to `Invocation`**

In `src/call_manager/proposal_assist/tools/registry.py`, add to the `Invocation`
class, directly after `snapshot`:

```python
    def stamp(self) -> FenceStamp:
        """This invocation's fencing identity.

        Raises UnfencedInvocation rather than defaulting: an actor with no
        claimed generation has no belief to fence against, and defaulting would
        submit an effect against a generation nobody claimed. Generation 0 is
        REAL (Workspace.lifecycle_generation has server_default 0), so this
        tests for None, never for falsiness.
        """
        if self.lifecycle_generation is None:
            raise UnfencedInvocation(self.workspace_id)
        return FenceStamp(
            workspace_id=self.workspace_id,
            generation=self.lifecycle_generation,
            active_turn_job_id=self.active_turn_job_id,
            origin_job_id=self.origin_job_id,
        )

    def provenance(self) -> Provenance:
        """The provenance the propose_* effects copy into durable rows."""
        return Provenance(
            origin_kind=self.origin_kind,
            origin_session_id=self.origin_session_id,
            origin_job_id=self.origin_job_id,
            triggering_turn_id=self.triggering_turn_id,
            triggering_prompt=self.triggering_prompt,
            created_by=self.created_by,
        )
```

and the imports at the top of the module (real imports, not `TYPE_CHECKING` —
they are used at runtime):

```python
from call_manager.proposal_assist.ports.errors import UnfencedInvocation
from call_manager.proposal_assist.ports.stamp import FenceStamp, Provenance
```

- [ ] **Step 6: Run the new tests**

Run: `uv run pytest tests/proposal_assist/test_f3b_stamp.py -q`
Expected: `7 passed`

- [ ] **Step 7: Run the tests that own the moved exception**

Run: `uv run pytest tests/proposal_assist/test_fencing.py tests/proposal_assist/test_fencing_mutex.py tests/fencing -q`
Expected: all pass. If `tests/fencing` goes red, read the failure before changing
anything — `test_only_f3a_identities_import_the_fencing_primitives` is an AST gate
over `src/`, and `ports/errors.py` must NOT import `fencing`; the dependency runs
the other way.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/ports src/call_manager/proposal_assist/fencing.py src/call_manager/proposal_assist/tools/registry.py tests/proposal_assist/test_f3b_stamp.py
git commit -m "feat(ports): FenceStamp, Provenance and the tool-facing error taxonomy (F3b Task 1)"
```

---

### Task 2: Primitive C — the shared lease

**The problem it solves.** `live_at_generation` is an `exists()` subquery
(`fencing.py:98`). It takes **no lock on the workspace row**, so under READ COMMITTED
it evaluates against the statement's snapshot, and a delete/restore committing after
that snapshot is invisible to it. Reproduced on PostgreSQL 16: a generation-G
`INSERT … SELECT … WHERE live_at_generation(...)` qualifies, pauses inside the
statement, a second connection commits G+2, and the G effect lands durably against a
workspace now at G+2. For an `UPDATE` this is masked today only because PostgreSQL
re-checks the `WHERE` against the updated row version when a concurrent transaction
touches the **same target row**, which cancel-on-delete does — and that compensation
is removed in step 3 of the architecture.

The lease closes it: effects take the workspace's commit-mutex key in **shared** mode,
lifecycle transitions take it **exclusively**. Shared locks do not conflict with one
another, so effects stay concurrent; a lifecycle transition either waits for in-flight
effects to commit, or the effect waits and then observes the new generation.

**Files:**
- Modify: `src/call_manager/proposal_assist/fencing.py`
- Test: `tests/proposal_assist/test_fencing_lease.py`

**Interfaces:**
- Consumes: `commit_mutex_key`, `acquire_commit_mutex`, `CommitMutexTimeout`,
  `is_lock_timeout`, `MUTEX_TAIL_LOCK_TIMEOUT_SECONDS` — all from
  `call_manager.proposal_assist.drafting.locking`.
- Produces: `acquire_shared_lease(session: Session, workspace_id: uuid.UUID, *, timeout_seconds: int = 10) -> None`.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_fencing_lease.py`:

```python
"""Primitive C — the shared lease. Effects hold it in shared mode; lifecycle
transitions hold the same key exclusively."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import text

from call_manager.proposal_assist.drafting.locking import (
    CommitMutexTimeout,
    acquire_commit_mutex,
)
from call_manager.proposal_assist.fencing import acquire_shared_lease
from tests.proposal_assist.conftest import make_workspace


def _lock_timeout_ms(session) -> int:
    raw = session.execute(text("SHOW lock_timeout")).scalar()
    return int(raw.removesuffix("ms")) if raw.endswith("ms") else int(raw[:-1]) * 1000


def test_two_effects_hold_the_lease_concurrently(pg_session_factory):
    """Shared locks do not conflict. If effects serialized against each other the
    fence would be a global per-workspace write lock, not a lease."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()  # make_workspace does NOT flush — ws.id is None until it does
        ws_id = ws.id

    first_has_lease = threading.Event()
    second_got_lease = threading.Event()

    def _second():
        first_has_lease.wait(timeout=5)
        with pg_session_factory() as s2:
            acquire_shared_lease(s2, ws_id, timeout_seconds=3)
            second_got_lease.set()
            s2.rollback()

    t = threading.Thread(target=_second)
    with pg_session_factory() as s1:
        acquire_shared_lease(s1, ws_id)
        t.start()
        first_has_lease.set()
        assert second_got_lease.wait(timeout=10), "a second effect blocked on the lease"
        s1.rollback()
    t.join(timeout=10)


def test_the_lease_blocks_an_exclusive_lifecycle_acquisition(pg_session_factory):
    """The whole point. A lifecycle transition must not proceed while an effect
    is in flight."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()  # make_workspace does NOT flush — ws.id is None until it does
        ws_id = ws.id

    lease_held = threading.Event()
    outcome: dict = {}

    def _lifecycle():
        lease_held.wait(timeout=5)
        with pg_session_factory() as s2:
            try:
                acquire_commit_mutex(s2, ws_id, timeout_seconds=1)
                outcome["result"] = "acquired"
            except CommitMutexTimeout:
                outcome["result"] = "blocked"
            s2.rollback()

    t = threading.Thread(target=_lifecycle)
    with pg_session_factory() as s1:
        acquire_shared_lease(s1, ws_id)
        t.start()
        lease_held.set()
        t.join(timeout=20)
        s1.rollback()

    assert outcome["result"] == "blocked"


def test_the_lease_is_released_by_rollback(pg_session_factory):
    """pg_advisory_xact_lock_shared is TRANSACTION scoped — there is no explicit
    release, and an AUTOCOMMIT session would drop it instantly and silently
    provide no exclusion at all."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()  # make_workspace does NOT flush — ws.id is None until it does
        ws_id = ws.id

    with pg_session_factory() as s1:
        acquire_shared_lease(s1, ws_id)
        s1.rollback()

    with pg_session_factory() as s2:
        acquire_commit_mutex(s2, ws_id, timeout_seconds=2)  # must not raise
        s2.rollback()


def test_a_contended_lease_raises_commit_mutex_timeout(pg_session_factory):
    """Bounded, like every other acquisition here. An unbounded wait lets one
    wedged holder pin a worker indefinitely."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()  # make_workspace does NOT flush — ws.id is None until it does
        ws_id = ws.id

    holder_ready = threading.Event()
    release = threading.Event()

    def _exclusive_holder():
        with pg_session_factory() as s2:
            acquire_commit_mutex(s2, ws_id)
            holder_ready.set()
            release.wait(timeout=10)
            s2.rollback()

    t = threading.Thread(target=_exclusive_holder)
    t.start()
    try:
        holder_ready.wait(timeout=5)
        with pg_session_factory() as s1:
            with pytest.raises(CommitMutexTimeout):
                acquire_shared_lease(s1, ws_id, timeout_seconds=1)
            s1.rollback()
    finally:
        release.set()
        t.join(timeout=10)


def test_the_tail_stays_bounded_after_the_lease_is_won(pg_session_factory):
    """Slice C3's rule. Restoring DEFAULT (= 0 = disabled) here would un-bound
    every lock wait for the rest of the effect's transaction."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()  # make_workspace does NOT flush — ws.id is None until it does
        ws_id = ws.id

    with pg_session_factory() as s1:
        acquire_shared_lease(s1, ws_id, timeout_seconds=9)
        assert _lock_timeout_ms(s1) == 2000
        s1.rollback()


def test_a_non_positive_timeout_is_rejected(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        with pytest.raises(ValueError):
            acquire_shared_lease(s, ws.id, timeout_seconds=0)
        s.rollback()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_fencing_lease.py -q`
Expected: FAIL — `ImportError: cannot import name 'acquire_shared_lease'`

- [ ] **Step 3: Implement Primitive C**

In `src/call_manager/proposal_assist/fencing.py`, extend the import from
`drafting.locking` and append the function:

```python
from call_manager.proposal_assist.drafting.locking import (
    MUTEX_TAIL_LOCK_TIMEOUT_SECONDS,
    CommitMutexTimeout,
    WorkspaceDeleted,
    acquire_commit_mutex,
    commit_mutex_key,
    is_lock_timeout,
)
```

```python
def acquire_shared_lease(
    session: Session, workspace_id: uuid.UUID, *, timeout_seconds: int = 10
) -> None:
    """Primitive C. Take the workspace's commit-mutex key in SHARED mode for the
    rest of this transaction, so a lifecycle transition cannot commit underneath
    the effect that follows.

    Pair it with `live_at_generation` in the WHERE of the effect's single write
    (spec 3.5, mechanism 1). The predicate alone is NOT enough: it is an exists()
    subquery taking no lock on the workspace row, so under READ COMMITTED it
    evaluates against the statement's snapshot and a transition committing after
    that snapshot is invisible to it. Reproduced on PostgreSQL 16 with an INSERT,
    which has no target row and therefore no re-check to mask the race.

    The lease is what turns "I checked" into "I waited, then checked": shared
    locks do not conflict, so effects stay concurrent with each other, while a
    lifecycle transition needs the key EXCLUSIVELY and therefore either waits for
    in-flight effects or wins first and leaves the effect's predicate to fail.

    NEVER upgrade: an operation holding this lease must not go on to call
    acquire_commit_mutex* for the same workspace. Two operations each holding
    shared and each waiting to upgrade is the one deadlock shape this protocol
    has. An operation that will touch git or the filesystem takes the EXCLUSIVE
    mutex first, before its first effect of any kind.

    Transaction scoped: released at COMMIT or ROLLBACK, with no explicit unlock.
    Requires a non-AUTOCOMMIT session — an AUTOCOMMIT connection would release it
    immediately and silently provide no exclusion.

    Bounded exactly as acquire_commit_mutex is, and it leaves the same
    MUTEX_TAIL_LOCK_TIMEOUT_SECONDS cap in force for the rest of the transaction
    (Slice C3) rather than restoring DEFAULT, which is 0 = disabled.
    """
    timeout_ms = int(timeout_seconds) * 1000
    if timeout_ms <= 0:
        raise ValueError(f"timeout_seconds must be a positive int, got {timeout_seconds!r}")
    session.execute(text(f"SET LOCAL lock_timeout = '{timeout_ms}ms'"))
    try:
        session.execute(
            text("SELECT pg_advisory_xact_lock_shared(:k)"),
            {"k": commit_mutex_key(workspace_id)},
        )
    except OperationalError as exc:
        if is_lock_timeout(exc):
            raise CommitMutexTimeout(str(workspace_id)) from exc
        raise
    session.execute(text(f"SET LOCAL lock_timeout = '{MUTEX_TAIL_LOCK_TIMEOUT_SECONDS * 1000}ms'"))
```

Add `from sqlalchemy import exists, select, text` and
`from sqlalchemy.exc import OperationalError` to the module's imports, and add
`"acquire_shared_lease"` to `__all__`.

- [ ] **Step 3b: Correct the module docstring — there are three primitives now**

The header still says "Two primitives" and calls Primitive A "the default", which
is the claim this task disproves. Replace the block from `Two primitives, chosen
by what the effect touches:` down to the end of the `40P01 against the delete
path (spec 5).` paragraph with:

```python
Three primitives. Which ones an effect uses is decided ONCE per operation and
never changed mid-flight (2026-08-18 spec 3.5):

  A. `live_at_generation` — a PREDICATE, no lock. Goes in the WHERE of the
     statement that performs the effect, so the fence and the write are ONE
     statement. NOT SUFFICIENT ALONE: it takes no lock on the workspace row, so
     under READ COMMITTED it evaluates against the statement's snapshot and a
     transition committing after that snapshot is invisible to it.

  C. `acquire_shared_lease` — the workspace's commit-mutex key in SHARED mode.
     Pairs with A for every DB-only effect, acquired FIRST. Shared locks do not
     conflict, so effects stay concurrent with each other; a lifecycle
     transition needs the key exclusively, so it either waits for in-flight
     effects or wins first and leaves A to fail.

  B. `acquire_commit_mutex_at_generation` — the EXCLUSIVE mutex plus an
     under-mutex re-read. For effects spanning git, the filesystem, or multiple
     rows that must move together, taken before the first effect of any kind.

NEVER take C and then B for the same workspace. Two operations each holding
shared and each waiting to upgrade is the one deadlock shape this protocol has.

Where a transaction ALREADY holds the WORKSPACE row under FOR UPDATE, use none
of them: assert the generation on that already-locked row. Only the workspace row
qualifies — a job row carries no lifecycle state, so locking one proves nothing.
Acquiring the mutex afterwards inverts the normative M -> J -> W order and opens
a real Postgres 40P01 against the delete path (spec 5).
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_fencing_lease.py -q`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/fencing.py tests/proposal_assist/test_fencing_lease.py
git commit -m "feat(fencing): Primitive C — the shared lease (F3b Task 2)"
```

---

### Task 3: The three single-row UPDATE effects

`submit_scorecard`, `submit_review`, `submit_uncited_claims`. Today each is a
`session.get(..., with_for_update=True)` followed by guard checks, a Python-side
attribute assignment and a `commit()` — a read, then a write, in two statements. Each
becomes **one** predicate-bearing `UPDATE … RETURNING` behind a shared lease.

**The classifier is not a guard.** When the `UPDATE` matches nothing, the operation
runs a follow-up `SELECT` purely to decide *which message* the agent sees. It
authorizes nothing, and it runs strictly **after** the write was already refused by
its own statement. Moving any part of it before the write reintroduces the
two-statement race (GC-2).

**Files:**
- Create: `src/call_manager/proposal_assist/ports/effects.py`
- Create: `src/call_manager/proposal_assist/port_impl/__init__.py`
- Create: `src/call_manager/proposal_assist/port_impl/effects.py`
- Create: `tests/proposal_assist/f3b_seeds.py`
- Create: `tests/proposal_assist/test_f3b_effects.py`
- Modify: `tests/fencing/inventory.py` (`FENCING_IMPORT_FILES`)

**Interfaces:**
- Consumes: `FenceStamp`, `Provenance` (Task 1); `acquire_shared_lease`,
  `live_at_generation`, `WorkspaceGenerationStale` (Tasks 1-2);
  `EffectRejected` (Task 1).
- Produces:
  - Commands `SubmitScorecard(scorecard: dict)`, `SubmitReview(review_run_item_id: uuid.UUID, markdown: str)`, `SubmitUncitedClaims(claims: list[dict])`.
  - `Effects` Protocol with `submit_scorecard`, `submit_review`, `submit_uncited_claims` (Task 4 adds two more).
  - `EFFECT_MECHANISMS: Mapping[str, str]`.
  - `SqlEffects(session_factory)` in `port_impl/effects.py`.
  - `tests/proposal_assist/f3b_seeds.py` exporting `seed_workspace`, `bump_generation`, `seed_evaluator_job`, `seed_uncited_job`, `seed_review_item`, `seed_running_turn` (the last is used by Task 6).

- [ ] **Step 1: Write the seeding helpers**

Create `tests/proposal_assist/f3b_seeds.py` (a helper module, not a test module —
it has no `test_` functions and pytest will not collect it):

```python
"""Row seeding for the F3b port tests, shared by the unit and lock-case suites.

make_workspace is a FUNCTION taking a session (tests/proposal_assist/conftest.py),
not a fixture.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import update

from call_manager.db.models import (
    ChatTurn,
    EvaluatorJob,
    ReviewRun,
    ReviewRunItem,
    TurnJob,
    UncitedClaimsJob,
    Workspace,
)
from tests.proposal_assist.conftest import make_workspace


def seed_workspace(session, *, generation: int = 0) -> uuid.UUID:
    ws = make_workspace(session)
    session.flush()
    session.execute(
        update(Workspace).where(Workspace.id == ws.id).values(lifecycle_generation=generation)
    )
    session.commit()
    return ws.id


def bump_generation(session, workspace_id: uuid.UUID, *, to: int) -> None:
    session.execute(
        update(Workspace).where(Workspace.id == workspace_id).values(lifecycle_generation=to)
    )


def seed_evaluator_job(session, workspace_id: uuid.UUID, *, generation: int) -> uuid.UUID:
    job = EvaluatorJob(
        workspace_id=workspace_id,
        status="running",
        claimed_at_generation=generation,
        started_at=datetime.now(UTC),
    )
    session.add(job)
    session.commit()
    return job.id


def seed_uncited_job(session, workspace_id: uuid.UUID, *, generation: int) -> uuid.UUID:
    job = UncitedClaimsJob(
        workspace_id=workspace_id,
        status="running",
        claimed_at_generation=generation,
        started_at=datetime.now(UTC),
    )
    session.add(job)
    session.commit()
    return job.id


def seed_review_item(
    session, workspace_id: uuid.UUID, *, generation: int
) -> tuple[uuid.UUID, uuid.UUID]:
    """Returns (review_run_id, review_run_item_id). ReviewRun requires draft_sha."""
    run = ReviewRun(
        workspace_id=workspace_id,
        status="running",
        claimed_at_generation=generation,
        draft_sha="0" * 40,
    )
    session.add(run)
    session.flush()
    item = ReviewRunItem(review_run_id=run.id, kind="fit_check", status="running")
    session.add(item)
    session.commit()
    return run.id, item.id


def seed_running_turn(session, workspace_id: uuid.UUID) -> uuid.UUID:
    """A running TurnJob for the accounting tests. TurnJob needs a ChatTurn."""
    ws = session.get(Workspace, workspace_id)
    turn = ChatTurn(workspace_id=workspace_id, role="user", content={"type": "text", "text": "hi"})
    session.add(turn)
    session.flush()
    job = TurnJob(
        workspace_id=workspace_id,
        user_turn_id=turn.id,
        submitted_by_user_id=ws.owner_id,
        status="running",
        queued_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
    )
    session.add(job)
    session.commit()
    return job.id
```

- [ ] **Step 2: Write the failing test**

Create `tests/proposal_assist/test_f3b_effects.py`:

```python
"""F3b — the semantic effect operations. Each owns its predicate, transaction
and commit; the tool that will call it holds no session at all."""

from __future__ import annotations

import pytest

from call_manager.db.models import EvaluatorJob, ReviewRunItem, UncitedClaimsJob
from call_manager.proposal_assist.ports.effects import (
    EFFECT_MECHANISMS,
    Effects,
    SubmitReview,
    SubmitScorecard,
    SubmitUncitedClaims,
)
from call_manager.proposal_assist.ports.errors import (
    EffectRejected,
    WorkspaceGenerationStale,
)
from call_manager.proposal_assist.ports.stamp import FenceStamp
from call_manager.proposal_assist.port_impl.effects import SqlEffects
from tests.proposal_assist.f3b_seeds import (
    bump_generation,
    seed_evaluator_job,
    seed_review_item,
    seed_uncited_job,
    seed_workspace,
)

_CARD = {"criteria": [], "overall_band": "adequate", "summary": "ok"}


def _stamp(ws_id, *, generation, origin_job_id=None) -> FenceStamp:
    return FenceStamp(
        workspace_id=ws_id,
        generation=generation,
        active_turn_job_id=None,
        origin_job_id=origin_job_id,
    )


# --------------------------------------------------------------- the contract


def test_every_effect_declares_its_mechanism():
    """Spec 3.5: a named port does not make its fence atomic. Each operation
    states WHICH mechanism it uses, and this test is what stops the table from
    drifting away from the code as operations are added."""
    declared = set(EFFECT_MECHANISMS)
    protocol_methods = {n for n in vars(Effects) if not n.startswith("_")}
    assert declared == protocol_methods, (
        f"undeclared: {sorted(protocol_methods - declared)}; "
        f"stale: {sorted(declared - protocol_methods)}"
    )


def test_the_implementation_satisfies_the_protocol(pg_session_factory):
    assert isinstance(SqlEffects(pg_session_factory), Effects)


def test_no_effect_returns_a_session_or_an_orm_instance(pg_session_factory):
    """GC-1. A returned ORM instance carries object_session(instance), which is
    the full Session again."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        job_id = seed_evaluator_job(s, ws_id, generation=1)
    result = SqlEffects(pg_session_factory).submit_scorecard(
        _stamp(ws_id, generation=1, origin_job_id=job_id), SubmitScorecard(scorecard=_CARD)
    )
    assert result is None


# ------------------------------------------------------------ submit_scorecard


def test_submit_scorecard_lands_at_the_claimed_generation(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=4)
        job_id = seed_evaluator_job(s, ws_id, generation=4)

    SqlEffects(pg_session_factory).submit_scorecard(
        _stamp(ws_id, generation=4, origin_job_id=job_id), SubmitScorecard(scorecard=_CARD)
    )

    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, job_id).scorecard == _CARD


def test_submit_scorecard_refuses_a_stale_generation(pg_session_factory):
    """The field, not the row count. A stale write onto an existing row leaves
    rowcount unchanged, so counting rows would pass a broken implementation."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=4)
        job_id = seed_evaluator_job(s, ws_id, generation=4)
        bump_generation(s, ws_id, to=6)
        s.commit()

    with pytest.raises(WorkspaceGenerationStale) as e:
        SqlEffects(pg_session_factory).submit_scorecard(
            _stamp(ws_id, generation=4, origin_job_id=job_id), SubmitScorecard(scorecard=_CARD)
        )
    assert (e.value.expected, e.value.actual) == (4, 6)

    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, job_id).scorecard is None


def test_submit_scorecard_refuses_a_soft_deleted_workspace(pg_session_factory):
    from datetime import UTC, datetime

    from call_manager.db.models import Workspace

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=4)
        job_id = seed_evaluator_job(s, ws_id, generation=4)
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    with pytest.raises(WorkspaceGenerationStale):
        SqlEffects(pg_session_factory).submit_scorecard(
            _stamp(ws_id, generation=4, origin_job_id=job_id), SubmitScorecard(scorecard=_CARD)
        )
    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, job_id).scorecard is None


def test_submit_scorecard_is_exactly_once(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        job_id = seed_evaluator_job(s, ws_id, generation=1)

    effects = SqlEffects(pg_session_factory)
    stamp = _stamp(ws_id, generation=1, origin_job_id=job_id)
    effects.submit_scorecard(stamp, SubmitScorecard(scorecard=_CARD))
    with pytest.raises(EffectRejected) as e:
        effects.submit_scorecard(stamp, SubmitScorecard(scorecard={"overall_band": "gap"}))
    assert e.value.reason == "a scorecard has already been submitted for this run"

    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, job_id).scorecard == _CARD


def test_submit_scorecard_refuses_another_workspaces_job(pg_session_factory):
    with pg_session_factory() as s:
        mine = seed_workspace(s, generation=1)
        theirs = seed_workspace(s, generation=1)
        their_job = seed_evaluator_job(s, theirs, generation=1)

    with pytest.raises(EffectRejected) as e:
        SqlEffects(pg_session_factory).submit_scorecard(
            _stamp(mine, generation=1, origin_job_id=their_job),
            SubmitScorecard(scorecard=_CARD),
        )
    assert e.value.reason == (
        "scorecard target is not an active evaluator run for this workspace"
    )
    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, their_job).scorecard is None


def test_submit_scorecard_refuses_a_job_that_is_no_longer_running(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        job_id = seed_evaluator_job(s, ws_id, generation=1)
        s.get(EvaluatorJob, job_id).status = "cancelled"
        s.commit()

    with pytest.raises(EffectRejected):
        SqlEffects(pg_session_factory).submit_scorecard(
            _stamp(ws_id, generation=1, origin_job_id=job_id), SubmitScorecard(scorecard=_CARD)
        )


# -------------------------------------------------------------- submit_review


def test_submit_review_lands_on_the_running_item(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=2)
        run_id, item_id = seed_review_item(s, ws_id, generation=2)

    SqlEffects(pg_session_factory).submit_review(
        _stamp(ws_id, generation=2, origin_job_id=run_id),
        SubmitReview(review_run_item_id=item_id, markdown="a review"),
    )

    with pg_session_factory() as s:
        assert s.get(ReviewRunItem, item_id).raw_response_json == {"markdown": "a review"}


def test_submit_review_refuses_a_stale_generation(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=2)
        run_id, item_id = seed_review_item(s, ws_id, generation=2)
        bump_generation(s, ws_id, to=3)
        s.commit()

    with pytest.raises(WorkspaceGenerationStale):
        SqlEffects(pg_session_factory).submit_review(
            _stamp(ws_id, generation=2, origin_job_id=run_id),
            SubmitReview(review_run_item_id=item_id, markdown="a review"),
        )
    with pg_session_factory() as s:
        assert s.get(ReviewRunItem, item_id).raw_response_json is None


def test_submit_review_refuses_an_item_from_another_workspaces_run(pg_session_factory):
    """Ownership runs through the PARENT run — review_run_items has no
    workspace_id of its own."""
    with pg_session_factory() as s:
        mine = seed_workspace(s, generation=1)
        theirs = seed_workspace(s, generation=1)
        their_run, their_item = seed_review_item(s, theirs, generation=1)

    with pytest.raises(EffectRejected) as e:
        SqlEffects(pg_session_factory).submit_review(
            _stamp(mine, generation=1, origin_job_id=their_run),
            SubmitReview(review_run_item_id=their_item, markdown="x"),
        )
    assert e.value.reason == "submit_review target run is not active for this workspace"
    with pg_session_factory() as s:
        assert s.get(ReviewRunItem, their_item).raw_response_json is None


def test_submit_review_is_exactly_once(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        run_id, item_id = seed_review_item(s, ws_id, generation=1)

    effects = SqlEffects(pg_session_factory)
    stamp = _stamp(ws_id, generation=1, origin_job_id=run_id)
    effects.submit_review(stamp, SubmitReview(review_run_item_id=item_id, markdown="first"))
    with pytest.raises(EffectRejected) as e:
        effects.submit_review(stamp, SubmitReview(review_run_item_id=item_id, markdown="second"))
    assert e.value.reason == "a review has already been submitted for this item"

    with pg_session_factory() as s:
        assert s.get(ReviewRunItem, item_id).raw_response_json == {"markdown": "first"}



def test_a_foreign_item_that_already_has_a_review_still_reports_the_run(
    pg_session_factory,
):
    """REGRESSION on classifier ORDERING. Checking raw_response_json before the
    parent run's ownership would confirm, to a caller holding another
    workspace's item id, that a review already exists there — and would change
    the message review_submit.py returns today."""
    with pg_session_factory() as s:
        mine = seed_workspace(s, generation=1)
        theirs = seed_workspace(s, generation=1)
        their_run, their_item = seed_review_item(s, theirs, generation=1)
        s.get(ReviewRunItem, their_item).raw_response_json = {"markdown": "theirs"}
        s.commit()

    with pytest.raises(EffectRejected) as e:
        SqlEffects(pg_session_factory).submit_review(
            _stamp(mine, generation=1, origin_job_id=their_run),
            SubmitReview(review_run_item_id=their_item, markdown="x"),
        )
    assert e.value.reason == "submit_review target run is not active for this workspace"

    with pg_session_factory() as s:
        assert s.get(ReviewRunItem, their_item).raw_response_json == {"markdown": "theirs"}

# ------------------------------------------------------- submit_uncited_claims


def test_submit_uncited_claims_lands_at_the_claimed_generation(pg_session_factory):
    claims = [{"claim_text": "x", "section_index": 0, "reason": "y"}]
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=9)
        job_id = seed_uncited_job(s, ws_id, generation=9)

    SqlEffects(pg_session_factory).submit_uncited_claims(
        _stamp(ws_id, generation=9, origin_job_id=job_id), SubmitUncitedClaims(claims=claims)
    )

    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).claims == claims


def test_submit_uncited_claims_refuses_a_stale_generation(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=9)
        job_id = seed_uncited_job(s, ws_id, generation=9)
        bump_generation(s, ws_id, to=10)
        s.commit()

    with pytest.raises(WorkspaceGenerationStale):
        SqlEffects(pg_session_factory).submit_uncited_claims(
            _stamp(ws_id, generation=9, origin_job_id=job_id), SubmitUncitedClaims(claims=[])
        )
    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).claims is None


def test_submit_uncited_claims_accepts_an_empty_list_exactly_once(pg_session_factory):
    """An empty list is a real result — "I found nothing" — and must not be
    confused with "not submitted yet"."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=0)
        job_id = seed_uncited_job(s, ws_id, generation=0)

    effects = SqlEffects(pg_session_factory)
    stamp = _stamp(ws_id, generation=0, origin_job_id=job_id)
    effects.submit_uncited_claims(stamp, SubmitUncitedClaims(claims=[]))
    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).claims == []
    with pytest.raises(EffectRejected) as e:
        effects.submit_uncited_claims(stamp, SubmitUncitedClaims(claims=[]))
    assert e.value.reason == "claims have already been submitted for this run"


def test_generation_zero_is_fenced_like_any_other(pg_session_factory):
    """Workspace.lifecycle_generation has server_default 0. A `not generation`
    check anywhere in the chain would wave a fresh workspace straight through."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=0)
        job_id = seed_uncited_job(s, ws_id, generation=0)
        bump_generation(s, ws_id, to=1)
        s.commit()

    with pytest.raises(WorkspaceGenerationStale) as e:
        SqlEffects(pg_session_factory).submit_uncited_claims(
            _stamp(ws_id, generation=0, origin_job_id=job_id), SubmitUncitedClaims(claims=[])
        )
    assert (e.value.expected, e.value.actual) == (0, 1)
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_f3b_effects.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'call_manager.proposal_assist.ports.effects'`

- [ ] **Step 4: Write the tool-facing contract**

Create `src/call_manager/proposal_assist/ports/effects.py`:

```python
"""Semantic effect commands and the Effects contract — stdlib only.

Spec 3.2. One method per semantic operation, each taking an immutable FenceStamp
and a command object. The method owns the session, the generation check, the
transaction and the commit; the caller sees none of them.

There is deliberately NO generic `fenced_write(...) as session` and NO public
`fenced_execute(ctx, statement)`. The first hands back a session; the second
hands back the ability to compose any statement against any table. Both are the
authority this design removes.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from call_manager.proposal_assist.ports.stamp import FenceStamp, Provenance


@dataclass(frozen=True, slots=True)
class SubmitScorecard:
    scorecard: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SubmitReview:
    review_run_item_id: uuid.UUID
    markdown: str


@dataclass(frozen=True, slots=True)
class SubmitUncitedClaims:
    claims: list[dict[str, Any]]


@runtime_checkable
class Effects(Protocol):
    """Every method raises WorkspaceGenerationStale when the workspace has moved
    past the stamp's generation, and EffectRejected for a wrong, finished or
    already-submitted target."""

    def submit_scorecard(self, stamp: FenceStamp, cmd: SubmitScorecard) -> None: ...

    def submit_review(self, stamp: FenceStamp, cmd: SubmitReview) -> None: ...

    def submit_uncited_claims(
        self, stamp: FenceStamp, cmd: SubmitUncitedClaims
    ) -> None: ...


#: Spec 3.5 — every effect operation and the ONE mechanism it uses. A named port
#: does not make its internal fence atomic, so each operation declares its
#: mechanism and never upgrades between them. test_every_effect_declares_its_
#: mechanism keeps this table from drifting away from the Protocol.
#:
#: Every F3b operation is DB-only (mechanism 1). The exclusive-mutex mechanism
#: belongs to effects that touch git or the filesystem, which arrive in F3e.
EFFECT_MECHANISMS: Mapping[str, str] = MappingProxyType(
    {
        "submit_scorecard": "shared lease + live_at_generation in the UPDATE",
        "submit_review": "shared lease + live_at_generation in the UPDATE",
        "submit_uncited_claims": "shared lease + live_at_generation in the UPDATE",
    }
)
```

> **Task 3 omits `field` and `Provenance` from the imports.** Only Task 4's two
> propose commands use them, and `ruff check` (F401) is this project's gate — an
> import added a task early fails it. Task 4 adds them when it adds the commands.

- [ ] **Step 5: Write the implementation**

Create `src/call_manager/proposal_assist/port_impl/__init__.py`:

```python
"""SQLAlchemy-backed port implementations. Tool code may NOT import this package.

The split is the whole mechanism: `ports/` is stdlib-only and tool-facing, this
package holds every session, model and statement. F3c installs the import gate
that enforces it; F3b establishes the shape.
"""
```

Create `src/call_manager/proposal_assist/port_impl/effects.py`:

```python
"""SqlEffects — the semantic operations, each owning its own fence.

Every operation here follows mechanism 1 of spec 3.5: acquire the SHARED lease,
then perform ONE predicate-bearing write, then inspect what it returned.

Two rules govern every method and neither is negotiable:

  1. The generation predicate is INSIDE the write statement. A predicate in its
     own statement guards nothing — both statements take fresh READ COMMITTED
     snapshots. That defect shipped twice (28abbc4, 701fbb7).
  2. The lease is taken BEFORE the write and never upgraded to the exclusive
     mutex. Shared-then-exclusive is the one deadlock shape this protocol has.
"""

from __future__ import annotations

import uuid
from typing import Any, NoReturn

from sqlalchemy import exists, select, update
from sqlalchemy.orm import sessionmaker

from call_manager.db.models import (
    EvaluatorJob,
    ReviewRun,
    ReviewRunItem,
    UncitedClaimsJob,
    Workspace,
)
from call_manager.proposal_assist.fencing import acquire_shared_lease, live_at_generation
from call_manager.proposal_assist.ports.effects import (
    SubmitReview,
    SubmitScorecard,
    SubmitUncitedClaims,
)
from call_manager.proposal_assist.ports.errors import (
    EffectRejected,
    WorkspaceGenerationStale,
)
from call_manager.proposal_assist.ports.stamp import FenceStamp

_SCORECARD_TWICE = "a scorecard has already been submitted for this run"
_SCORECARD_TARGET = "scorecard target is not an active evaluator run for this workspace"
_REVIEW_TWICE = "a review has already been submitted for this item"
_REVIEW_RUN = "submit_review target run is not active for this workspace"
_REVIEW_ITEM = "submit_review target is not a running item for this run"
_CLAIMS_TWICE = "claims have already been submitted for this run"
_CLAIMS_TARGET = "target is not an active uncited-claims run for this workspace"


def _explain_refusal(
    session,
    stamp: FenceStamp,
    *,
    checks: tuple[tuple[Any, str], ...],
    fallback: str,
) -> NoReturn:
    """Decide WHICH message the agent sees. Authorizes nothing.

    Runs only after the write's own single statement already refused it. Every
    statement here is diagnostic: moving any of it ahead of the write would turn
    the operation into the two-statement race the fence exists to close.

    Staleness is classified first because a deletion also bumps the generation,
    so both conditions hold at once and callers map them to different outcomes.
    """
    row = session.execute(
        select(Workspace.lifecycle_generation, Workspace.soft_deleted_at).where(
            Workspace.id == stamp.workspace_id
        )
    ).first()
    if row is None or row[1] is not None or row[0] != stamp.generation:
        raise WorkspaceGenerationStale(
            stamp.workspace_id,
            expected=stamp.generation,
            actual=None if row is None else row[0],
        )
    for clause, message in checks:
        if session.execute(select(clause)).scalar():
            raise EffectRejected(message)
    raise EffectRejected(fallback)


class SqlEffects:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._sessions = session_factory

    def submit_scorecard(self, stamp: FenceStamp, cmd: SubmitScorecard) -> None:
        with self._sessions.begin() as session:
            acquire_shared_lease(session, stamp.workspace_id)
            landed = session.execute(
                update(EvaluatorJob)
                .where(
                    EvaluatorJob.id == stamp.origin_job_id,
                    EvaluatorJob.workspace_id == stamp.workspace_id,
                    EvaluatorJob.status == "running",
                    EvaluatorJob.scorecard.is_(None),
                    live_at_generation(stamp.workspace_id, stamp.generation),
                )
                .values(scorecard=cmd.scorecard)
                .returning(EvaluatorJob.id)
            ).first()
            if landed is None:
                _explain_refusal(
                    session,
                    stamp,
                    checks=(
                        (
                            exists().where(
                                EvaluatorJob.id == stamp.origin_job_id,
                                EvaluatorJob.workspace_id == stamp.workspace_id,
                                EvaluatorJob.scorecard.is_not(None),
                            ),
                            _SCORECARD_TWICE,
                        ),
                    ),
                    fallback=_SCORECARD_TARGET,
                )

    def submit_review(self, stamp: FenceStamp, cmd: SubmitReview) -> None:
        run_is_active = exists().where(
            ReviewRun.id == stamp.origin_job_id,
            ReviewRun.workspace_id == stamp.workspace_id,
            ReviewRun.status == "running",
        )
        with self._sessions.begin() as session:
            acquire_shared_lease(session, stamp.workspace_id)
            landed = session.execute(
                update(ReviewRunItem)
                .where(
                    ReviewRunItem.id == cmd.review_run_item_id,
                    ReviewRunItem.review_run_id == stamp.origin_job_id,
                    ReviewRunItem.status == "running",
                    ReviewRunItem.raw_response_json.is_(None),
                    run_is_active,
                    live_at_generation(stamp.workspace_id, stamp.generation),
                )
                .values(raw_response_json={"markdown": cmd.markdown})
                .returning(ReviewRunItem.id)
            ).first()
            if landed is None:
                # ORDER MATTERS and it mirrors review_submit.py's today: item
                # identity/status, THEN the parent run's ownership, THEN
                # exactly-once. Checking the response first would tell a caller
                # holding a foreign item's id that a review already exists there
                # — cross-workspace state, and a changed message besides.
                item_is_claimable = exists().where(
                    ReviewRunItem.id == cmd.review_run_item_id,
                    ReviewRunItem.review_run_id == stamp.origin_job_id,
                    ReviewRunItem.status == "running",
                )
                _explain_refusal(
                    session,
                    stamp,
                    checks=(
                        (~item_is_claimable, _REVIEW_ITEM),
                        (~run_is_active, _REVIEW_RUN),
                        (
                            exists().where(
                                ReviewRunItem.id == cmd.review_run_item_id,
                                ReviewRunItem.review_run_id == stamp.origin_job_id,
                                ReviewRunItem.raw_response_json.is_not(None),
                            ),
                            _REVIEW_TWICE,
                        ),
                    ),
                    fallback=_REVIEW_ITEM,
                )

    def submit_uncited_claims(self, stamp: FenceStamp, cmd: SubmitUncitedClaims) -> None:
        with self._sessions.begin() as session:
            acquire_shared_lease(session, stamp.workspace_id)
            landed = session.execute(
                update(UncitedClaimsJob)
                .where(
                    UncitedClaimsJob.id == stamp.origin_job_id,
                    UncitedClaimsJob.workspace_id == stamp.workspace_id,
                    UncitedClaimsJob.status == "running",
                    UncitedClaimsJob.claims.is_(None),
                    live_at_generation(stamp.workspace_id, stamp.generation),
                )
                .values(claims=cmd.claims)
                .returning(UncitedClaimsJob.id)
            ).first()
            if landed is None:
                _explain_refusal(
                    session,
                    stamp,
                    checks=(
                        (
                            exists().where(
                                UncitedClaimsJob.id == stamp.origin_job_id,
                                UncitedClaimsJob.workspace_id == stamp.workspace_id,
                                UncitedClaimsJob.claims.is_not(None),
                            ),
                            _CLAIMS_TWICE,
                        ),
                    ),
                    fallback=_CLAIMS_TARGET,
                )
```

- [ ] **Step 6: Open the fencing-import allowlist for the port implementation**

`tests/fencing/test_only_f3a_identities_import_the_fencing_primitives` is an AST gate
over `src/` with an **exact, currently empty** allowlist. `port_impl/effects.py`
imports `fencing`, so without this the gate goes red.

In `tests/fencing/inventory.py`, replace the `FENCING_IMPORT_FILES` definition:

```python
#: A SEPARATE, EXACT set for importing the F1 fencing primitives. Empty through
#: F3a — nothing imported them yet. F3b adds precisely its own port
#: implementation, which is the only place a fence may now be expressed: the
#: whole point of the ports is that no tool, route or pass runner writes one.
#: Sharing one list with the column check would both permit premature fencing
#: imports across all of F3a's files and block the ports, which live in neither.
FENCING_IMPORT_FILES: frozenset[str] = frozenset({
    "proposal_assist/port_impl/effects.py",
})
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_f3b_effects.py tests/fencing -q`
Expected: `test_f3b_effects.py` all pass; `tests/fencing` all pass.

- [ ] **Step 8: Commit the green implementation**

Commit BEFORE mutating (GC-10): `port_impl/effects.py` does not exist in git yet,
so a `git checkout` of it would fail and leave the mutation in the tree.

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/ports src/call_manager/proposal_assist/port_impl tests/proposal_assist/f3b_seeds.py tests/proposal_assist/test_f3b_effects.py tests/fencing/inventory.py
git commit -m "feat(ports): the three single-row UPDATE effects (F3b Task 3)"
```

- [ ] **Step 9: Prove the fence is load-bearing, by mutation**

Run each mutation, confirm the named failure, then restore and confirm the tree
is clean.

Mutation 1 — drop the predicate from `submit_scorecard`'s `WHERE`
(delete the `live_at_generation(...)` line):

Run: `uv run pytest tests/proposal_assist/test_f3b_effects.py -q -k scorecard`
Expected: FAIL `test_submit_scorecard_refuses_a_stale_generation` — a stale
scorecard lands.

```bash
git checkout src/call_manager/proposal_assist/port_impl/effects.py
git status --porcelain   # MUST be empty — see GC-10
```

Mutation 2 — split the predicate out of the write (the shape GC-2 forbids):
replace the `live_at_generation(...)` line in `submit_uncited_claims` with a
preceding separate statement
`session.execute(select(live_at_generation(stamp.workspace_id, stamp.generation))).scalar()`.

Run: `uv run pytest tests/proposal_assist/test_f3b_effects.py -q -k uncited`
Expected: FAIL `test_submit_uncited_claims_refuses_a_stale_generation` — the
pre-check reads the CURRENT generation and refuses nothing.

```bash
git checkout src/call_manager/proposal_assist/port_impl/effects.py
git status --porcelain   # MUST be empty — see GC-10
```

---

### Task 4: The two INSERT effects

`propose_artifact_edit` and `propose_citation_insert` both write a **pending**
`proposed_changes` row — no git commit (the git write happens later, at accept time,
on the user-side path, which is not a tool site).

**An INSERT is the exposed case.** It has no target row, so PostgreSQL's re-check of
the `WHERE` against an updated row version — the compensation that masks the race for
an `UPDATE` today — does not apply. This is the shape reproduced on PostgreSQL 16, and
it is why these two operations must be `INSERT … SELECT … WHERE live_at_generation(...)`
behind the lease, never `INSERT … VALUES` after a check.

**Two column defaults matter.** `INSERT … FROM SELECT` does **not** apply Python-side
column defaults, so `ProposedChange.id` (`default=uuid.uuid4`) must be supplied
explicitly. `status` and `created_at` are **server** defaults and are correctly
omitted.

**Files:**
- Modify: `src/call_manager/proposal_assist/ports/effects.py`
- Modify: `src/call_manager/proposal_assist/port_impl/effects.py`
- Modify: `tests/proposal_assist/test_f3b_effects.py`

**Interfaces:**
- Consumes: `Provenance` (Task 1), `SqlEffects` (Task 3).
- Produces:
  - `ProposeArtifactEdit(target_artifact: str, section_index: int, new_text: str, base_commit_sha: str, base_section_sha: str, provenance: Provenance, selection: Mapping[str, Any] | None = None)`.
  - `ProposeCitationInsert(section_index: int, base_commit_sha: str, base_section_sha: str, reference_id: uuid.UUID, anchor: str, chunk_index: int, evidence_quote: str, stance: str, confidence: str | None, provenance: Provenance)`.
  - `Effects.propose_artifact_edit(...) -> uuid.UUID`, `Effects.propose_citation_insert(...) -> uuid.UUID`.

- [ ] **Step 1: Write the failing tests**

First add these to the **existing import block at the top** of
`tests/proposal_assist/test_f3b_effects.py` — `ruff` selects the `E` rules, so
mid-file imports fail E402 and a `# noqa` here would be covering for the wrong fix:

```python
import uuid

from call_manager.db.models import ProposedChange
from call_manager.proposal_assist.ports.effects import (
    ProposeArtifactEdit,
    ProposeCitationInsert,
)
from call_manager.proposal_assist.ports.stamp import Provenance
```

(merge them into the existing `call_manager.db.models` and
`call_manager.proposal_assist.ports.effects` import statements rather than
repeating those module names — `ruff`'s `I` rules will otherwise reorder them.)

Then append the tests:

```python
# ------------------------------------------------------- the propose_* inserts

_PROV = Provenance(
    origin_kind="interactive_turn",
    origin_session_id="sess-9",
    origin_job_id=None,
    triggering_turn_id=None,
    triggering_prompt="tighten the aims",
    created_by=None,
)


def _edit(**over) -> ProposeArtifactEdit:
    base = dict(
        target_artifact="draft",
        section_index=1,
        new_text="new prose",
        base_commit_sha="a" * 40,
        base_section_sha="b" * 64,
        provenance=_PROV,
    )
    return ProposeArtifactEdit(**{**base, **over})


def test_propose_artifact_edit_inserts_a_pending_row(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=5)

    pc_id = SqlEffects(pg_session_factory).propose_artifact_edit(
        _stamp(ws_id, generation=5), _edit()
    )

    with pg_session_factory() as s:
        pc = s.get(ProposedChange, pc_id)
        assert (pc.workspace_id, pc.status, pc.kind) == (ws_id, "pending", "prose_edit")
        assert (pc.target_artifact, pc.section_index) == ("draft", 1)
        assert (pc.base_commit_sha, pc.base_section_sha) == ("a" * 40, "b" * 64)
        assert pc.payload == {"new_text": "new prose"}
        assert pc.created_at is not None


def test_propose_artifact_edit_copies_the_provenance_verbatim(pg_session_factory):
    """These fields are durable attribution. A snapshot taken late — or dropped
    on the way through the port — misattributes stored data."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)

    pc_id = SqlEffects(pg_session_factory).propose_artifact_edit(
        _stamp(ws_id, generation=1), _edit()
    )

    with pg_session_factory() as s:
        pc = s.get(ProposedChange, pc_id)
        assert pc.origin_kind == "interactive_turn"
        assert pc.origin_session_id == "sess-9"
        assert pc.triggering_prompt == "tighten the aims"
        assert pc.origin_job_id is None
        assert pc.created_by is None


def test_a_draft_edit_folds_the_selection_into_the_payload(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)

    pc_id = SqlEffects(pg_session_factory).propose_artifact_edit(
        _stamp(ws_id, generation=1),
        _edit(selection={"anchor": "aims", "from_offset": 10, "to_offset": 20}),
    )

    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).payload == {
            "new_text": "new prose",
            "selection_anchor": "aims",
            "selection_from": 10,
            "selection_to": 20,
        }


def test_a_spec_edit_ignores_the_selection(pg_session_factory):
    """Selection provenance is a draft-editor concept; the spec editor has none."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)

    pc_id = SqlEffects(pg_session_factory).propose_artifact_edit(
        _stamp(ws_id, generation=1),
        _edit(target_artifact="spec", selection={"anchor": "x", "from_offset": 1, "to_offset": 2}),
    )

    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).payload == {"new_text": "new prose"}


def test_propose_artifact_edit_refuses_a_stale_generation(pg_session_factory):
    """The INSERT case, which has no target row and therefore no READ COMMITTED
    re-check to mask a missing fence."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=5)
        bump_generation(s, ws_id, to=7)
        s.commit()

    with pytest.raises(WorkspaceGenerationStale) as e:
        SqlEffects(pg_session_factory).propose_artifact_edit(
            _stamp(ws_id, generation=5), _edit()
        )
    assert (e.value.expected, e.value.actual) == (5, 7)

    with pg_session_factory() as s:
        assert s.query(ProposedChange).count() == 0


def test_propose_artifact_edit_refuses_a_soft_deleted_workspace(pg_session_factory):
    from datetime import UTC, datetime

    from call_manager.db.models import Workspace

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=5)
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    with pytest.raises(WorkspaceGenerationStale):
        SqlEffects(pg_session_factory).propose_artifact_edit(
            _stamp(ws_id, generation=5), _edit()
        )
    with pg_session_factory() as s:
        assert s.query(ProposedChange).count() == 0


def test_propose_citation_insert_writes_the_accept_time_payload(pg_session_factory):
    """ev_id / citekey / offsets are minted authoritatively at ACCEPT time, not
    stored here — the payload mirrors the tool's arguments and nothing more."""
    ref_id = uuid.uuid4()
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=2)

    pc_id = SqlEffects(pg_session_factory).propose_citation_insert(
        _stamp(ws_id, generation=2),
        ProposeCitationInsert(
            section_index=3,
            base_commit_sha="c" * 40,
            base_section_sha="d" * 64,
            reference_id=ref_id,
            anchor="as shown",
            chunk_index=4,
            evidence_quote="the quote",
            stance="supports",
            confidence=None,
            provenance=_PROV,
        ),
    )

    with pg_session_factory() as s:
        pc = s.get(ProposedChange, pc_id)
        assert (pc.kind, pc.target_artifact, pc.status) == (
            "citation_insert",
            "draft",
            "pending",
        )
        assert pc.payload == {
            "reference_id": str(ref_id),
            "anchor": "as shown",
            "chunk_index": 4,
            "evidence_quote": "the quote",
            "stance": "supports",
            "confidence": None,
        }


def test_propose_citation_insert_refuses_a_stale_generation(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=2)
        bump_generation(s, ws_id, to=3)
        s.commit()

    with pytest.raises(WorkspaceGenerationStale):
        SqlEffects(pg_session_factory).propose_citation_insert(
            _stamp(ws_id, generation=2),
            ProposeCitationInsert(
                section_index=0,
                base_commit_sha="c" * 40,
                base_section_sha="d" * 64,
                reference_id=uuid.uuid4(),
                anchor="a",
                chunk_index=0,
                evidence_quote="q",
                stance="supports",
                confidence=None,
                provenance=_PROV,
            ),
        )
    with pg_session_factory() as s:
        assert s.query(ProposedChange).count() == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/proposal_assist/test_f3b_effects.py -q`
Expected: FAIL — `ImportError: cannot import name 'ProposeArtifactEdit'`

- [ ] **Step 3: Add the commands and the Protocol methods**

In `src/call_manager/proposal_assist/ports/effects.py`, add the two commands:

```python
@dataclass(frozen=True, slots=True)
class ProposeArtifactEdit:
    """A pending full-text replacement of ONE section.

    base_commit_sha / base_section_sha come from the agent's READ-TIME snapshot
    and are passed in by the caller. They must never be recomputed live: a human
    edit landed since the read would silently become the base, and accept would
    then clobber it.
    """

    target_artifact: str
    section_index: int
    new_text: str
    base_commit_sha: str
    base_section_sha: str
    provenance: Provenance
    #: Draft only. Folded into the payload as selection_anchor/_from/_to.
    selection: Mapping[str, Any] | None = field(default=None)


@dataclass(frozen=True, slots=True)
class ProposeCitationInsert:
    """A pending citation insert. ev_id, citekey and offsets are minted
    authoritatively at ACCEPT time, not stored here."""

    section_index: int
    base_commit_sha: str
    base_section_sha: str
    reference_id: uuid.UUID
    anchor: str
    chunk_index: int
    evidence_quote: str
    stance: str
    confidence: str | None
    provenance: Provenance
```

Add to the `Effects` Protocol:

```python
    def propose_artifact_edit(
        self, stamp: FenceStamp, cmd: ProposeArtifactEdit
    ) -> uuid.UUID: ...

    def propose_citation_insert(
        self, stamp: FenceStamp, cmd: ProposeCitationInsert
    ) -> uuid.UUID: ...
```

Add to `EFFECT_MECHANISMS`:

```python
        "propose_artifact_edit": "shared lease + INSERT … SELECT … WHERE live_at_generation",
        "propose_citation_insert": "shared lease + INSERT … SELECT … WHERE live_at_generation",
```

- [ ] **Step 4: Implement the two inserts**

In `src/call_manager/proposal_assist/port_impl/effects.py`, extend the imports:

```python
from sqlalchemy import exists, insert, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB

from call_manager.db.models import ProposedChange
from call_manager.proposal_assist.ports.effects import (
    ProposeArtifactEdit,
    ProposeCitationInsert,
    SubmitReview,
    SubmitScorecard,
    SubmitUncitedClaims,
)
from call_manager.proposal_assist.ports.stamp import FenceStamp, Provenance
```

and add to `SqlEffects`:

```python
    def _insert_proposed_change(
        self,
        stamp: FenceStamp,
        *,
        kind: str,
        target_artifact: str,
        section_index: int,
        base_commit_sha: str,
        base_section_sha: str,
        payload: dict[str, Any],
        provenance: Provenance,
    ) -> uuid.UUID:
        """The shared INSERT for both propose_* effects.

        INSERT … SELECT … WHERE, never INSERT … VALUES after a check: an INSERT
        has no target row, so there is no READ COMMITTED re-check to mask a
        missing fence, and the check-then-insert shape is the exact race
        reproduced on PostgreSQL 16.

        `id` is supplied explicitly because FROM SELECT does not apply
        Python-side column defaults. `status` and `created_at` are SERVER
        defaults and are correctly left out.
        """
        pc_id = uuid.uuid4()
        with self._sessions.begin() as session:
            acquire_shared_lease(session, stamp.workspace_id)
            landed = session.execute(
                insert(ProposedChange)
                .from_select(
                    [
                        "id",
                        "workspace_id",
                        "target_artifact",
                        "section_index",
                        "base_commit_sha",
                        "base_section_sha",
                        "kind",
                        "payload",
                        "origin_kind",
                        "origin_session_id",
                        "origin_job_id",
                        "triggering_turn_id",
                        "triggering_prompt",
                        "created_by",
                    ],
                    select(
                        literal(pc_id),
                        literal(stamp.workspace_id),
                        literal(target_artifact),
                        literal(section_index),
                        literal(base_commit_sha),
                        literal(base_section_sha),
                        literal(kind),
                        literal(payload, type_=JSONB),
                        literal(provenance.origin_kind),
                        literal(provenance.origin_session_id),
                        literal(provenance.origin_job_id),
                        literal(provenance.triggering_turn_id),
                        literal(provenance.triggering_prompt),
                        literal(provenance.created_by),
                    ).where(live_at_generation(stamp.workspace_id, stamp.generation)),
                )
                .returning(ProposedChange.id)
            ).first()
            if landed is None:
                _explain_refusal(session, stamp, checks=(), fallback=_PROPOSE_TARGET)
            return landed[0]

    def propose_artifact_edit(self, stamp: FenceStamp, cmd: ProposeArtifactEdit) -> uuid.UUID:
        payload: dict[str, Any] = {"new_text": cmd.new_text}
        if cmd.target_artifact == "draft" and cmd.selection:
            payload |= {
                "selection_anchor": cmd.selection.get("anchor"),
                "selection_from": cmd.selection.get("from_offset"),
                "selection_to": cmd.selection.get("to_offset"),
            }
        return self._insert_proposed_change(
            stamp,
            kind="prose_edit",
            target_artifact=cmd.target_artifact,
            section_index=cmd.section_index,
            base_commit_sha=cmd.base_commit_sha,
            base_section_sha=cmd.base_section_sha,
            payload=payload,
            provenance=cmd.provenance,
        )

    def propose_citation_insert(
        self, stamp: FenceStamp, cmd: ProposeCitationInsert
    ) -> uuid.UUID:
        return self._insert_proposed_change(
            stamp,
            kind="citation_insert",
            target_artifact="draft",
            section_index=cmd.section_index,
            base_commit_sha=cmd.base_commit_sha,
            base_section_sha=cmd.base_section_sha,
            payload={
                "reference_id": str(cmd.reference_id),
                "anchor": cmd.anchor,
                "chunk_index": cmd.chunk_index,
                "evidence_quote": cmd.evidence_quote,
                "stance": cmd.stance,
                "confidence": cmd.confidence,
            },
            provenance=cmd.provenance,
        )
```

with the new message constant beside the others:

```python
_PROPOSE_TARGET = "this workspace is no longer accepting proposed changes"
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_f3b_effects.py -q`
Expected: all pass (23 tests).

- [ ] **Step 6: Commit the green implementation**

Before mutating (GC-10). A `git checkout` here without this commit would restore
Task 3's version of the file and silently delete all of Task 4's work.

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/ports/effects.py src/call_manager/proposal_assist/port_impl/effects.py tests/proposal_assist/test_f3b_effects.py
git commit -m "feat(ports): the two propose_* INSERT effects (F3b Task 4)"
```

- [ ] **Step 7: Prove the INSERT fence is load-bearing**

Delete the `.where(live_at_generation(...))` clause from `_insert_proposed_change`.

Run: `uv run pytest tests/proposal_assist/test_f3b_effects.py -q -k propose`
Expected: FAIL `test_propose_artifact_edit_refuses_a_stale_generation` and
`test_propose_citation_insert_refuses_a_stale_generation`.

```bash
git checkout src/call_manager/proposal_assist/port_impl/effects.py
git status --porcelain   # MUST be empty — see GC-10
```

---

### Task 5: The two ordered lock cases, and the predicate-only regression

This is F3b's primary proof (spec §9). **Do not** write the straddle test revision 7
prescribed — pausing an effect mid-flight and committing a delete in the gap
**deadlocks a correct implementation**: the paused effect holds the shared lease, the
delete needs the exclusive lock, and each waits for the other. A test that can only
pass against a broken build is worse than no test.

The lease protocol is proven instead by **which side wins the lock, both directions**:

- **Case A — the effect acquires shared first.** The lifecycle transition blocks until
  the effect commits, then transitions. The effect's write **lands**, and that is
  correct: it was live when it ran. Case A is what proves the fence does not simply
  refuse everything.
- **Case B — the lifecycle acquires exclusive first.** The effect blocks; after the
  transition it acquires shared, evaluates its predicate against the **new**
  generation, and lands **nothing**. Case B is the one that matters.

**Assert the FIELD, not the row count.** Three of the five operations update an
existing row, so a stale write lands with the row count unchanged.

**Files:**
- Create: `tests/proposal_assist/test_f3b_lock_cases.py`

**Interfaces:**
- Consumes: everything from Tasks 1-4, plus `tests/proposal_assist/f3b_seeds.py`.
- Produces: nothing importable — this task is test-only.

- [ ] **Step 1: Write the test**

Create `tests/proposal_assist/test_f3b_lock_cases.py`:

```python
"""F3b — the two ORDERED LOCK CASES that prove the shared-lease protocol.

Not a straddle. Pausing an effect mid-flight and committing a lifecycle
transition in the gap deadlocks a CORRECT implementation: the paused effect
holds the shared lease and the transition needs the exclusive lock. What is
provable is which side wins the lock, in both directions.
"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import event, select, text

from call_manager.db.models import (
    EvaluatorJob,
    ProposedChange,
    ReviewRunItem,
    UncitedClaimsJob,
    Workspace,
)
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex
from call_manager.proposal_assist.port_impl.effects import SqlEffects
from call_manager.proposal_assist.ports.effects import (
    ProposeArtifactEdit,
    ProposeCitationInsert,
    SubmitReview,
    SubmitScorecard,
    SubmitUncitedClaims,
)
from call_manager.proposal_assist.ports.errors import WorkspaceGenerationStale
from call_manager.proposal_assist.ports.stamp import FenceStamp, Provenance
from tests.proposal_assist.f3b_seeds import (
    bump_generation,
    seed_evaluator_job,
    seed_review_item,
    seed_uncited_job,
    seed_workspace,
)

_G = 4  # the generation every case claims at
_NEXT = 5  # what the lifecycle transition moves it to

_PROV = Provenance(
    origin_kind="interactive_turn",
    origin_session_id=None,
    origin_job_id=None,
    triggering_turn_id=None,
    triggering_prompt=None,
    created_by=None,
)

EFFECT_NAMES = [
    "submit_scorecard",
    "submit_review",
    "submit_uncited_claims",
    "propose_artifact_edit",
    "propose_citation_insert",
]


def _build_case(session, effects: SqlEffects, name: str):
    """Seed one effect's rows and return (workspace_id, call, landed).

    `call()` runs the operation. `landed(session)` reports whether its write is
    durably present — by FIELD, never by row count: three of these five update an
    existing row, so a stale write leaves the count unchanged.

    Inner defs rather than lambdas: `ruff` selects the E rules and E731 forbids
    assigning a lambda to a name.
    """
    ws_id = seed_workspace(session, generation=_G)

    if name == "submit_scorecard":
        job_id = seed_evaluator_job(session, ws_id, generation=_G)
        stamp = FenceStamp(ws_id, _G, None, job_id)

        def call():
            effects.submit_scorecard(stamp, SubmitScorecard(scorecard={"ok": 1}))

        def landed(s):
            return s.get(EvaluatorJob, job_id).scorecard is not None

    elif name == "submit_review":
        run_id, item_id = seed_review_item(session, ws_id, generation=_G)
        stamp = FenceStamp(ws_id, _G, None, run_id)

        def call():
            effects.submit_review(
                stamp, SubmitReview(review_run_item_id=item_id, markdown="body")
            )

        def landed(s):
            return s.get(ReviewRunItem, item_id).raw_response_json is not None

    elif name == "submit_uncited_claims":
        job_id = seed_uncited_job(session, ws_id, generation=_G)
        stamp = FenceStamp(ws_id, _G, None, job_id)

        def call():
            effects.submit_uncited_claims(stamp, SubmitUncitedClaims(claims=[]))

        def landed(s):
            return s.get(UncitedClaimsJob, job_id).claims is not None

    elif name == "propose_artifact_edit":
        stamp = FenceStamp(ws_id, _G, None, None)

        def call():
            effects.propose_artifact_edit(
                stamp,
                ProposeArtifactEdit(
                    target_artifact="draft",
                    section_index=0,
                    new_text="t",
                    base_commit_sha="a" * 40,
                    base_section_sha="b" * 64,
                    provenance=_PROV,
                ),
            )

        def landed(s):
            return s.query(ProposedChange).filter_by(workspace_id=ws_id).count() == 1

    elif name == "propose_citation_insert":
        stamp = FenceStamp(ws_id, _G, None, None)

        def call():
            effects.propose_citation_insert(
                stamp,
                ProposeCitationInsert(
                    section_index=0,
                    base_commit_sha="a" * 40,
                    base_section_sha="b" * 64,
                    reference_id=uuid.uuid4(),
                    anchor="x",
                    chunk_index=0,
                    evidence_quote="q",
                    stance="supports",
                    confidence=None,
                    provenance=_PROV,
                ),
            )

        def landed(s):
            return s.query(ProposedChange).filter_by(workspace_id=ws_id).count() == 1

    else:  # pragma: no cover - guards a typo in EFFECT_NAMES
        raise AssertionError(f"unknown effect {name!r}")

    return ws_id, call, landed


@contextmanager
def _hold_the_lease_for(engine, seconds: float, signal: threading.Event):
    """Pause the effect's own thread immediately AFTER it wins the shared lease
    and while it still holds it, so the lifecycle transition provably queues
    behind it. Surgical: no production hook, and the pause is on the effect's
    connection inside its own transaction."""

    def _hook(conn, cursor, statement, params, context, executemany):
        if "pg_advisory_xact_lock_shared" in statement:
            signal.set()
            time.sleep(seconds)

    event.listen(engine, "after_cursor_execute", _hook)
    try:
        yield
    finally:
        event.remove(engine, "after_cursor_execute", _hook)


@pytest.mark.parametrize("name", EFFECT_NAMES)
def test_case_a_effect_first_the_write_lands_and_lifecycle_waits(
    name, pg_session_factory, migrated_engine
):
    """CASE A. The effect wins the lease; the transition blocks until it commits.
    The write LANDS — correct, it was live when it ran. Without this case the
    suite would pass an implementation that simply refuses everything."""
    effects = SqlEffects(pg_session_factory)
    with pg_session_factory() as s:
        ws_id, call, landed = _build_case(s, effects, name)

    lease_held = threading.Event()
    observed: dict = {}

    def _effect():
        call()

    def _lifecycle():
        lease_held.wait(timeout=10)
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id, timeout_seconds=20)
            # Ordering fact, not a timing artifact: we hold the EXCLUSIVE lock,
            # so if the effect's write is visible here it committed first.
            observed["effect_visible_when_lifecycle_won"] = landed(s)
            bump_generation(s, ws_id, to=_NEXT)
            s.commit()

    with _hold_the_lease_for(migrated_engine, 1.0, lease_held):
        threads = [threading.Thread(target=_effect), threading.Thread(target=_lifecycle)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

    assert observed["effect_visible_when_lifecycle_won"] is True
    with pg_session_factory() as s:
        assert landed(s) is True
        assert s.get(Workspace, ws_id).lifecycle_generation == _NEXT


@pytest.mark.parametrize("name", EFFECT_NAMES)
def test_case_b_lifecycle_first_the_effect_lands_nothing(name, pg_session_factory):
    """CASE B — the one that matters. The transition wins the exclusive lock; the
    effect blocks, then evaluates its predicate against the NEW generation and
    produces no semantic effect at all."""
    effects = SqlEffects(pg_session_factory)
    with pg_session_factory() as s:
        ws_id, call, landed = _build_case(s, effects, name)

    holder_ready = threading.Event()
    effect_starting = threading.Event()

    def _lifecycle():
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id, timeout_seconds=20)
            bump_generation(s, ws_id, to=_NEXT)
            holder_ready.set()
            effect_starting.wait(timeout=10)
            time.sleep(0.5)  # let the effect queue on the lease
            s.commit()

    t = threading.Thread(target=_lifecycle)
    t.start()
    try:
        assert holder_ready.wait(timeout=10)
        effect_starting.set()
        started = time.monotonic()
        with pytest.raises(WorkspaceGenerationStale) as e:
            call()
        waited = time.monotonic() - started
    finally:
        t.join(timeout=60)

    assert waited >= 0.4, "the effect did not block on the lease"
    assert (e.value.expected, e.value.actual) == (_G, _NEXT)
    with pg_session_factory() as s:
        assert landed(s) is False


def test_without_the_lease_a_generation_change_lands_a_stale_insert(pg_session_factory):
    """REGRESSION for the mechanism itself, not for any operation.

    live_at_generation is an exists() subquery taking NO lock on the workspace
    row, so under READ COMMITTED it evaluates against the statement's snapshot.
    An INSERT has no target row and therefore no re-check to mask that. Here the
    statement starts at generation G, a second connection commits G+2 while it
    sleeps, and the G-era row lands durably against a workspace now at G+2.

    This is WHY acquire_shared_lease exists. The same interleaving with the lease
    is Case B above, which lands nothing.
    """
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=7)

    pc_id = uuid.uuid4()
    predicate_only = text(
        """
        INSERT INTO proposed_changes
            (id, workspace_id, target_artifact, section_index, base_commit_sha,
             base_section_sha, kind, payload, origin_kind)
        SELECT :pc, :ws, 'draft', 0, 'a', 'b', 'prose_edit',
               '{}'::jsonb, 'interactive_turn'
        FROM (SELECT pg_sleep(1.5)) AS _pause
        WHERE EXISTS (
            SELECT 1 FROM workspaces
            WHERE id = :ws AND soft_deleted_at IS NULL AND lifecycle_generation = :gen
        )
        """
    )

    def _transition():
        time.sleep(0.4)  # inside the sleeping statement's lifetime
        with pg_session_factory() as s:
            bump_generation(s, ws_id, to=9)
            s.commit()

    t = threading.Thread(target=_transition)
    t.start()
    with pg_session_factory() as s:
        s.execute(predicate_only, {"pc": pc_id, "ws": ws_id, "gen": 7})
        s.commit()
    t.join(timeout=30)

    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id) is not None, (
            "the race did not reproduce — check that the transition committed "
            "DURING the sleeping statement, not before or after it"
        )
        assert s.execute(
            select(Workspace.lifecycle_generation).where(Workspace.id == ws_id)
        ).scalar() == 9
```

- [ ] **Step 2: Run the lock cases**

Run: `uv run pytest tests/proposal_assist/test_f3b_lock_cases.py -q`
Expected: `11 passed`. These are wall-clock tests (each Case A holds a lease for
1.0s, each Case B waits 0.5s); the file takes roughly 20 seconds on its own.

If Case A hangs rather than failing, the effect is upgrading the lease to the
exclusive mutex — GC-3's forbidden shape. Read the operation, do not raise the
join timeouts.

- [ ] **Step 3: Commit the green tests**

Before mutating (GC-10), so the restore below is a real revert.

```bash
uv run ruff check .
git add tests/proposal_assist/test_f3b_lock_cases.py
git commit -m "test(ports): the two ordered lock cases and the predicate-only regression (F3b Task 5)"
```

- [ ] **Step 4: Prove Case B fails without the lease**

Remove the `acquire_shared_lease(session, stamp.workspace_id)` line from
`submit_scorecard` in `port_impl/effects.py`.

Run: `uv run pytest tests/proposal_assist/test_f3b_lock_cases.py -q -k "case_b and scorecard"`
Expected: FAIL on `assert waited >= 0.4` — with no lease the effect never queues
behind the transition. This is the assertion that makes Case B a proof of the
*lease* rather than of the predicate alone.

```bash
git checkout src/call_manager/proposal_assist/port_impl/effects.py
git status --porcelain   # MUST be empty — see GC-10
```

---

### Task 6: The accounting port — owner-bound, deliberately unfenced

`2026-08-17` §2 permits and §7 *requires* these to land on a stale generation:
freeing an active-unique slot and refunding an admission slot must happen **after**
the workspace has moved on. Fencing them would strand jobs and destroy the deliberate
late token accounting at `review/pass_runner.py:164`.

"Exempt from the generation check" is not "exempt from atomicity" and not "exempt from
ownership." Ownership is load-bearing here **because** there is no generation
predicate: today `references.py:466` and `:502` target *whichever* turn is running for
the workspace, so after delete → restore → a new turn starts, an actor from the old
generation reserves or refunds **the new turn's** slot. The port binds both to
`stamp.active_turn_job_id`.

**Files:**
- Create: `src/call_manager/proposal_assist/ports/accounting.py`
- Create: `src/call_manager/proposal_assist/port_impl/accounting.py`
- Create: `tests/proposal_assist/test_f3b_accounting.py`

**Interfaces:**
- Consumes: `seed_workspace`, `seed_running_turn`, `bump_generation` (Task 3).
- Produces:
  - `MAX_FETCHES_PER_TURN = 3`.
  - `Accounting` Protocol: `reserve_fetch_slot(active_turn_job_id: uuid.UUID) -> int | None`, `refund_fetch_slot(active_turn_job_id: uuid.UUID) -> None`.
  - `SqlAccounting(session_factory)`.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_f3b_accounting.py`:

```python
"""F3b — accounting: owner-bound, atomic, and deliberately UNFENCED."""

from __future__ import annotations

import uuid

from call_manager.db.models import TurnJob
from call_manager.proposal_assist.port_impl.accounting import SqlAccounting
from call_manager.proposal_assist.ports.accounting import (
    MAX_FETCHES_PER_TURN,
    Accounting,
)
from tests.proposal_assist.f3b_seeds import (
    bump_generation,
    seed_running_turn,
    seed_workspace,
)


def test_the_implementation_satisfies_the_protocol(pg_session_factory):
    assert isinstance(SqlAccounting(pg_session_factory), Accounting)


def test_reserve_increments_and_returns_the_new_count(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        job_id = seed_running_turn(s, ws_id)

    acct = SqlAccounting(pg_session_factory)
    assert acct.reserve_fetch_slot(job_id) == 1
    assert acct.reserve_fetch_slot(job_id) == 2


def test_reserve_refuses_past_the_cap(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        job_id = seed_running_turn(s, ws_id)

    acct = SqlAccounting(pg_session_factory)
    for expected in range(1, MAX_FETCHES_PER_TURN + 1):
        assert acct.reserve_fetch_slot(job_id) == expected
    assert acct.reserve_fetch_slot(job_id) is None

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).fetch_count == MAX_FETCHES_PER_TURN


def test_refund_never_pushes_the_count_negative(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        job_id = seed_running_turn(s, ws_id)

    acct = SqlAccounting(pg_session_factory)
    acct.refund_fetch_slot(job_id)
    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).fetch_count == 0


def test_accounting_still_lands_on_a_stale_generation(pg_session_factory):
    """DELIBERATE. A stale actor must still release what it reserved; fencing
    this would strand the job holding its slot."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        job_id = seed_running_turn(s, ws_id)

    acct = SqlAccounting(pg_session_factory)
    acct.reserve_fetch_slot(job_id)

    with pg_session_factory() as s:
        bump_generation(s, ws_id, to=4)
        s.commit()

    acct.refund_fetch_slot(job_id)
    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).fetch_count == 0


def test_a_stale_actors_refund_cannot_touch_the_new_turns_slot(pg_session_factory):
    """The interleaving the ownership binding exists for.

    Reserve at generation G, delete and restore, start a NEW turn at G+1. The old
    actor's refund must not decrement the new turn's count. An implementation
    targeting `workspace_id AND status='running'` — what references.py does today
    — takes a slot straight out of the new turn.
    """
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        old_turn = seed_running_turn(s, ws_id)

    acct = SqlAccounting(pg_session_factory)
    assert acct.reserve_fetch_slot(old_turn) == 1

    with pg_session_factory() as s:
        # delete + restore: the running turn is terminalized, the generation
        # advances, and a new turn starts. Only one turn per workspace may be
        # 'running' (unique partial index), so the old one MUST be terminal.
        s.get(TurnJob, old_turn).status = "failed"
        bump_generation(s, ws_id, to=2)
        s.commit()
        new_turn = seed_running_turn(s, ws_id)
        s.get(TurnJob, new_turn).fetch_count = 2
        s.commit()

    acct.refund_fetch_slot(old_turn)
    acct.reserve_fetch_slot(old_turn)

    with pg_session_factory() as s:
        assert s.get(TurnJob, new_turn).fetch_count == 2, (
            "the stale actor's accounting reached the NEW turn's row"
        )
        assert s.get(TurnJob, old_turn).fetch_count == 1


def test_an_unknown_turn_job_is_a_no_op(pg_session_factory):
    acct = SqlAccounting(pg_session_factory)
    assert acct.reserve_fetch_slot(uuid.uuid4()) is None
    acct.refund_fetch_slot(uuid.uuid4())  # must not raise
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_f3b_accounting.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'call_manager.proposal_assist.ports.accounting'`

- [ ] **Step 3: Write the contract**

Create `src/call_manager/proposal_assist/ports/accounting.py`:

```python
"""Accounting commands — owner-bound and DELIBERATELY unfenced. Stdlib only.

Spec 3.3. Freeing an active-unique slot and refunding an admission slot must
happen AFTER the workspace has moved on, so these operations carry no generation
predicate on purpose. Fencing them would strand jobs holding their slots.

They are still atomic and still owned: each is ONE conditional statement bound to
the turn job that originated it, never "whichever turn is running for this
workspace". A read-modify-write (`job.fetch_count += 1`) is prohibited here as
everywhere — that defect shipped twice (28abbc4, 701fbb7).
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

#: Per-turn reference-fetch budget. Lives in the tool-facing contract so the
#: tool can name it in the message the agent sees without importing anything
#: that owns a session.
MAX_FETCHES_PER_TURN = 3


@runtime_checkable
class Accounting(Protocol):
    def reserve_fetch_slot(self, active_turn_job_id: uuid.UUID) -> int | None:
        """Atomic cap-check + increment. Returns the NEW count, or None when the
        turn is not running or the cap is already reached."""
        ...

    def refund_fetch_slot(self, active_turn_job_id: uuid.UUID) -> None:
        """Return a slot that was reserved but never spent on usable content."""
        ...
```

- [ ] **Step 4: Write the implementation**

Create `src/call_manager/proposal_assist/port_impl/accounting.py`:

```python
"""SqlAccounting — one conditional statement per operation, owner-bound.

No shared lease and no generation predicate, by design (spec 3.3). What replaces
the fence is OWNERSHIP: both statements target the turn job that originated the
work, so an actor from a previous generation cannot reserve or refund the slot of
a turn that started after the workspace moved on.
"""

from __future__ import annotations

import uuid

from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from call_manager.db.models import TurnJob
from call_manager.proposal_assist.ports.accounting import MAX_FETCHES_PER_TURN


class SqlAccounting:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._sessions = session_factory

    def reserve_fetch_slot(self, active_turn_job_id: uuid.UUID) -> int | None:
        with self._sessions.begin() as session:
            row = session.execute(
                update(TurnJob)
                .where(
                    TurnJob.id == active_turn_job_id,
                    TurnJob.status == "running",
                    TurnJob.fetch_count < MAX_FETCHES_PER_TURN,
                )
                .values(fetch_count=TurnJob.fetch_count + 1)
                .returning(TurnJob.fetch_count)
            ).first()
            return None if row is None else row[0]

    def refund_fetch_slot(self, active_turn_job_id: uuid.UUID) -> None:
        with self._sessions.begin() as session:
            session.execute(
                update(TurnJob)
                .where(
                    TurnJob.id == active_turn_job_id,
                    TurnJob.status == "running",
                    TurnJob.fetch_count > 0,
                )
                .values(fetch_count=TurnJob.fetch_count - 1)
            )
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_f3b_accounting.py -q`
Expected: `7 passed`

- [ ] **Step 6: Commit the green implementation**

Before mutating (GC-10): `port_impl/accounting.py` is new in this task, so a
`git checkout` of it would fail and leave the mutation in place.

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/ports/accounting.py src/call_manager/proposal_assist/port_impl/accounting.py tests/proposal_assist/test_f3b_accounting.py
git commit -m "feat(ports): owner-bound, deliberately unfenced accounting (F3b Task 6)"
```

- [ ] **Step 7: Prove the ownership binding is load-bearing**

In `port_impl/accounting.py`, replace `TurnJob.id == active_turn_job_id` in
**`refund_fetch_slot`** with the shape `references.py` uses today. To do that the
method needs the workspace, so temporarily resolve it from the id:

```python
                    TurnJob.workspace_id == select(TurnJob.workspace_id)
                    .where(TurnJob.id == active_turn_job_id)
                    .scalar_subquery(),
```
(keeping `TurnJob.status == "running"`), with `from sqlalchemy import select` added.

Run: `uv run pytest tests/proposal_assist/test_f3b_accounting.py -q -k stale_actors`
Expected: FAIL — `the stale actor's accounting reached the NEW turn's row`
(new turn's `fetch_count` is 1, not 2).

```bash
git checkout src/call_manager/proposal_assist/port_impl/accounting.py
git status --porcelain   # MUST be empty — see GC-10
```

---

### Task 7: The query port — detached DTOs

Reads are **not** fenced (spec §5): a read takes no lock and checks no generation. A
stale read that informs a durable write is caught by that write's own operation, which
re-checks the generation inside its own transaction — the only moment that matters.

Detachment is what is load-bearing here. An attached ORM instance carries
`object_session(instance)`, which is the full `Session` again — the exact leak that
defeated the forwarding-proxy design. So every method selects **columns**, never
entities, and returns a frozen dataclass.

The two methods are the reads `propose_citation_impl` performs today
(`citations.py:457` and `:466`). `ReferenceChunk.embedding` is a `Vector(1024)` and is
deliberately not exposed — the tool has no use for it and it would bloat every result.

**Files:**
- Create: `src/call_manager/proposal_assist/ports/queries.py`
- Create: `src/call_manager/proposal_assist/port_impl/queries.py`
- Create: `tests/proposal_assist/test_f3b_queries.py`

**Interfaces:**
- Produces:
  - `ReferenceView(id, workspace_id, citekey, title, is_tombstoned)` — frozen.
  - `ReferenceChunkView(reference_id, workspace_id, chunk_index, section_path, text)` — frozen.
  - `Queries` Protocol: `get_reference(workspace_id, reference_id) -> ReferenceView | None`, `get_reference_chunk(workspace_id, reference_id, chunk_index) -> ReferenceChunkView | None`.
  - `SqlQueries(session_factory)`.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_f3b_queries.py`:

```python
"""F3b — query ports return detached DTOs, never attached ORM instances."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.port_impl.queries import SqlQueries
from call_manager.proposal_assist.ports.queries import (
    Queries,
    ReferenceChunkView,
    ReferenceView,
)
from tests.proposal_assist.f3b_seeds import seed_workspace


def _seed_reference(session, ws_id, *, tombstoned=False) -> uuid.UUID:
    ref = Reference(
        workspace_id=ws_id,
        kind="paper",
        source_type="uploaded",  # ck_references_source_type: uploaded|crossref|pubmed|arxiv|web
        filename="p.pdf",
        storage_path=f"{ws_id}/p.pdf",
        mime_type="application/pdf",
        title="A Paper",
        citekey="doe2020",
        tombstoned_at=datetime.now(UTC) if tombstoned else None,
    )
    session.add(ref)
    session.flush()
    session.add(
        ReferenceChunk(
            reference_id=ref.id,
            workspace_id=ws_id,
            chunk_index=0,
            section_path="Intro",
            text="the chunk text",
            embedding=[0.0] * 1024,
            embedding_model="bge-m3",
        )
    )
    session.commit()
    return ref.id


def test_the_implementation_satisfies_the_protocol(pg_session_factory):
    assert isinstance(SqlQueries(pg_session_factory), Queries)


def test_get_reference_returns_a_frozen_dto(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=0)
        ref_id = _seed_reference(s, ws_id)

    view = SqlQueries(pg_session_factory).get_reference(ws_id, ref_id)
    assert view == ReferenceView(
        id=ref_id,
        workspace_id=ws_id,
        citekey="doe2020",
        title="A Paper",
        is_tombstoned=False,
    )
    assert dataclasses.is_dataclass(view)


def test_a_tombstoned_reference_is_reported_not_hidden(pg_session_factory):
    """The tool needs to tell "no such reference" from "tombstoned, cannot be
    cited" — they are different messages to the agent."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=0)
        ref_id = _seed_reference(s, ws_id, tombstoned=True)

    assert SqlQueries(pg_session_factory).get_reference(ws_id, ref_id).is_tombstoned


def test_get_reference_scopes_to_the_workspace(pg_session_factory):
    """The port takes the workspace_id rather than trusting the caller to compare
    it afterwards, so a cross-workspace read cannot be forgotten."""
    with pg_session_factory() as s:
        mine = seed_workspace(s, generation=0)
        theirs = seed_workspace(s, generation=0)
        their_ref = _seed_reference(s, theirs)

    assert SqlQueries(pg_session_factory).get_reference(mine, their_ref) is None


def test_get_reference_chunk_returns_the_text_without_the_embedding(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=0)
        ref_id = _seed_reference(s, ws_id)

    chunk = SqlQueries(pg_session_factory).get_reference_chunk(ws_id, ref_id, 0)
    assert chunk == ReferenceChunkView(
        reference_id=ref_id,
        workspace_id=ws_id,
        chunk_index=0,
        section_path="Intro",
        text="the chunk text",
    )
    assert not hasattr(chunk, "embedding")


def test_a_missing_chunk_is_none(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=0)
        ref_id = _seed_reference(s, ws_id)

    assert SqlQueries(pg_session_factory).get_reference_chunk(ws_id, ref_id, 99) is None


def test_no_query_result_carries_a_live_session(pg_session_factory):
    """GC-5. object_session() on an attached instance IS the full Session — the
    leak that defeated the forwarding-proxy design. A DTO carries data."""
    from sqlalchemy import inspect as sa_inspect

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=0)
        ref_id = _seed_reference(s, ws_id)

    q = SqlQueries(pg_session_factory)
    for result in (q.get_reference(ws_id, ref_id), q.get_reference_chunk(ws_id, ref_id, 0)):
        assert sa_inspect(result, raiseerr=False) is None, (
            f"{type(result).__name__} is an ORM-mapped object"
        )
        for value in dataclasses.asdict(result).values():
            assert sa_inspect(value, raiseerr=False) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_f3b_queries.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'call_manager.proposal_assist.ports.queries'`

- [ ] **Step 3: Write the contract**

Create `src/call_manager/proposal_assist/ports/queries.py`:

```python
"""Query DTOs and the Queries contract — stdlib only.

Spec 3.1. Every method opens its own read-only session and returns a FROZEN
dataclass, never an attached ORM instance: an attached instance carries
object_session(instance), which is the full Session again.

Reads are NOT fenced (spec 5). A stale read that informs a durable write is
caught by that write's operation, which re-checks the generation inside its own
transaction. The consequence is stated rather than engineered around: a tool can
read a workspace deleted microseconds later. The agent's WRITE will fail.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ReferenceView:
    id: uuid.UUID
    workspace_id: uuid.UUID
    citekey: str | None
    title: str | None
    #: Reported, not hidden: "no such reference" and "tombstoned, cannot be
    #: cited" are different messages to the agent.
    is_tombstoned: bool


@dataclass(frozen=True, slots=True)
class ReferenceChunkView:
    reference_id: uuid.UUID
    workspace_id: uuid.UUID
    chunk_index: int
    section_path: str | None
    text: str
    #: `embedding` is deliberately absent — a Vector(1024) the tool has no use for.


@runtime_checkable
class Queries(Protocol):
    def get_reference(
        self, workspace_id: uuid.UUID, reference_id: uuid.UUID
    ) -> ReferenceView | None: ...

    def get_reference_chunk(
        self, workspace_id: uuid.UUID, reference_id: uuid.UUID, chunk_index: int
    ) -> ReferenceChunkView | None: ...
```

- [ ] **Step 4: Write the implementation**

Create `src/call_manager/proposal_assist/port_impl/queries.py`:

```python
"""SqlQueries — column selects mapped into detached DTOs.

Never `select(Model)` and never `session.get(...)`: an entity result is an
attached instance, and handing one back would hand back its Session. Selecting
columns makes that mistake structurally impossible rather than merely discouraged.

Each method takes the workspace_id and scopes on it, rather than returning a row
for the caller to compare afterwards — a comparison the caller can forget.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.ports.queries import ReferenceChunkView, ReferenceView


class SqlQueries:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._sessions = session_factory

    def get_reference(
        self, workspace_id: uuid.UUID, reference_id: uuid.UUID
    ) -> ReferenceView | None:
        with self._sessions() as session:
            row = session.execute(
                select(
                    Reference.id,
                    Reference.workspace_id,
                    Reference.citekey,
                    Reference.title,
                    Reference.tombstoned_at,
                ).where(
                    Reference.id == reference_id,
                    Reference.workspace_id == workspace_id,
                )
            ).first()
        if row is None:
            return None
        return ReferenceView(
            id=row[0],
            workspace_id=row[1],
            citekey=row[2],
            title=row[3],
            is_tombstoned=row[4] is not None,
        )

    def get_reference_chunk(
        self, workspace_id: uuid.UUID, reference_id: uuid.UUID, chunk_index: int
    ) -> ReferenceChunkView | None:
        with self._sessions() as session:
            row = session.execute(
                select(
                    ReferenceChunk.reference_id,
                    ReferenceChunk.workspace_id,
                    ReferenceChunk.chunk_index,
                    ReferenceChunk.section_path,
                    ReferenceChunk.text,
                ).where(
                    ReferenceChunk.reference_id == reference_id,
                    ReferenceChunk.workspace_id == workspace_id,
                    ReferenceChunk.chunk_index == chunk_index,
                )
            ).first()
        if row is None:
            return None
        return ReferenceChunkView(
            reference_id=row[0],
            workspace_id=row[1],
            chunk_index=row[2],
            section_path=row[3],
            text=row[4],
        )
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_f3b_queries.py -q`
Expected: `7 passed`

- [ ] **Step 6: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/ports/queries.py src/call_manager/proposal_assist/port_impl/queries.py tests/proposal_assist/test_f3b_queries.py
git commit -m "feat(ports): the query port returns detached DTOs (F3b Task 7)"
```

---

### Task 8: The `Ports` bundle and the boundary test

Ports are an **injected bundle**, built by a factory and satisfying a `Protocol` —
**not module globals**. `effects._session_factory` would be reachable from any module
that can import `effects`, which is every tool, and that hands back exactly the
authority this design removes.

F3c installs the full import gate. This task installs the narrower check the ports
package must satisfy from the day it exists: **importing it pulls in no SQLAlchemy at
all.** That is a transitive check — a module can leak a session through an innocent-
looking project helper — so it runs in a fresh interpreter and inspects `sys.modules`.

**Files:**
- Modify: `src/call_manager/proposal_assist/ports/__init__.py`
- Modify: `src/call_manager/proposal_assist/port_impl/__init__.py`
- Create: `tests/proposal_assist/test_f3b_ports_boundary.py`

**Interfaces:**
- Produces: `Ports(queries, effects, accounting)` frozen dataclass;
  `build_ports(session_factory) -> Ports`.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_f3b_ports_boundary.py`:

```python
"""F3b — the ports boundary: stdlib-only contracts, injected implementations."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from call_manager.proposal_assist.port_impl import build_ports
from call_manager.proposal_assist.ports import Ports
from call_manager.proposal_assist.ports.accounting import Accounting
from call_manager.proposal_assist.ports.effects import Effects
from call_manager.proposal_assist.ports.queries import Queries

PORTS_DIR = Path(__file__).resolve().parents[2] / "src" / "call_manager" / "proposal_assist" / "ports"
PORT_IMPL_DIR = PORTS_DIR.parent / "port_impl"


def test_importing_the_ports_package_pulls_in_no_sqlalchemy():
    """Transitive, and therefore run in a FRESH interpreter. A source-level scan
    would miss a stdlib-looking project helper that imports a session two hops
    down; sys.modules cannot be fooled that way."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import call_manager.proposal_assist.ports as _p; "
            "import call_manager.proposal_assist.ports.effects; "
            "import call_manager.proposal_assist.ports.queries; "
            "import call_manager.proposal_assist.ports.accounting; "
            "import sys; print(sorted(m for m in sys.modules if m.split('.')[0] "
            "in {'sqlalchemy', 'psycopg', 'alembic'}))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]", (
        f"the ports package reached the database layer: {result.stdout.strip()}"
    )


def test_the_ports_package_never_names_the_implementation_package():
    """The dependency runs one way. A ports module importing port_impl would let
    a tool reach every session in the tree through a stdlib-looking name."""
    offenders = []
    for path in sorted(PORTS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for name in names:
                if "port_impl" in name or name.split(".")[0] in {"sqlalchemy", "psycopg"}:
                    offenders.append(f"{path.name}: {name}")
    assert not offenders, "\n  ".join(offenders)


def test_no_implementation_module_holds_a_session_factory_as_a_module_global():
    """Spec 4.3. `effects._session_factory` would be reachable from any module
    that can import effects — which, after F3d, is every tool."""
    import importlib

    offenders = []
    for path in sorted(PORT_IMPL_DIR.glob("*.py")):
        module = importlib.import_module(
            f"call_manager.proposal_assist.port_impl.{path.stem}"
            if path.stem != "__init__"
            else "call_manager.proposal_assist.port_impl"
        )
        for attr, value in vars(module).items():
            if isinstance(value, (sessionmaker, Engine, Session)):
                offenders.append(f"{path.name}::{attr}")
    assert not offenders, "\n  ".join(offenders)


def test_build_ports_returns_the_three_capabilities(pg_session_factory):
    ports = build_ports(pg_session_factory)
    assert isinstance(ports, Ports)
    assert isinstance(ports.queries, Queries)
    assert isinstance(ports.effects, Effects)
    assert isinstance(ports.accounting, Accounting)


def test_the_bundle_is_frozen(pg_session_factory):
    import dataclasses

    ports = build_ports(pg_session_factory)
    try:
        ports.effects = None  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("Ports is mutable — a tool could swap its own effects in")


def test_the_bundle_exposes_no_session(pg_session_factory):
    """GC-1, checked one level down: a tool holding Ports must not be able to
    walk to a session through a public attribute."""
    ports = build_ports(pg_session_factory)
    for capability in (ports.queries, ports.effects, ports.accounting):
        public = {
            name: getattr(capability, name)
            for name in dir(capability)
            if not name.startswith("_")
        }
        for name, value in public.items():
            assert not isinstance(value, (sessionmaker, Engine, Session)), (
                f"{type(capability).__name__}.{name} is a database handle"
            )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_f3b_ports_boundary.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_ports'`

- [ ] **Step 3: Write the bundle**

Replace the contents of `src/call_manager/proposal_assist/ports/__init__.py` with
its existing docstring plus:

```python
from __future__ import annotations

from dataclasses import dataclass

from call_manager.proposal_assist.ports.accounting import Accounting
from call_manager.proposal_assist.ports.effects import Effects
from call_manager.proposal_assist.ports.queries import Queries

__all__ = ["Ports"]


@dataclass(frozen=True, slots=True)
class Ports:
    """The injected bundle a tool holds instead of a session.

    An INJECTED bundle, never module globals: `effects._session_factory` would be
    reachable from any module that can import `effects`, which after F3d is every
    tool. Frozen, so a tool cannot swap in its own implementation.
    """

    queries: Queries
    effects: Effects
    accounting: Accounting
```

- [ ] **Step 4: Write the factory**

Append to `src/call_manager/proposal_assist/port_impl/__init__.py`:

```python
from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from call_manager.proposal_assist.port_impl.accounting import SqlAccounting
from call_manager.proposal_assist.port_impl.effects import SqlEffects
from call_manager.proposal_assist.port_impl.queries import SqlQueries
from call_manager.proposal_assist.ports import Ports

__all__ = ["build_ports"]


def build_ports(session_factory: sessionmaker) -> Ports:
    """Construct the capability bundle for one workspace's tool surface.

    Called by the adapter factories in F3d. Nothing calls it in F3b — the slice
    is additive and no production caller is converted.
    """
    return Ports(
        queries=SqlQueries(session_factory),
        effects=SqlEffects(session_factory),
        accounting=SqlAccounting(session_factory),
    )
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_f3b_ports_boundary.py -q`
Expected: `6 passed`

- [ ] **Step 6: Commit the green implementation**

Before mutating (GC-10): `ports/__init__.py` gains its bundle in this task, and
`port_impl/__init__.py` its factory.

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/ports/__init__.py src/call_manager/proposal_assist/port_impl/__init__.py tests/proposal_assist/test_f3b_ports_boundary.py
git commit -m "feat(ports): the injected Ports bundle and its boundary test (F3b Task 8)"
```

- [ ] **Step 7: Prove the boundary test bites**

Add `from sqlalchemy import select  # noqa: F401` to
`src/call_manager/proposal_assist/ports/queries.py`.

Run: `uv run pytest tests/proposal_assist/test_f3b_ports_boundary.py -q`
Expected: FAIL on **both** `test_importing_the_ports_package_pulls_in_no_sqlalchemy`
and `test_the_ports_package_never_names_the_implementation_package`. If only the
AST test fails, the subprocess check is not actually importing that module — fix it
before continuing, because it is the one that catches transitive leaks.

```bash
git checkout src/call_manager/proposal_assist/ports/queries.py
git status --porcelain   # MUST be empty — see GC-10
```

---

### Task 9: Correct the surfaces that document predicate-only as correct

No production caller uses them, so this is documentation and test debt rather than a
behaviour change — but they read as authoritative and would be followed.

**Two of the corrections already landed**, with the change that made them true:
Task 1 fixed the paragraph claiming `WorkspaceGenerationStale` lives in
`fencing.py`, and Task 2 rewrote the module header's "Two primitives … this is
the default". This task takes the rest.

**Scope note:** correcting only the "One statement" paragraph of the architecture
spec is not enough — the section header, its lead-in and §3.1's own opening line
each restate the claim, directly above it. Step 1 replaces the whole run.

**Files:**
- Modify: `docs/superpowers/specs/2026-08-17-lifecycle-fence-design.md` (§3 header through §3.1)
- Modify: `src/call_manager/proposal_assist/fencing.py` (the two function docstrings)
- Modify: `tests/fencing/inventory.py` (`SLICE_STATUS`)

- [ ] **Step 1: Correct the architecture spec — the whole of §3 and §3.1**

Replacing only the "One statement" paragraph leaves the section header saying
**"Two fence primitives"**, the lead-in saying **"Two mechanisms"**, and §3.1's
own opening saying **"with no new lock"** — three authoritative statements of the
thing being corrected, sitting directly above the correction.

In `docs/superpowers/specs/2026-08-17-lifecycle-fence-design.md`, replace
everything from the line `## 3. Two fence primitives` down to (and including) the
line `It is the default. Prefer it wherever the effect is DB-only.` with:

````markdown
## 3. Three fence primitives

Forcing every fence through the mutex would introduce contention and — as §5
shows — real deadlocks. Three mechanisms, and which ones an effect uses is
decided ONCE per operation and never changed mid-flight. The mechanism table
lives in `2026-08-18-fence-session-ownership-design.md` §3.5; this section
describes the primitives themselves.

### 3.1 Primitive A — conditional write (DB-only effects)

For an effect that is purely one or more DB rows, the fence is a predicate on the
**write statement itself**:

```sql
UPDATE/INSERT ... WHERE EXISTS (
    SELECT 1 FROM workspaces
    WHERE id = :ws AND soft_deleted_at IS NULL AND lifecycle_generation = :gen
)
```

One statement. This is the rule established in `28abbc4` and re-proved in
`701fbb7`: a predicate on a candidate SELECT with the write in a separate
statement does not guard the write, because each statement takes a fresh
READ COMMITTED snapshot.

**Primitive A is NOT sufficient on its own.** It is an `exists()` subquery and it
takes **no lock on the workspace row**, so under READ COMMITTED it evaluates
against the statement's snapshot — and a lifecycle transition committing after
that snapshot is invisible to it. Reproduced on PostgreSQL 16: a generation-G
`INSERT … SELECT … WHERE EXISTS(...)` qualifies, pauses inside the statement, a
second connection commits G+2, and the G-era row lands durably against a
workspace now at G+2. For an `UPDATE` the race is masked only because PostgreSQL
re-checks the `WHERE` against the updated row version when a concurrent
transaction touches the same target row, which cancel-on-delete does — and that
compensation is removed in step 3.

### 3.1a Primitive C — the shared lease

So a DB-only effect pairs Primitive A with `acquire_shared_lease` (F3b), acquired
**first**, in the same transaction: take the workspace's commit-mutex key in
SHARED mode, then perform the predicate-bearing write. Shared locks do not
conflict, so effects stay concurrent with each other; a lifecycle transition
needs the key exclusively, so it either waits for in-flight effects or wins first
and leaves the effect's predicate to fail. The predicate remains — it is what
turns "I waited" into "I checked".

Never take the shared lease and then attempt the exclusive mutex. Two operations
each holding shared and each waiting to upgrade is the one deadlock shape this
protocol has; an effect that will touch git or the filesystem takes Primitive B
first, before its first effect of any kind.
````

The `### 3.2 Primitive B — mutex + generation` heading and everything after it
stay exactly as they are.

- [ ] **Step 2: Correct `live_at_generation`'s docstring**

In `src/call_manager/proposal_assist/fencing.py`, replace the first two paragraphs
of `live_at_generation`'s docstring (from `"""Primitive A.` down to and including
the closing ``` of the code example) with:

```python
    """Primitive A. "Is this workspace LIVE and STILL at *generation*?", as a
    predicate that governs the WRITE.

    NOT SUFFICIENT ALONE. This is an exists() subquery and it takes NO LOCK on the
    workspace row, so under READ COMMITTED it evaluates against the statement's
    snapshot and a transition committing after that snapshot is invisible to it.
    Pair it with `acquire_shared_lease` (Primitive C), which must be acquired
    FIRST, in the same transaction:

        acquire_shared_lease(session, ws_id)
        session.execute(
            update(SomeJob)
            .where(SomeJob.id == job_id, live_at_generation(ws_id, gen))
            .values(status="done")
        )

    NOT as a separate check before the write. A predicate evaluated in its own
    statement does not guard a later one: both run in a single READ COMMITTED
    transaction and each takes a FRESH snapshot, so a transition committing
    between them is invisible to the check and fully effective on the write.
    That defect shipped twice (fixed in 28abbc4 and again in 701fbb7).
```

leaving the rest of the docstring (the `jobs_retirement._still_at_generation`
warning and the equality paragraph) exactly as it is.

- [ ] **Step 3: Correct `acquire_commit_mutex_at_generation`'s docstring**

Replace its second paragraph:

```python
    For effects spanning git, the filesystem, or multiple rows that must move
    together. For DB-only effects prefer `live_at_generation`, which takes no
    lock and therefore cannot create a lock-order problem.
```

with:

```python
    For effects spanning git, the filesystem, or multiple rows that must move
    together — and for those it is MANDATORY, taken before the first effect of
    any kind. For DB-only effects use `acquire_shared_lease` plus
    `live_at_generation` instead: those stay concurrent with each other, while
    this mutex serializes every writer for the workspace.

    Never call this while already holding the shared lease for the same
    workspace. Two operations each holding shared and each waiting to upgrade is
    the one deadlock shape this protocol has.
```

- [ ] **Step 4: Mark the slice complete**

In `tests/fencing/inventory.py`:

```python
    "F3b": "complete",  # the ports — no inventory row of its own; F3d converts callers
```

The inventory contains no `F3b` row: the tool sites it will serve are sliced `F3d`
and `F3e`, and they stay `fenced: false` until those slices convert them. The entry
records that the slice is done, and the status/flag consistency check is inert for it.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -n 16 --dist loadfile`
Expected: **`3608 + N passed, 1 skipped`** where N is the number of tests this branch
added (roughly 60). Read the `1 skipped` as the signal — it is the `ANTHROPIC_API_KEY`
test; anything else skipped means something got disabled.

- [ ] **Step 6: Commit**

```bash
uv run ruff check .
git add docs/superpowers/specs/2026-08-17-lifecycle-fence-design.md src/call_manager/proposal_assist/fencing.py tests/fencing/inventory.py
git commit -m "docs(fencing): the predicate alone is not the fence (F3b Task 9)"
```

---

## What F3b deliberately does NOT do

Named so a reviewer does not read them as gaps:

- **No caller is converted.** Every tool still takes `ctx.session_factory`. F3d
  converts them, module by module — it is a dependency migration, not five site
  conversions, because `tools/citations.py` holds other session-dependent helpers.
- **No import gate.** F3c installs the positive allowlist of §4, with negative
  fixtures, plus the recursive DTO contract test and a fixture reaching a resource
  locator. Task 8's boundary test is narrower on purpose: it constrains the new
  package only.
- **No resource locators removed.** `ToolContext.repo_dir` and `workspaces_dir` stay
  until F3c — removing them is what forces git and filesystem *reads* behind query
  ports, and that is F3c's scope.
- **No `WorkspaceGenerationStale` → tool-error mapping.** The conversion point is
  `_safe_tool` in `tools/registry.py`, whose bare `except Exception` currently turns
  any raise into a logged `"internal error"`. F3d adds the explicit clause, with the
  test that pins it, when the first tool can actually raise it.
- **No git or filesystem effect operations.** All five F3b effects are DB-only. The
  multi-resource mechanism arrives with the ingest pipeline in F3e.
- **No `require_live_generation` primitive.** §3.5 explains why a bare pre-check
  cannot exist, and this design does not invent one.

## Self-review notes

Checked against spec §7's F3b paragraph:

| Spec requirement | Task |
|---|---|
| `ports/queries.py`, `ports/effects.py`, `ports/accounting.py` | 7, 3-4, 6 |
| the DTOs and commands | 7, 3-4 |
| `FenceStamp` | 1 |
| the shared-lease helper of §3.5 | 2 |
| the exclusive `acquire_commit_mutex_at_generation` from F1 | already shipped in F1; Task 9 corrects its docstring |
| a table naming every effect operation and the mechanism it uses | 3 (`EFFECT_MECHANISMS` + its drift test) |
| each operation arrives with the two ordered lock cases | 5 |
| the three surfaces documenting predicate-only as correct | 1 (the exception's home), 2 (the module header), 9 (the spec section + both function docstrings) |

Two spec items are answered rather than implemented, and both are recorded above:
`acquire_commit_mutex_at_generation` already exists (F1, `7de5342`), and the
`WorkspaceGenerationStale` conversion point is confirmed — `_safe_tool`,
`tools/registry.py:319` — but wired in F3d, when a tool can first raise it.
