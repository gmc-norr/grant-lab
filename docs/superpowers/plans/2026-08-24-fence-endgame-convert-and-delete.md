# Fence Endgame: Convert the Callers, Delete the Trusting Primitive

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert every production caller of `acquire_commit_mutex_live` to `acquire_commit_mutex_at_generation`, then delete `acquire_commit_mutex_live` so the interpreter — not 4,993 lines of AST scanners — enforces the fence.

**Architecture:** The fence mechanism is already merged and unused. `acquire_commit_mutex_at_generation` exists at `src/call_manager/proposal_assist/fencing.py:115` with **zero** production callers; 18 sites still call the trusting `acquire_commit_mutex_live`. This plan closes that gap and removes the scaffolding that only existed to police it. The endgame is deletion: once the trusting primitive is gone, calling it is a `NameError`, and every lexical gate that checks "did this site take the right primitive" has nothing left to check.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, pytest (+ testcontainers Postgres), `uv`, `ruff`.

## Global Constraints

- Test command: `uv run --python 3.12 pytest -n 16 --dist loadfile`. Never `-n auto` (resolves to 72 workers here) and never `--dist loadgroup`.
- Baseline before this slice: **`3361 passed, 1 skipped`** (2026-08-16). The `1 skipped` is the signal, not the total — it is the real-SDK test gated on `ANTHROPIC_API_KEY`. `0 skipped` means a live API call ran.
- `ruff check` is the lint gate (not `ruff format`). Line limit 100.
- `pytest` exit code 4 means usage error, not test failure. Read the code, not just the tail.
- Lock order is **M → J → W** (mutex, then setup job, then workspace row). `acquire_commit_mutex_at_generation` acquires M. Calling it after a J or W row lock inverts the order against the delete path and opens a real Postgres 40P01.
- Where a caller **already holds** the workspace row `FOR UPDATE`, do not call the mutex at all — assert the generation on the already-locked row.
- Never call `acquire_commit_mutex_at_generation` while already holding the shared lease for the same workspace. That is the one deadlock shape this protocol has.
- Never hold the mutex across context assembly or an LLM call.
- **Generation 0 is real.** `Workspace.lifecycle_generation` has `server_default 0`. Test for `None`, never for falsiness.
- **The stamp is snapshotted at the access check, never re-read at the effect.** A stamp minted immediately before the write always matches and fences nothing. This is the single most important rule in this plan: a "converted" site that mints its own stamp locally is *worse* than an unconverted one, because it looks fenced and is not.

### Module map — read this before writing any import

The two primitives live in **different modules**, and getting this wrong produces a gate that passes for the wrong reason. Measured 2026-08-24:

| Symbol | Defined in |
|---|---|
| `acquire_commit_mutex` (raw) | `proposal_assist/drafting/locking.py:195` |
| `acquire_commit_mutex_live` (**the one being deleted**) | `proposal_assist/drafting/locking.py:257` |
| `acquire_commit_mutex_at_generation` (Primitive B) | `proposal_assist/fencing.py:115` |
| `acquire_shared_lease` (Primitive C) | `proposal_assist/fencing.py:174` |
| `live_at_generation` (**Primitive A** — a predicate, not the trusting mutex) | `proposal_assist/fencing.py` |

`fencing.py` does **not** define or re-export `acquire_commit_mutex_live`, and `locking.py` has no `__all__`. An absence check written against `fencing` therefore passes today, before any work is done — a hollow gate. Every absence assertion in this plan targets `drafting.locking`.

Also measured: importing `call_manager.proposal_assist.fencing` as the *first* import in a bare `python -c` raises a circular-import `ImportError`. Import `drafting.locking` first, or run assertions inside pytest where the app package is already loaded.

---

## Why This Slice Exists (measured, 2026-08-24)

| Claim | Measurement |
|---|---|
| Fence mechanism merged | `fencing.py` + `ports/*.py` = **843 lines** |
| Gates protecting it | `tests/fencing/*` = **4,993 lines** (2,446 scanner + 2,547 tests-of-scanners) |
| Sites on the trusting primitive | **18** |
| Sites on the checking primitive | **0** |
| Test files referencing the trusting primitive | **17 files, 54 references** |
| Real race tests that survive this slice | **2,144 lines** (`test_lifecycle_races.py`, `test_api_writers_mutex_straddle.py`, `test_lifecycle_generation.py`) |

Half the gate code tests the scanners, not the application. That half protects the protector. It exists only because two mutex primitives coexist. Delete one and it is dead code.

## What This Closes

An outside review (2026-08-24) found a hole that quarantine-style repair cannot close:

