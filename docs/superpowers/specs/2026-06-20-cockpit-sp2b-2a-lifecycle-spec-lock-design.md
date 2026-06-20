# Cockpit SP2b-2a — Two-mode lifecycle + spec lock — Design

> Sub-project 2 (cockpit), slice SP2b-2a. The first of three slices that SP2b-2
> decomposes into: **2a** (this doc — lifecycle migration + spec lock), then
> **2b** (agent ↔ spec propose/accept), then **2c** (coherence-keeper background
> session). Built on SP2b-1 (artifact-aware foundation + Spec tab), merged to
> master `dd36994`.

## Goal

Replace the dormant five-value `current_phase` lifecycle with the real
two-mode `{setup, active}` model from the cockpit design (§3), and make the
**Spec lockable**: an owner can freeze the spec so it becomes the stable,
agreed-upon basis for downstream work (plan/draft generation, coherence
checks) and cannot be edited — by a human or, later, by an agent — until it is
explicitly unlocked.

## Why now

SP2b-1 made the Spec a real, editable artifact. The next slices (2b/2c) let an
agent *propose changes to the spec* and a background keeper *react to spec
changes*. Both need a well-defined "the spec is settled" state to anchor on.
The lock is that anchor, and the lifecycle migration clears the dead
five-phase scaffolding that would otherwise confuse the two-mode model.

## Non-goals (explicit scope fence)

- **No agent↔spec propose/accept** — that is SP2b-2b. `proposals/accepting.py`
  keeps its `target_artifact != "draft"` guard for now.
- **No coherence-keeper / evaluator background sessions** — SP2b-2c / SP2b-3.
- **No Scorecard / Readiness / mock-panel** — SP2b-3.
- **No `phase_approvals` rework** — the table stays dead and untouched; its
  retire-or-repurpose decision belongs to the submission-readiness work.
- **No lock-history UI** — the audit table is for provenance/debugging and the
  future Scorecard, not a v1 screen.

---

## Section 1 — Schema + data migration

### Model changes (`src/call_manager/db/models.py`)

- **`Workspace.current_phase`** — CHECK constraint
  `('setup','spec','plan','draft','review')` → `('setup','active')`. Column type
  (`Text`) and `server_default="setup"` unchanged. Constraint name stays
  `ck_workspaces_current_phase` (dropped + recreated).
- **`Workspace.spec_locked_at`** — new, nullable `timestamptz`. `NULL` ⇒
  unlocked.
- **`Workspace.spec_locked_by`** — new, nullable FK `users.id`
  `ON DELETE SET NULL` (a deleted locker must not orphan the workspace row).
- **`SpecLockEvent`** — new model / table `spec_lock_events`:
  - `id` — uuid PK
  - `workspace_id` — FK `workspaces.id` `ON DELETE CASCADE`
  - `action` — `Text`, CHECK `('lock','unlock')`, constraint
    `ck_spec_lock_events_action`
  - `actor_id` — FK `users.id` `ON DELETE SET NULL` (audit row survives a
    deleted user)
  - `artifact_commit_sha` — `Text`, the spec HEAD frozen (lock) or released
    (unlock)
  - `created_at` — `timestamptz`, server default `now()`
  - index `ix_spec_lock_events_ws_created` on `(workspace_id, created_at DESC)`
- **`PhaseApproval`** — left exactly as-is (dead model: nothing reads or writes
  it). Not migrated, not dropped.

### Alembic migration `0052_workspace_two_mode_lifecycle_and_spec_lock`

Down-revision: the current head **`0051_proposed_changes`** (the worktree
already carries `0050_drop_magic_link_tokens` and `0051_proposed_changes` from
SP2a — using `0050` would fork the history). Upgrade order matters — the old
CHECK must be **dropped before** the data update, because `'active'` is not yet
a permitted value and the UPDATE would otherwise violate the still-present
constraint:

1. **Drop** `ck_workspaces_current_phase` (the old `('setup',…,'review')`
   constraint) — clears the way for the new value.
2. **Data migrate** the rows now that no CHECK blocks `'active'`:
   ```sql
   UPDATE workspaces
   SET current_phase = 'active'
   WHERE current_phase IN ('spec','plan','draft','review');
   ```
   Every non-`setup` workspace lands `active`, and the two new columns are
   `NULL`, so **all existing workspaces migrate to active + unlocked**.
3. **Recreate** `ck_workspaces_current_phase` as `('setup','active')`.
4. Add `spec_locked_at`, `spec_locked_by` (both NULL on existing rows).
5. Create `spec_lock_events` (table + CHECK + index).

**Downgrade is explicitly lossy** (documented in a module comment): the old
four-way distinction among `spec/plan/draft/review` is unrecoverable. Downgrade
maps `active → 'draft'` (best-effort), drops `spec_lock_events`, drops the two
columns, and restores the old five-value CHECK.

### Code touch-ups in the same slice