1. A request passes its ownership check at generation **G** and pauses.
2. Delete runs: generation → **G+1**.
3. Admin restores: generation → **G+2** (`restore_workspace_db`, `workspaces.py:331`).
4. The request resumes and takes the mutex.
5. `acquire_commit_mutex_live` reads only `soft_deleted_at`, which is now `NULL`. **The write lands.**

With Primitive B, step 5 compares `G != G+2` and raises `WorkspaceGenerationStale`. Verified against the implementation at `fencing.py:160-172`, which compares for **equality** (a `>=` would wave through exactly this corruption).

## Non-Goals

- **Quarantine (renaming the repo at delete).** Independent, cheap, and worth doing as defense-in-depth — but it cannot replace this slice, because closing the hole above requires a generation check *at the write*. Tracked in `docs/superpowers/specs/2026-08-24-workspace-delete-repair-design.md`, whose headline claim that quarantine replaces the fence is **superseded by this plan**.
- **Admin hard-delete.** Does not exist. `scrub_workspace_bytes` has zero production callers and does not remove the repo. A real gap, separately tracked, out of scope here.
- **Moving background work out of process** (Rust/Go supervisor, external workers). Measured and rejected for this problem: the LLM already runs as a killable, reapable subprocess, and 17 of the 18 unfenced sites are HTTP handlers and collab WebSocket edits, not the model.

---

## Site Inventory (measured — do not re-derive)

Produced by AST scan of `src/call_manager`, matching calls to `acquire_commit_mutex_live` and reporting the enclosing function's parameters.

### Group X — EXCLUDED, do not convert (1)

| Site | Function | Why |
|---|---|---|
| `workspaces.py:173` | `soft_delete_workspace_db(session, workspace_id, timeout_seconds)` | This **is** the delete path; it bumps the generation. Fencing it against a generation it is about to change is incoherent. Converts to raw `acquire_commit_mutex` in Task 6. |

### Group A — workspace object already a parameter (3)

| Site | Function |
|---|---|
| `proposal_assist/analysis/insert.py:355` | `insert_from_annotation(session, workspace, user_id, annotation, workspaces_dir)` |
| `proposal_assist/citations/human_cite.py:106` | `insert_citation_for_user(session, workspace, user_id, …)` |
| `proposal_assist/editing/save.py:183` | `save_section(session, workspace, user_id, section_index, …)` |

These receive a `workspace`. **That is not automatically sufficient** — Task 2 Step 1 verifies, per site, that the caller loaded it at the access check. If a caller loads it just before the call, threading a stamp from higher up is required instead.

### Group B — the `workspace_write` choke point (1 site, 6 routes)

| Site | Function | Fan-in |
|---|---|---|
| `proposal_assist/web/_writes.py:88` | `workspace_write(session, workspace_id, timeout_seconds, what)` | 6 callers: `web/api/rubric.py` ×3, `web/api/settings.py` ×3 |

One conversion covers six routes. **Trap:** `_writes.py` documents that tests shorten the mutex wait by replacing the module global with `functools.partial(..., timeout_seconds=1)`, and a call-time keyword *overrides* a partial's keyword. Do not pass `timeout_seconds` explicitly.

### Group C — routes with the workspace in local scope (3)

| Site | Function |
|---|---|
| `web/api/workspaces.py:540` | `_git()` — nested closure |
| `web/api/workspaces.py:651` | `post_lock_spec(workspace_id, request, user)` |
| `web/api/workspaces.py:739` | `post_unlock_spec(workspace_id, request, user)` |

### Group D — collab cluster, stamp must be threaded (8)

| Site | Function | Callers |
|---|---|---|
| `collab/kernel.py:136` | `apply_citation_to_room` | `citations/collab_cite.py:75`, `collab/kernel.py:386` |
| `collab/kernel.py:208` | `kernel_accept_prose` | `proposals/kernel_accept.py:85` |
| `collab/kernel.py:325` | `kernel_init_spec` | `citations/collab_cite.py:130` |
| `collab/kernel.py:369` | `kernel_accept_citation` | `proposals/kernel_accept.py:80` |
| `collab/kernel.py:443` | `apply_annotation_insert_to_room` | `citations/collab_cite.py:180` |
| `collab/checkpoint.py:91` | `checkpoint_artifact` | 6 callers: `web/api/workspaces.py` ×2, `collab/handover.py`, `collab/checkpoint.py:210`, `collab/kernel.py` ×2 |
| `collab/hydrate.py:58` | `load_or_hydrate` | 5 callers incl. `web/collab_ws.py:106` |
| `collab/flip.py:61` | `maybe_auto_enable` | `web/api/workspaces.py` ×2 |

These take `workspace_id`, not a workspace. Each needs a `stamp: FenceStamp` parameter threaded from its caller's access check.

### Group E — tool path, needs the invocation stamp (1)

| Site | Function |
|---|---|
| `proposal_assist/citations/core.py:556` | `_insert_citation_locked(ctx, session, ref_uuid, …)` |

`ctx` is a `ToolContext`. `ports/stamp.py` states explicitly: **never read the stamp from the retained ToolContext at the moment of the effect** — `set_turn_provenance` restamps that context every turn, so a late read reads a *newer* generation and the stale actor's write is fenced against the wrong generation. The stamp must come from `Invocation.stamp()` at dispatch.

**Totals: 1 excluded + 3 + 1 + 3 + 8 + 1 = 17 to convert.**

---

## File Structure

**Modified (conversion):**
- `src/call_manager/proposal_assist/analysis/insert.py`, `citations/human_cite.py`, `citations/core.py`, `editing/save.py`
- `src/call_manager/proposal_assist/web/_writes.py`, and its 6 callers in `web/api/rubric.py`, `web/api/settings.py`
- `src/call_manager/web/api/workspaces.py`
- `src/call_manager/proposal_assist/collab/{kernel,checkpoint,hydrate,flip,handover}.py`
- `src/call_manager/proposal_assist/citations/collab_cite.py`, `proposals/kernel_accept.py`, `web/collab_ws.py`

**Modified (deletion):**
- `src/call_manager/proposal_assist/drafting/locking.py:257` — remove `acquire_commit_mutex_live`. **It lives here, not in `fencing.py`** (see the module map above).
- `src/call_manager/workspaces.py` — delete path moves to raw `acquire_commit_mutex`, also from `drafting/locking.py`

**Deleted (dead scaffolding, Task 8 only):**
- `tests/fencing/live_mutex_gate.py`, `tests/fencing/test_live_mutex_gate.py`
- Whichever of `tests/fencing/*` Task 8 Step 1 proves are dead — **measured, not assumed**

**Untouched (real behavioral coverage, must stay green throughout):**
- `tests/proposal_assist/test_lifecycle_races.py`, `test_api_writers_mutex_straddle.py`, `tests/test_lifecycle_generation.py`

---

## Task 1: Prove the endgame closes the restore hole

The load-bearing claim of this plan. Written first, as a failing test, before any conversion.

**Files:**
- Test: `tests/proposal_assist/test_fence_restore_hole.py` (create)

**Interfaces:**
- Consumes: `acquire_commit_mutex_at_generation`, `WorkspaceGenerationStale` from `call_manager.proposal_assist.fencing`; `soft_delete_workspace_db`, `restore_workspace_db` from `call_manager.workspaces`
- Produces: nothing later tasks import. This is a pinned regression test.

- [ ] **Step 1: Write the failing test**

Two cases. Case A proves today's primitive lets the stale write through. Case B proves Primitive B stops it. Case A is what makes this test honest — without it, Case B could pass for the wrong reason.

```python
"""The delete-restore-resume hole (outside review, 2026-08-24).

An actor admitted at generation G can resume after a delete AND a restore have
both committed. `soft_deleted_at` is NULL again by then, so a liveness-only
check waves it through. Only a generation comparison catches it.
"""

import pytest

from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex_live
from call_manager.proposal_assist.fencing import (
    WorkspaceGenerationStale,
    acquire_commit_mutex_at_generation,
)
from call_manager.workspaces import restore_workspace_db, soft_delete_workspace_db


def _generation(session, ws_id) -> int:
    from sqlalchemy import select

    from call_manager.db.models import Workspace

    return session.execute(
        select(Workspace.lifecycle_generation).where(Workspace.id == ws_id)
    ).scalar_one()


def test_live_primitive_admits_the_stale_writer_after_restore(
    session_factory, seeded_workspace, admin_user
):
    """Case A: the hole, demonstrated. This is why the slice exists."""
    ws_id = seeded_workspace.id
    with session_factory() as s:
        admitted_at = _generation(s, ws_id)

    with session_factory() as s:
        soft_delete_workspace_db(s, workspace_id=ws_id)
    with session_factory() as s:
        restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_user.id)

    with session_factory() as s:
        assert _generation(s, ws_id) != admitted_at, (
            "delete+restore must move the generation, or this test proves nothing"
        )
        # The stale actor wakes up. The trusting primitive lets it write.
        acquire_commit_mutex_live(s, ws_id)  # does NOT raise — that is the hole


def test_generation_primitive_refuses_the_stale_writer_after_restore(
    session_factory, seeded_workspace, admin_user
):
    """Case B: the fix. Same interleaving, Primitive B, refused."""
    ws_id = seeded_workspace.id
    with session_factory() as s:
        admitted_at = _generation(s, ws_id)

    with session_factory() as s:
        soft_delete_workspace_db(s, workspace_id=ws_id)
    with session_factory() as s:
        restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_user.id)

    with session_factory() as s:
        with pytest.raises(WorkspaceGenerationStale):
            acquire_commit_mutex_at_generation(s, ws_id, generation=admitted_at)
```