- `proposal_assist/web/rubric_review.py:147` — post-setup
  `ws.current_phase = "spec"` → `"active"`.
- `proposal_assist/web/rubric_review.py:153` — redirect guard
  `if ws.current_phase == "spec":` → `== "active"`.
- `web/filters.py:82` — the Jinja `current_phase` human-label filter (legacy
  HTMX workspace page) → label map for `{setup, active}`.
- `proposal_assist/web/workspaces.py:605` and `:779` — the two
  `if ws.current_phase == "setup":` guards are **unchanged**; `setup` persists.

---

## Section 2 — Endpoints + enforcement + read-model

### Endpoints (`web/api/workspaces.py`)

Both are **owner-gated** and follow the existing `/spec` status conventions
(`require_owner` runs before any file-precheck 404). Both run their mutation
under the **per-workspace commit mutex**
(`acquire_commit_mutex(session, workspace_id)`), so they cannot race a
concurrent spec save.

**Refresh the workspace row after acquiring the mutex.** `require_owner` loads
the `Workspace` *before* the handler waits on the mutex; a concurrent
lock/unlock could commit during that wait, leaving the SQLAlchemy identity map
showing a stale `spec_locked_at`. So inside the critical section — after
`acquire_commit_mutex` succeeds — call `session.refresh(ws, ["spec_locked_at",
"spec_locked_by"])` before reading lock state to decide real-transition vs.
no-op. This makes the idempotency check (below) read committed truth, not a
stale snapshot.

- **`POST /api/workspaces/{id}/spec/lock`**
  - Precondition: the spec file must have non-whitespace content. Whitespace-only
    → **409** `"cannot lock an empty spec"`.
  - Idempotent on state: already locked (per the refreshed row) → return current
    lock state, write **no** new `SpecLockEvent`.
  - Real transition: set `spec_locked_at = now()`, `spec_locked_by = user.id`,
    pin `artifact_commit_sha = current_head(spec_repo_dir)`, insert one
    `SpecLockEvent(action='lock')`. Commit (releases the mutex).
- **`POST /api/workspaces/{id}/spec/unlock`**
  - Idempotent on state: already unlocked → no-op, no event row.
  - Real transition: null `spec_locked_at` and `spec_locked_by`, insert one
    `SpecLockEvent(action='unlock', artifact_commit_sha=current_head)` (records
    what was released). Commit.

Status map: `CommitMutexTimeout → 429` + `Retry-After`; non-owner `→ 403`;
unknown workspace `→ 404`.

### Core enforcement guard (`proposal_assist/editing/save.py`)

`save_section` already receives the `workspace` object and acquires the commit
mutex itself (`save.py:173`). Because the caller loads `workspace` *before*
`save_section` waits on that mutex, a concurrent lock can commit during the
wait and leave the identity-map row stale. So the guard must read a **freshly
refreshed** row from *inside* the critical section — after the mutex is held,
**after** `artifact = Artifact(artifact)` normalization:

```python
acquire_commit_mutex(session, workspace.id)   # already present at save.py:173
session.refresh(workspace, ["spec_locked_at"])  # re-read under the lock
if artifact is Artifact.SPEC and workspace.spec_locked_at is not None:
    raise SpecLockedError(workspace.id)
```

`save_section` is the chokepoint for the **edit** paths that flow through it:
- the human `/spec` save route,
- and (later) the SP2b-**2b** accept-to-spec path (`proposals/accepting.py`
  replays `save_section` as the named approver).

Both honour the lock with no extra code. The draft path is structurally
unaffected (`artifact is Artifact.DRAFT` never trips the guard).

**`post_init_spec` is the exception** — it does *not* call `save_section`; it
writes the skeleton directly via `write_and_commit_files`
(`web/api/workspaces.py:351`). It therefore needs its **own explicit guard**:
after acquiring the mutex and refreshing the workspace, if
`spec_locked_at is not None`, raise `SpecLockedError` (→ 409) instead of
seeding. (In practice the empty-spec precondition means a locked spec is rarely
also re-seedable, but the guard is required for correctness, and the locked-init
→ 409 test pins it.)

`SpecLockedError` is a new exception in the save/locking module. Routes that
hit it for the spec map `SpecLockedError → 409` `"spec is locked — unlock to
edit"`.

### Read-model (`SpecOut`)

`SpecOut` (the GET `/spec` response) gains:
- `locked: bool`
- `locked_at: datetime | None`
- `locked_by: str | None` — display name/email of the locker, for the banner
- `can_edit: bool` = `is_owner AND not locked` — the editor's existing
  `readOnly` path keys off an editability flag
- `can_manage_lock: bool` = `is_owner` — drives whether the Lock/Unlock control
  renders

Lock **state** is visible to all members (a collaborator sees the banner and a
read-only editor); only the owner can **toggle** it.

---

## Section 3 — Frontend (minimal)

Lean on SP2b-1's existing editor; the editor already has a read-only path keyed
off an editability flag, so locking is mostly wiring new fields, not new editor
work.