- [ ] **Step 2: Fix the fixture names against the real conftest**

The fixture names above (`session_factory`, `seeded_workspace`, `admin_user`) are a guess. Do **not** leave them guessed.

Run: `grep -rn "^def \|^@pytest.fixture" tests/proposal_assist/conftest.py tests/conftest.py | head -40`

Replace with the real fixture names and signatures. If no workspace fixture exists, follow the pattern in `tests/proposal_assist/test_lifecycle_races.py` — it already sets up workspaces for exactly this kind of race.

- [ ] **Step 3: Run the test**

Run: `uv run --python 3.12 pytest tests/proposal_assist/test_fence_restore_hole.py -v`

Expected: **both PASS.** Case A passes because the hole is real today. Case B passes because Primitive B already works — it just has no callers. If Case A *fails* (the live primitive raised), stop: either restore does not bump the generation, or the delete path already fences this. Report before continuing — the premise of the whole plan is wrong.

- [ ] **Step 4: Commit**

```bash
git add tests/proposal_assist/test_fence_restore_hole.py
git commit -m "test(fence): pin the delete-restore-resume hole both ways"
```

---

## Task 2: Convert Group A — the three workspace-object sites

**Files:**
- Modify: `src/call_manager/proposal_assist/analysis/insert.py:355`, `citations/human_cite.py:106`, `editing/save.py:183`
- Test: the existing suites covering each (found in Step 3)

**Interfaces:**
- Consumes: `stamp_for_workspace` from `call_manager.proposal_assist.ports.stamp`; `acquire_commit_mutex_at_generation` from `fencing`
- Produces: nothing new. Signatures unchanged if Step 1 passes; a new `stamp` parameter if it does not.

- [ ] **Step 1: Verify the workspace came from the access check — per site**

For each of the three, find every caller and answer one question: **was `workspace` loaded at the permission check, or right before this call?**

```bash
grep -rn "insert_from_annotation(\|insert_citation_for_user(\|save_section(" src/ --include=*.py | grep -v "^src/call_manager/proposal_assist/\(analysis/insert\|citations/human_cite\|editing/save\).py:.*def "
```

For each caller, read from the route entry down to the call. If the workspace is loaded at the access check and passed straight through, Group A is genuinely easy. If any caller loads it late, that site moves to the Group D pattern (thread a `stamp` parameter) — record which, and say so in the report.

**Write the answer down before editing anything.** This is the rule that decides whether a conversion is real or cosmetic.

- [ ] **Step 2: Convert each site**

For a site whose Step 1 answer was "loaded at the access check":

```python
# Before:
acquire_commit_mutex_live(session, workspace.id)

# After:
stamp = stamp_for_workspace(workspace)
acquire_commit_mutex_at_generation(session, stamp.workspace_id, generation=stamp.generation)
```

Add the import: `from call_manager.proposal_assist.ports.stamp import stamp_for_workspace`.

Leave every surrounding comment intact — several document lock-order and timeout traps that still apply.

- [ ] **Step 3: Find and run the covering tests**

```bash
grep -rln "insert_from_annotation\|insert_citation_for_user\|save_section" tests/ --include=*.py
```

Run exactly those files:

`uv run --python 3.12 pytest <the files listed> -v`

Expected: all pass. A test that fails with `WorkspaceGenerationStale` is a **finding, not a nuisance** — it means that test's fixture builds a stamp at a different generation than the write, which is the very staleness this slice exists to catch. Report it; do not paper over it by re-minting the stamp near the write.

- [ ] **Step 4: Prove the conversion by breaking it**

For one converted site, temporarily change the generation passed to a wrong value:

```python
acquire_commit_mutex_at_generation(session, stamp.workspace_id, generation=stamp.generation + 1)
```

Re-run that site's tests. Expected: **failures.** If everything still passes, the tests do not exercise this path and the conversion is unproven — say so in the report. Revert the mutation.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(fence): convert the three workspace-object sites to Primitive B"
```

---

## Task 3: Convert Group B — the `workspace_write` choke point

One conversion, six routes.

**Files:**
- Modify: `src/call_manager/proposal_assist/web/_writes.py:88`
- Modify: `src/call_manager/web/api/rubric.py` (3 call sites), `src/call_manager/web/api/settings.py` (3 call sites)
- Test: `tests/proposal_assist/test_workspace_write.py`, `tests/proposal_assist/test_api_writers_mutex_straddle.py`

**Interfaces:**
- Consumes: `FenceStamp` from `ports.stamp`
- Produces: `workspace_write(session, *, stamp: FenceStamp, timeout_seconds: int, what: str)` — the `workspace_id` parameter is replaced by `stamp`, since `stamp.workspace_id` carries it. Later tasks do not depend on this.

- [ ] **Step 1: Read the whole docstring first**

Run: `sed -n '55,110p' src/call_manager/proposal_assist/web/_writes.py`

It documents three traps that this task must not break: the rollback-on-every-exceptional-exit contract, the `WorkspaceDeleted` / `HTTPException` passthrough, and the `functools.partial` timeout trap. All three still apply after conversion.

- [ ] **Step 2: Change the signature and the acquisition**

```python
# Signature: workspace_id: uuid.UUID  ->  *, stamp: FenceStamp
# Body:
        try:
            acquire_commit_mutex_at_generation(
                session, stamp.workspace_id, generation=stamp.generation
            )
        except CommitMutexTimeout as e:
            raise _busy() from e
```

**Do not pass `timeout_seconds`** — not even from a parameter defaulting to 10. The docstring explains why: tests replace the module global with a `functools.partial` carrying `timeout_seconds=1`, and a call-time keyword overrides a partial's keyword, silently restoring the production 10s and making every wedged-mutex test wait ten seconds.

Also update the `f"…{what} transaction"` message construction if it referenced `workspace_id`.

- [ ] **Step 3: Update the six callers**

```bash
grep -n "workspace_write(" src/call_manager/web/api/rubric.py src/call_manager/web/api/settings.py
```

Each route already runs an access check that loads the workspace. Mint the stamp there — at the check — and pass it in:

```python
stamp = stamp_for_workspace(ws)
with workspace_write(session, stamp=stamp, timeout_seconds=2, what="rubric"):
    ...
```

If a route does **not** load a workspace object at its access check, stop and report it. Do not load one next to the `workspace_write` call to satisfy the type — that produces a stamp that always matches and fences nothing.

- [ ] **Step 4: Run the covering tests**

Run: `uv run --python 3.12 pytest tests/proposal_assist/test_workspace_write.py tests/proposal_assist/test_api_writers_mutex_straddle.py -v`

Expected: pass. `test_api_writers_mutex_straddle.py` is 1,604 lines and patches the live primitive by name — expect to update those patch targets to `acquire_commit_mutex_at_generation`. Note in the report how many patch sites changed.

- [ ] **Step 5: Prove the timeout trap is still respected**

Confirm the partial-patching tests still shorten the wait rather than taking 10s:

Run: `uv run --python 3.12 pytest tests/proposal_assist/test_workspace_write.py -v --durations=10`

Expected: no test in the file takes ~10s. A ~10s duration means `timeout_seconds` leaked into the call and the trap fired.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(fence): convert workspace_write to Primitive B (covers 6 routes)"
```

---

## Task 4: Convert Group C — the three `workspaces.py` route sites

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py:540`, `:651`, `:739`
- Test: `tests/proposal_assist/collab/test_collab_spec_lock.py`, plus whatever Step 2 finds

- [ ] **Step 1: Read each route from its access check to its write**

Run: `sed -n '500,560p;630,700p;715,780p' src/call_manager/web/api/workspaces.py`

For each, identify the line where the access check loads the workspace. That is where the stamp is minted. `_git()` at :540 is a nested closure — the stamp must be captured from the enclosing route scope, not minted inside the closure.

- [ ] **Step 2: Find the covering tests**

```bash
grep -rln "post_lock_spec\|post_unlock_spec\|/lock-spec\|/unlock-spec" tests/ --include=*.py
```

- [ ] **Step 3: Convert all three**

Mint at the access check, use at the write:

```python
stamp = stamp_for_workspace(ws)          # at the access check
...
acquire_commit_mutex_at_generation(session, stamp.workspace_id, generation=stamp.generation)
```

`post_lock_spec` and `post_unlock_spec` also call `checkpoint_artifact` (`:665`, `:746`), which Task 5 converts. Leave those calls alone in this task; Task 5 adds the `stamp` argument.

- [ ] **Step 4: Run the tests found in Step 2**

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(fence): convert the three workspaces.py route sites to Primitive B"
```