- **`SpecPanel`** reads the new lock fields from the `["spec", id]` query.
  - Locked → render a banner above the editor: **🔒 Spec locked by
    {locked_by} on {date}**. Calm-clinical styling via a `.spec-lock-banner`
    rule in `frontend/src/styles/cockpit.css` (matching the existing
    `.spec-empty` treatment).
  - Editor goes read-only via the existing `can_edit=false → readOnly` path —
    the same mechanism the non-owner view already uses. No new CodeMirror
    config.
- **Owner Lock/Unlock control** (rendered only when `can_manage_lock`):
  - Unlocked → **Lock spec** button; disabled (with a hint) when the spec is
    whitespace-only, mirroring the backend 409 precondition so the user never
    hits the error path for the empty case.
  - Locked → **Unlock spec** button.
  - Hooks `useLockSpec(id)` / `useUnlockSpec(id)` each `POST` and on success
    **invalidate `["spec", id]`**, so banner, editability, and button all
    re-derive from one refetch (single source of truth — no optimistic local
    lock state to drift).

Scope guard (YAGNI): no lock-history UI, no "ask someone to unlock" affordance,
no draft-side indicator. Frontend surface is exactly: banner + read-only + one
toggle button.

---

## Section 4 — Testing strategy + risks

### Migration test (highest stakes — touches every workspace)
- Seed workspaces at each old phase; run `0052` upgrade; assert
  `spec/plan/draft/review → active`, `setup` unchanged, all land
  `spec_locked_at IS NULL`.
- Assert the new CHECK rejects an old value (`INSERT ... current_phase='draft'`
  raises).
- Assert `spec_lock_events` exists with its `action` CHECK.

### Lock/unlock endpoint tests
- Owner-gated: non-owner → 403, fired before any existence probe.
- Empty-spec precondition: lock on whitespace-only spec → 409, **no** event row.
- Idempotency: double-lock writes no second row; double-unlock same. The
  no-op decision reads the **mutex-refreshed** row, so a lock that committed
  while this request waited on the mutex is observed (no duplicate event).
- Happy path: lock writes one `action='lock'` row with
  `artifact_commit_sha == current spec HEAD`; unlock writes one
  `action='unlock'` row.

### Enforcement tests
- Locked spec → `/spec` save → 409; `post_init_spec` re-seed → 409.
- **Draft unaffected** while spec is locked (key isolation assertion).
- unlock → spec editable again.

### `save_section` unit tests (the chokepoint, tested directly)
- `SpecLockedError` raised for `artifact=SPEC` + locked.
- **Not** raised for `artifact=DRAFT` + locked.
- **Not** raised for `artifact=SPEC` + unlocked.

### GET /spec read-model test
- Lock fields present; `can_edit` reflects owner-AND-unlocked; `can_manage_lock`
  reflects ownership.

### Frontend tests (Vitest + jsdom)
- Locked → banner shown + editor read-only.
- Owner sees toggle; member does not.
- Lock button disabled for whitespace-only spec.
- `useLockSpec` invalidates `["spec", id]`.

### Risk — lock/save race
Two requests (lock + spec-save, or two locks) interleave. The handlers load the
`Workspace` *before* waiting on the mutex, so the identity-map row can be stale
by the time the mutex is granted. Mitigated structurally: both take the
per-workspace commit mutex, and **after acquiring it** the code calls
`session.refresh(workspace, ["spec_locked_at", …])` before reading lock state —
so the guard and the idempotency check both see committed truth, not a stale
snapshot. Worst case the loser fails safe (save → 409, or the second lock
no-ops). No corruption path; no lost update; no duplicate audit row.

---

## File-touch summary

| File | Change |
|------|--------|
| `db/models.py` | CHECK `setup/active`; `spec_locked_at`, `spec_locked_by`; `SpecLockEvent` |
| `alembic/versions/0052_*.py` | down_revision `0051`; drop CHECK → data-migrate → recreate CHECK → add cols → create table; lossy downgrade |
| `proposal_assist/editing/save.py` | `SpecLockedError` + the spec-lock guard, reading a `session.refresh`-ed row under the mutex |
| `web/api/workspaces.py` | `POST /spec/lock`, `POST /spec/unlock` (refresh-under-mutex idempotency); explicit lock guard in `post_init_spec`; `SpecOut` lock fields + `can_edit`/`can_manage_lock`; `SpecLockedError → 409` mapping |
| `proposal_assist/web/rubric_review.py` | `"spec"` → `"active"` (×2) |
| `web/filters.py` | `current_phase` label map → `{setup, active}` |
| `frontend/src/.../SpecPanel.tsx` | lock banner + read-only wiring + Lock/Unlock control |
| `frontend/src/.../hooks` | `useLockSpec`, `useUnlockSpec` |
| `frontend/src/styles/cockpit.css` | `.spec-lock-banner` |
| tests | migration, endpoints, enforcement, `save_section` unit, read-model, frontend |