---

## Task 5: Convert Group D — the collab cluster

The largest task: 8 sites, each needing a `stamp` parameter threaded from its caller. Convert **inner to outer** so each step leaves the tree importable.

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/{checkpoint,hydrate,flip,kernel,handover}.py`
- Modify callers: `citations/collab_cite.py`, `proposals/kernel_accept.py`, `web/collab_ws.py`, `web/api/workspaces.py`
- Test: `tests/proposal_assist/collab/*`

**Interfaces:**
- Produces: each converted function gains a keyword-only `stamp: FenceStamp` parameter and drops `workspace_id` where `stamp.workspace_id` replaces it. Exact signatures are settled in Step 1 and recorded in the report so later steps do not guess.

- [ ] **Step 1: Write the signature table first**

Before editing, list all 8 functions with their current and target signatures, and the caller that supplies the stamp. Record it in the report file. Later steps reference this table instead of re-deriving it.

The callers, already measured:

| Function | Supplied by |
|---|---|
| `checkpoint_artifact` | `web/api/workspaces.py` ×2, `collab/handover.py:40`, `collab/checkpoint.py:210`, `collab/kernel.py:174`, `:294` |
| `load_or_hydrate` | `collab_cite.py` ×3, `kernel_accept.py:65`, `web/collab_ws.py:106` |
| `maybe_auto_enable` | `web/api/workspaces.py:285`, `:334` |
| `apply_citation_to_room` | `collab_cite.py:75`, `kernel.py:386` |
| `kernel_accept_prose` | `kernel_accept.py:85` |
| `kernel_init_spec` | `collab_cite.py:130` |
| `kernel_accept_citation` | `kernel_accept.py:80` |
| `apply_annotation_insert_to_room` | `collab_cite.py:180` |

- [ ] **Step 2: Convert the leaves — `checkpoint_artifact`, `load_or_hydrate`, `maybe_auto_enable`**

Add `*, stamp: FenceStamp` and convert the acquisition. Update in-tree callers (`kernel.py:174`, `:294`, `checkpoint.py:210`, `handover.py:40`) to pass the stamp they already hold or receive.

- [ ] **Step 3: Run the collab suite**

Run: `uv run --python 3.12 pytest tests/proposal_assist/collab/ -v`

Expected: pass. Fix before continuing — do not stack two conversions on a red suite.

- [ ] **Step 4: Convert the five `kernel.py` entry points**

Each gains `*, stamp: FenceStamp`.

- [ ] **Step 5: Thread the stamp from the WebSocket and route callers**

`collab_cite.py`, `kernel_accept.py`, `web/collab_ws.py` must mint the stamp at **their** access check. For the WebSocket path, that is the connection's authorization — `collab/ws_auth.py`. Read it before choosing the mint point:

Run: `sed -n '1,60p' src/call_manager/proposal_assist/collab/ws_auth.py`

A long-lived socket is exactly the actor this fence is for: it authorizes once at generation G and can still be sending edits after a delete. Mint at authorization; do not re-mint per message.

**If minting at authorization turns out to require restructuring beyond this task, stop and report.** Do not fall back to minting per message — that is the cosmetic conversion this plan exists to avoid.

- [ ] **Step 6: Run the collab suite plus the race suites**

Run: `uv run --python 3.12 pytest tests/proposal_assist/collab/ tests/proposal_assist/test_lifecycle_races.py -v`

Expected: pass.

- [ ] **Step 7: Prove the socket path by breaking it**

In `ws_auth.py` (or wherever Step 5 mints), pass `generation=stamp.generation + 1`. Re-run `tests/proposal_assist/collab/`. Expected: failures. If green, the socket path has no test coverage for this — report it as a gap. Revert.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor(fence): convert the collab cluster to Primitive B"
```

---

## Task 6: Convert Group E, fix the delete path, delete the trusting primitive

The moment the slice is for.

**Files:**
- Modify: `src/call_manager/proposal_assist/citations/core.py:556`
- Modify: `src/call_manager/workspaces.py:173`
- Modify: `src/call_manager/proposal_assist/drafting/locking.py` — delete `acquire_commit_mutex_live` (**defined here, not in `fencing.py`**)

- [ ] **Step 1: Convert `_insert_citation_locked` using the invocation stamp**

Run: `cat src/call_manager/proposal_assist/ports/invocation.py`

The stamp must come from `Invocation.stamp()` at dispatch — **not** from the retained `ToolContext`. `ports/stamp.py` documents why: `set_turn_provenance` restamps that context every turn, so a late read reads a newer generation and the stale actor's write is fenced against the wrong generation.

Thread the stamp from the tool dispatch boundary down to `_insert_citation_locked`.

- [ ] **Step 2: Move the delete path to the raw mutex**

`soft_delete_workspace_db` must not be fenced against a generation it is about to change. Replace:

```python
acquire_commit_mutex_live(session, workspace_id, timeout_seconds=timeout_seconds)
```

with:

```python
acquire_commit_mutex(session, workspace_id, timeout_seconds=timeout_seconds)
```

No new import is needed. `workspaces.py:31-34` already imports **both** names from `drafting.locking`:

```python
from call_manager.proposal_assist.drafting.locking import (
    acquire_commit_mutex,
    acquire_commit_mutex_live,
)
```

Drop `acquire_commit_mutex_live` from that list and keep `acquire_commit_mutex`.

The liveness check `_live` performed is not lost: delete's own transaction re-reads and terminalizes the row. Confirm that by reading the function body before editing:

Run: `sed -n '128,232p' src/call_manager/workspaces.py`

If delete relied on `_live` raising `WorkspaceDeleted` for double-delete, preserve that behaviour explicitly rather than dropping it.

- [ ] **Step 3: Confirm zero callers remain**

```bash
grep -rn "acquire_commit_mutex_live" src/ --include=*.py
```

Expected: **only the definition in `drafting/locking.py:257`.** Any other hit means a site was missed — convert it before continuing.

- [ ] **Step 4: Delete the function**

Remove `acquire_commit_mutex_live` from **`src/call_manager/proposal_assist/drafting/locking.py:257`** — not `fencing.py`, which never defined it. `locking.py` has no `__all__`, so there is no export list to update. Leave `acquire_commit_mutex` at `:195` in place; the delete path now uses it.

Then update any module docstring in `locking.py` or `fencing.py` that describes a two-primitive world. Note that **`live_at_generation` is "Primitive A"** — that name does *not* refer to the function being deleted, so do not delete text about Primitive A by mistake.

- [ ] **Step 5: Prove the interpreter is now the gate**

The absence assertion must target `drafting.locking`. Written against `fencing` it passes today, before any work — a hollow gate.

Prove the gate is real by running it **before** the deletion:

```bash
uv run --python 3.12 python -c "
from call_manager.proposal_assist.drafting import locking
print('present:', hasattr(locking, 'acquire_commit_mutex_live'))
"
```

Expected **before** Step 4: `present: True`. Expected **after** Step 4: `present: False`.

If it prints `False` before the deletion, the assertion is not measuring what you think — stop and report. **A gate that cannot fail proves nothing**; this one is what replaces `tests/fencing/live_mutex_gate.py` (280 lines) and its 140-line test, so it has to be real.

- [ ] **Step 6: Update the 54 test references**

```bash
grep -rn "acquire_commit_mutex_live" tests/ --include=*.py
```

17 files, 54 references at plan time. Most are patch targets that become `acquire_commit_mutex_at_generation`. Files in `tests/fencing/` are handled in Task 8 — do not repair a gate here that Task 8 deletes. Check that list first.

- [ ] **Step 7: Run the full suite**

Run: `uv run --python 3.12 pytest -n 16 --dist loadfile`

Expected: `1 skipped`, and a pass total at or above baseline minus the tests intentionally removed. **Read `0 skipped` as "a live API call just ran"**, not as success.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor(fence): delete acquire_commit_mutex_live — the interpreter is the gate now"
```

---

## Task 7: Re-prove the restore hole is closed

Task 1's Case A asserted the hole exists. It cannot exist any more.

**Files:**
- Modify: `tests/proposal_assist/test_fence_restore_hole.py`

- [ ] **Step 1: Convert Case A into a permanent proof of absence**

Case A called `acquire_commit_mutex_live`, which no longer exists, so it will now fail on import. Replace it:

```python
def test_the_trusting_primitive_no_longer_exists():
    """The hole is closed by absence, not by a rule someone must remember.

    Case A of this file used to demonstrate the hole with acquire_commit_mutex_live.
    That function is gone. This is what replaces the lexical gates.

    TARGET `drafting.locking`, where the function was actually defined. Written
    against `fencing` this assertion passes on an UNMODIFIED tree, because
    fencing.py never defined or re-exported it — a gate that cannot fail.
    """
    from call_manager.proposal_assist.drafting import locking

    assert not hasattr(locking, "acquire_commit_mutex_live")
    # The raw mutex stays: the delete path uses it.
    assert hasattr(locking, "acquire_commit_mutex")
```

Keep Case B exactly as written — it still passes and now proves the live behaviour of the only remaining primitive.

- [ ] **Step 2: Run it**

Run: `uv run --python 3.12 pytest tests/proposal_assist/test_fence_restore_hole.py -v`

Expected: both pass.

- [ ] **Step 3: Commit**

```bash
git add tests/proposal_assist/test_fence_restore_hole.py
git commit -m "test(fence): the hole is closed by absence"
```

---

## Task 8: Delete the dead scaffolding

Only now, and only what is **measured** dead.

**Files:**
- Delete: files proven dead in Step 1
- Modify: `tests/fencing/inventory.py` if pins reach zero

- [ ] **Step 1: Measure what is dead — do not assume**

For each file in `tests/fencing/`, answer: does it check a property the interpreter now enforces?

```bash
wc -l tests/fencing/*.py | sort -rn
grep -rn "acquire_commit_mutex_live\|_MUTEX_PRIMITIVES\|live_mutex" tests/fencing/*.py | cut -d: -f1 | sort | uniq -c | sort -rn
```

Certain to be dead — its entire subject is gone:
- `tests/fencing/live_mutex_gate.py` (280) + `tests/fencing/test_live_mutex_gate.py` (140)

Judgement required, decided by the question "does this check something the missing function no longer allows?":
- `sink_guard.py` (696) + test (109) — checks acquisition-before-effect *ordering*. Ordering still matters with one primitive. **Likely survives in reduced form.**
- `git_fence.py` (516) + test (435) — catalogues git sinks. Still useful as a new-site regression guard. **Likely survives.**
- `stamp_gate.py` (80) + test (81) — "exactly two constructors build a FenceStamp". A real invariant, not a migration artifact. **Survives.**
- `import_gate.py` (237) + test (450) — ports purity, unrelated to the two primitives. **Survives.**
- `inventory.py` (357) + `test_inventory_merge.py` (183) — the exemption pins. **Dies when pins reach zero; check.**
- `test_f3e1a_slice.py` (104), `test_f3e1a2_slice.py` (150) — historical slice gates for slices now complete. **Likely retire.**
- `detectors.py` (243) + test (371), `test_fence_coverage.py` (524) — decide by the same question.

Record the verdict and reason for **each file** in the report before deleting anything.

- [ ] **Step 2: Delete only the files judged dead, one commit per group**

```bash
git rm tests/fencing/live_mutex_gate.py tests/fencing/test_live_mutex_gate.py
uv run --python 3.12 pytest tests/fencing/ -v
```

Expected: pass. A failure means something imported the deleted module — that is information about what was actually load-bearing. Report it.

- [ ] **Step 3: Run the full suite**

Run: `uv run --python 3.12 pytest -n 16 --dist loadfile`

Expected: `1 skipped`. Record the new pass total and the net line delta:

```bash
git diff --stat $(git merge-base main HEAD) HEAD -- tests/fencing/
```

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore(fence): delete the scaffolding the interpreter replaced"
```

---

## Task 9: Record what changed

**Files:**
- Modify: `docs/superpowers/specs/2026-08-24-workspace-delete-repair-design.md`
- Modify: `CLAUDE.md` if it describes the fence programme

- [ ] **Step 1: Mark the quarantine spec superseded**

Add a header note: its headline claim — that quarantine can replace the fence — was disproved by the delete-restore-resume hole. Quarantine remains valid as defense-in-depth. Point at this plan.

- [ ] **Step 2: Record the two real gaps this slice does not close**

- **Admin hard-delete does not exist.** `scrub_workspace_bytes` has zero production callers and does not remove the repo.
- **Reference ingestion runs on an unkillable thread**, and APScheduler's in-process single-instance constraint blocks scaling the web tier. Neither is a delete/restore correctness problem; both are real.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "docs(fence): supersede the quarantine spec; record the remaining gaps"
```

---

## Definition of Done

- [ ] `grep -rn "acquire_commit_mutex_live" src/` returns **nothing**
- [ ] `python -c "from call_manager.proposal_assist.drafting import locking; assert not hasattr(locking,'acquire_commit_mutex_live')"` exits 0 — and the same line **failed** on the pre-slice tree
- [ ] `uv run --python 3.12 pytest -n 16 --dist loadfile` reports **`1 skipped`**
- [ ] `uv run --python 3.12 ruff check src tests` is clean
- [ ] Every conversion was proven by mutation, or its lack of coverage was reported
- [ ] Every `tests/fencing/` file was given a keep/delete verdict **with a reason**
- [ ] The net line delta across `src/` and `tests/fencing/` is recorded in the final report
