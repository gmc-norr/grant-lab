# Collab Document Epoch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A collab client that reconnects holding a Y.Doc from before a branch switch is refused with close code 4409 before it can send its state, so the old branch's text can never be merged into the new one.

**Architecture:** Every `collab_artifact_state` row gets a `doc_epoch` counter that `reset_checkpoint` bumps (a truncated log is a new document). The room learns its epoch from the same mutex-held read as the log, tells every client in a new wire frame (type 4) sent before STEP1, and the client presents it back as `?epoch=N` on every reconnect. The route compares before STEP1 and closes 4409 on mismatch.

**Tech Stack:** Python 3.12, FastAPI/Starlette WebSocket, SQLAlchemy 2 + Alembic, pycrdt (`write_var_uint`), pytest + testcontainers Postgres; TypeScript (`lib0/encoding`), vitest.

**Spec:** `docs/superpowers/specs/2026-09-02-collab-doc-epoch-design.md` (commit `b992e8c2`). Read §3.3 before Task 6 and §6 before Task 9.

## Global Constraints

- Work in a worktree: `.worktrees/doc-epoch` on branch `feat/collab-doc-epoch` from `master`. **Every Bash call starts with `cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch &&`** — the shell's cwd resets between turns and a patch that lands in the main tree gives a hollow green.
- Python 3.12 (`.python-version` is pinned; `uv run python --version` must print 3.12.x before trusting any test result).
- Tests: `uv run pytest -n 16 --dist loadfile` for the full suite; targeted runs plain `uv run pytest <path>`. Never `-n auto`, never `--dist loadgroup`.
- Lint: `uv run ruff check src tests && uv run ruff format --check src tests` before every commit. Line length 100.
- `docs/ARCHITECTURE.md` is generated. After the model change run `uv run python scripts/gen_architecture.py` and commit the regenerated file in the SAME commit. Never hand-edit it. `tests/architecture/test_architecture_map_is_current.py` fails otherwise.
- Wire constants: `MESSAGE_DOC_EPOCH = 4`, frame `[varuint 4][varuint epoch]`. Close codes: malformed `?epoch=` → 1008; mismatch → 4409 (`WS_DOCUMENT_REPLACED`).
- Log event on refusal: logger name `collab.stale_doc_refused`, level INFO, `extra["event"] == "collab_stale_doc_refused"`.
- No REST/OpenAPI change. No frontend dependency change (`pnpm add` is blocked by the 7-day cooldown anyway).
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Write the message to a file and use `git commit -F <file>` (a heredoc containing `EOF` breaks).
- No git remote exists. Never push. Merge is the user's call.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `alembic/versions/0069_collab_doc_epoch.py` (new) | add `doc_epoch BIGINT NOT NULL DEFAULT 0` | 1 |
| `src/call_manager/db/models.py` | `CollabArtifactState.doc_epoch` column | 1 |
| `docs/ARCHITECTURE.md` (regenerated) | column count 5 → 6 | 1 |
| `tests/test_migration_0069_collab_doc_epoch.py` (new) | column exists, NOT NULL, defaults 0 | 1 |
| `src/call_manager/proposal_assist/collab/state.py` | `get_doc_epoch`; `reset_checkpoint` bumps + creates row | 2 |
| `src/call_manager/proposal_assist/collab/store.py` | docstring cross-reference only | 2 |
| `tests/proposal_assist/collab/test_collab_state.py` | epoch accessor + bump tests | 2 |
| `tests/proposal_assist/test_branch_switch.py` | switch bumps both artifacts | 2 |
| `tests/proposal_assist/collab/test_collab_kill_switch_handover.py` | handover bumps | 2 |
| `src/call_manager/proposal_assist/collab/protocol.py` | `MESSAGE_DOC_EPOCH`, `create_doc_epoch_message`, `read_doc_epoch_message` | 3 |
| `tests/proposal_assist/collab/test_collab_protocol.py` | round-trip + bounds | 3 |
| `tests/proposal_assist/collab/test_collab_room.py` | `Room.handle` drops inbound frame 4 | 3 |
| `src/call_manager/proposal_assist/collab/hydrate.py` | `Hydration` list subclass carrying `doc_epoch` | 4 |
| `tests/proposal_assist/collab/test_collab_hydrate.py` | warm / cold / flag-off epoch | 4 |
| `src/call_manager/proposal_assist/collab/room.py` | `Room.doc_epoch`; manager records it at both cold-start sites | 5 |
| `tests/proposal_assist/collab/test_collab_room_operate.py` | manager records epoch; warm join keeps it; plain list → 0 | 5 |
| `src/call_manager/proposal_assist/web/collab_ws.py` | parse `?epoch=`, refuse mismatch, send frame 4 | 6 |
| `tests/proposal_assist/collab/test_collab_ws_route.py` | the reproduction refused; frame 4 then STEP1; malformed → 1008 | 6 |
| `frontend/src/collab/collabProvider.ts` | `docEpoch`, frame 4 handling, `?epoch=` on reconnect | 7 |
| `frontend/src/collab/collabProvider.test.ts` | spec tests 9–11 | 7 |
| scratch `mutate_doc_epoch.sh` (not committed) | mutation proof, spec test 8 | 8 |

---

### Task 0: Worktree

**Files:** none in the repo.

- [ ] **Step 1: Create the worktree and branch**

```bash
cd /home/parlar_ai/dev/call-monitor && git status --short | wc -l && git worktree add .worktrees/doc-epoch -b feat/collab-doc-epoch master && cd .worktrees/doc-epoch && git log --oneline -1
```

Expected: `0` (clean tree), then the new worktree line, then `b992e8c2 docs(spec): collab document epoch …`.

- [ ] **Step 2: Sync the environment and prove the Python version**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv sync -q && uv run python --version && (cd frontend && pnpm install --frozen-lockfile --silent && pnpm exec vitest run src/collab/collabProvider.test.ts 2>&1 | tail -3)
```

Expected: `Python 3.12.x`; vitest prints `Tests  … passed`.

- [ ] **Step 3: Baseline the files this plan touches**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run pytest tests/proposal_assist/collab/test_collab_state.py tests/proposal_assist/collab/test_collab_hydrate.py tests/proposal_assist/collab/test_collab_room.py tests/proposal_assist/collab/test_collab_room_operate.py tests/proposal_assist/collab/test_collab_protocol.py tests/proposal_assist/collab/test_collab_ws_route.py tests/proposal_assist/collab/test_collab_ws_close_codes.py tests/proposal_assist/collab/test_collab_kill_switch_handover.py tests/proposal_assist/test_branch_switch.py tests/architecture -q 2>&1 | tail -2
```

Expected: all passed, 0 failed. Write the count down; the final count must be this plus the tests added below.

---

### Task 1: Storage — migration 0069 and the ORM column

**Files:**
- Create: `alembic/versions/0069_collab_doc_epoch.py`
- Modify: `src/call_manager/db/models.py:2117-2134` (`CollabArtifactState`)
- Regenerate: `docs/ARCHITECTURE.md`
- Test: `tests/test_migration_0069_collab_doc_epoch.py` (new)

**Interfaces:**
- Produces: `CollabArtifactState.doc_epoch: Mapped[int]` (NOT NULL, server default 0, Python default 0). Tasks 2 and 4 read it.

- [ ] **Step 1: Write the failing migration test**

```python
# tests/test_migration_0069_collab_doc_epoch.py
"""0069 adds collab_artifact_state.doc_epoch: the counter a room shows every
client so a retained Y.Doc can prove which document it holds (spec
2026-09-02-collab-doc-epoch-design.md §3.1)."""

from sqlalchemy import inspect, text

from call_manager.db.models import CollabArtifactState, User, Workspace


def _ws(session):
    u = User(status="active", email="epoch-mig@ki.se")
    session.add(u)
    session.flush()
    ws = Workspace(title="W", owner_id=u.id, current_phase="active")
    session.add(ws)
    session.commit()
    return ws.id


def test_column_exists_not_null_bigint(migrated_engine):
    cols = {c["name"]: c for c in inspect(migrated_engine).get_columns("collab_artifact_state")}
    assert "doc_epoch" in cols, "0069 must add doc_epoch"
    assert cols["doc_epoch"]["nullable"] is False
    assert "BIGINT" in str(cols["doc_epoch"]["type"]).upper()


def test_a_row_inserted_without_the_column_reads_zero(pg_session_factory):
    # A pre-0069 row (or any writer that never names the column) is epoch 0:
    # the server default, not NULL, so the route's comparison never sees None.
    with pg_session_factory() as s:
        wid = _ws(s)
        s.execute(
            text(
                "INSERT INTO collab_artifact_state (workspace_id, artifact) "
                "VALUES (:wid, 'draft')"
            ),
            {"wid": wid},
        )
        s.commit()
        row = s.get(CollabArtifactState, (wid, "draft"))
        assert row.doc_epoch == 0


def test_orm_default_is_zero_too(pg_session_factory):
    with pg_session_factory() as s:
        wid = _ws(s)
        s.add(CollabArtifactState(workspace_id=wid, artifact="spec"))
        s.commit()
        assert s.get(CollabArtifactState, (wid, "spec")).doc_epoch == 0
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run pytest tests/test_migration_0069_collab_doc_epoch.py -q 2>&1 | tail -4
```

Expected: 3 failed — `assert "doc_epoch" in cols` and `AttributeError: … has no attribute 'doc_epoch'`.

- [ ] **Step 3: Write the migration**

```python
# alembic/versions/0069_collab_doc_epoch.py
"""collab_artifact_state.doc_epoch — a truncated log is a new document

Revision ID: 0069
Revises: 0068
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default so every existing row is epoch 0 (a real value the route
    # can compare against); NOT NULL so no code path has to consider None.
    op.add_column(
        "collab_artifact_state",
        sa.Column(
            "doc_epoch",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("collab_artifact_state", "doc_epoch")
```

- [ ] **Step 4: Add the ORM column**

In `src/call_manager/db/models.py`, inside `CollabArtifactState`, after the `last_checkpoint_commit_sha` line:

```python
    last_checkpoint_commit_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The document epoch (spec 2026-09-02-collab-doc-epoch-design.md): bumped by
    # state.reset_checkpoint every time the log is truncated, shown to every
    # client on connect, presented back on reconnect. A mismatch is a Y.Doc from
    # a previous document and is refused (4409) before it can send its state.
    doc_epoch: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0", default=0
    )
```

`BigInteger` is already imported in `models.py` (it is used two lines above).

- [ ] **Step 5: Regenerate the architecture map**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run python scripts/gen_architecture.py && git diff --stat docs/ARCHITECTURE.md
```

Expected: one line changed — the `collab_artifact_state` row's column count moves from 5 to 6.

- [ ] **Step 6: Run the tests**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run pytest tests/test_migration_0069_collab_doc_epoch.py tests/architecture/test_architecture_map_is_current.py -q 2>&1 | tail -2
```

Expected: all passed.

- [ ] **Step 7: Lint and commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run ruff check src tests alembic && uv run ruff format --check src tests alembic && printf '%s\n' 'feat(collab): doc_epoch column on collab_artifact_state (migration 0069)' '' 'A truncated log is a new document. This is the counter that will let a room' 'tell a reconnecting client which document it holds, and refuse the wrong one' '(spec 2026-09-02-collab-doc-epoch-design.md §3.1). Storage only; nothing' 'reads or bumps it yet.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/claude-1066/-home-parlar-ai-dev-call-monitor/12803af4-7808-42cf-8c3d-65073c6c6120/scratchpad/msg1.txt && git add alembic/versions/0069_collab_doc_epoch.py src/call_manager/db/models.py docs/ARCHITECTURE.md tests/test_migration_0069_collab_doc_epoch.py && git commit -q -F /tmp/claude-1066/-home-parlar-ai-dev-call-monitor/12803af4-7808-42cf-8c3d-65073c6c6120/scratchpad/msg1.txt && git show --stat HEAD | tail -6
```

Expected: the stat lists all four files.

---

### Task 2: `state.get_doc_epoch` and the bump in `reset_checkpoint`

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/state.py:96-108` (`reset_checkpoint`) and add `get_doc_epoch` after `get_checkpoint_watermark`
- Modify: `src/call_manager/proposal_assist/collab/store.py:144-151` (docstring only)
- Test: `tests/proposal_assist/collab/test_collab_state.py`, `tests/proposal_assist/test_branch_switch.py`, `tests/proposal_assist/collab/test_collab_kill_switch_handover.py`

**Interfaces:**
- Consumes: `CollabArtifactState.doc_epoch` (Task 1).
- Produces: `state.get_doc_epoch(session, *, workspace_id: uuid.UUID, artifact: str) -> int` (0 when no row). `state.reset_checkpoint` now also increments `doc_epoch` and creates a missing row. Task 4 calls `get_doc_epoch`.

- [ ] **Step 1: Write the failing state tests**

Append to `tests/proposal_assist/collab/test_collab_state.py`:

```python


def test_doc_epoch_is_zero_without_a_row(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="st-e0@ki.se"))
        s.commit()
        wid = ws.id
    with pg_session_factory() as s:
        assert state.get_doc_epoch(s, workspace_id=wid, artifact="draft") == 0


def test_reset_checkpoint_bumps_the_epoch_and_clears_the_watermark(pg_session_factory):
    # A truncated log is a new document (spec §3.1): every reset moves the
    # epoch by exactly one, and the watermark goes back to "never opened".
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="st-e1@ki.se"))
        state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        state.advance_checkpoint(
            s, workspace_id=ws.id, artifact="draft", up_to_update_id=7, commit_sha="abc"
        )
        s.commit()
        wid = ws.id
    with pg_session_factory() as s:
        state.reset_checkpoint(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        assert state.get_doc_epoch(s, workspace_id=wid, artifact="draft") == 1
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == 0
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is True
        state.reset_checkpoint(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        assert state.get_doc_epoch(s, workspace_id=wid, artifact="draft") == 2


def test_reset_checkpoint_creates_a_missing_row_so_the_epoch_still_moves(pg_session_factory):
    # An artifact whose log is truncated before it ever had a state row (a
    # branch switch resets BOTH artifacts, enabled or not) must still leave
    # epoch 0 behind — otherwise a client from before the switch presents 0,
    # the room says 0, and the stale document is accepted.
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="st-e2@ki.se"))
        s.commit()
        wid = ws.id
    with pg_session_factory() as s:
        state.reset_checkpoint(s, workspace_id=wid, artifact="spec")
        s.commit()
    with pg_session_factory() as s:
        assert state.get_doc_epoch(s, workspace_id=wid, artifact="spec") == 1
        # created but NOT enabled: reset never flips the flag
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="spec") is False
        # per-artifact: draft is untouched
        assert state.get_doc_epoch(s, workspace_id=wid, artifact="draft") == 0
```

- [ ] **Step 2: Run them to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run pytest tests/proposal_assist/collab/test_collab_state.py -q 2>&1 | tail -4
```

Expected: 3 failed with `AttributeError: module … has no attribute 'get_doc_epoch'`.

- [ ] **Step 3: Implement**

In `src/call_manager/proposal_assist/collab/state.py`, add after `get_checkpoint_watermark`:

```python
def get_doc_epoch(session: Session, *, workspace_id: uuid.UUID, artifact: str) -> int:
    """The document epoch (spec 2026-09-02-collab-doc-epoch-design.md): the
    number of times this artifact's log has been truncated. 0 when there is no
    row — an artifact never enabled, or never reset. Callers read it in the
    SAME session as the log rows it labels (hydrate.load_or_hydrate)."""
    row = _get(session, workspace_id, artifact)
    return int(row.doc_epoch) if row is not None and row.doc_epoch is not None else 0
```

Replace `reset_checkpoint` in full:

```python
def reset_checkpoint(session: Session, *, workspace_id: uuid.UUID, artifact: str) -> None:
    """Return the artifact to the 'never opened' state (the log is truncated
    separately, same transaction) so a later reconnect hydrates fresh from git
    HEAD. enabled_at is deliberately untouched (sticky-forward).

    A truncated log is a NEW DOCUMENT, so this also moves `doc_epoch` by one —
    and creates the row if there is none, so an artifact truncated before it
    ever had a state row still moves off epoch 0. A client holding a Y.Doc from
    before the truncate presents the old epoch on reconnect and is refused
    (web/collab_ws.py, close 4409) instead of merging the old document into the
    new one (review 2026-09-02 §2.1).

    Two callers, both of which MUST have checkpointed the full log to git in the
    SAME transaction: the kill-switch handover (handover.py) and a branch switch
    (branches.py §4.2 step 6)."""
    row = _get(session, workspace_id, artifact)
    if row is None:
        row = CollabArtifactState(workspace_id=workspace_id, artifact=artifact)
        session.add(row)
    row.last_checkpoint_update_id = None
    row.last_checkpoint_commit_sha = None
    # A row added in this session has no epoch yet before flush (the ORM default
    # applies at INSERT); treat it as 0.
    row.doc_epoch = (row.doc_epoch or 0) + 1
```

In `src/call_manager/proposal_assist/collab/store.py`, replace the `truncate_log` docstring:

```python
    """Delete ALL log rows for one artifact.

    Two callers, both of which MUST have checkpointed the full log to git in the
    SAME transaction: the kill-switch handover (handover.py) and a branch switch
    (branches.py §4.2 step 6). Both ALSO call state.reset_checkpoint in that
    transaction: a truncated log is a new document, and reset_checkpoint is
    what bumps `doc_epoch` so a client from before the truncate is refused.

    Returns the number of rows deleted."""
```

- [ ] **Step 4: Run the state tests**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run pytest tests/proposal_assist/collab/test_collab_state.py -q 2>&1 | tail -2
```

Expected: 5 passed.

- [ ] **Step 5: Write the failing caller tests**

Append to `tests/proposal_assist/test_branch_switch.py`:

```python


def test_switch_moves_the_document_epoch_of_BOTH_artifacts(pg_session_factory, tmp_path):
    # Spec 2026-09-02-collab-doc-epoch-design.md §3.1: step 6 resets both
    # artifacts, and a reset is a new document. The spec artifact has no state
    # row here — it must still move, or a spec client from before the switch
    # presents 0, the room says 0, and its old text is merged into the branch.
    wid, repo, actor = _ws_with_repo(pg_session_factory, tmp_path)
    create_branch(repo, "short-version")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        store.append_update(
            s,
            workspace_id=wid,
            artifact="draft",
            actor_id=actor,
            update=_update_bytes("# Aims\n\nlive\n"),
        )
        s.commit()
        assert state.get_doc_epoch(s, workspace_id=wid, artifact="draft") == 0
        assert state.get_doc_epoch(s, workspace_id=wid, artifact="spec") == 0

    with pg_session_factory() as s:
        switch_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            target="short-version",
            workspaces_dir=tmp_path,
            teardown=lambda _wid: OK,
        )
        s.commit()

    with pg_session_factory() as s:
        assert state.get_doc_epoch(s, workspace_id=wid, artifact="draft") == 1
        assert state.get_doc_epoch(s, workspace_id=wid, artifact="spec") == 1
```

Append to `tests/proposal_assist/collab/test_collab_kill_switch_handover.py`:

```python


def test_handover_moves_the_document_epoch(pg_session_factory, tmp_path):
    """The handover truncates the log, and a truncated log is a new document
    (spec 2026-09-02-collab-doc-epoch-design.md §3.1): the epoch of every
    artifact it drained is one higher. An artifact it did NOT touch keeps its
    epoch — the bump rides reset_checkpoint, not the pass."""
    wid, oid, _repo = _setup(pg_session_factory, tmp_path, "ho-epoch@ki.se")
    _seed_room_edit(pg_session_factory, wid, oid, "## Background\n\nedit.\n")
    with pg_session_factory() as s:
        assert state.get_doc_epoch(s, workspace_id=wid, artifact="draft") == 0

    assert handover.run_kill_switch_handover(pg_session_factory, workspaces_dir=str(tmp_path)) == 1

    with pg_session_factory() as s:
        assert state.get_doc_epoch(s, workspace_id=wid, artifact="draft") == 1
        assert state.get_doc_epoch(s, workspace_id=wid, artifact="spec") == 0  # not drained
    # A second pass has nothing to drain and moves nothing.
    assert handover.run_kill_switch_handover(pg_session_factory, workspaces_dir=str(tmp_path)) == 0
    with pg_session_factory() as s:
        assert state.get_doc_epoch(s, workspace_id=wid, artifact="draft") == 1
```

- [ ] **Step 6: Run the caller tests**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run pytest tests/proposal_assist/test_branch_switch.py tests/proposal_assist/collab/test_collab_kill_switch_handover.py -q 2>&1 | tail -2
```

Expected: all passed (the two new tests pass on the implementation from Step 3 — they pin the callers, which already invoke `reset_checkpoint`). If either FAILS, the caller does not reach `reset_checkpoint` for that artifact; read `branches.py:228-231` / `handover.py:60-61` before touching anything.

- [ ] **Step 7: Prove the caller tests can fail**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && sed -i 's/    row.doc_epoch = (row.doc_epoch or 0) + 1/    pass  # MUTANT/' src/call_manager/proposal_assist/collab/state.py && uv run pytest tests/proposal_assist/test_branch_switch.py::test_switch_moves_the_document_epoch_of_BOTH_artifacts "tests/proposal_assist/collab/test_collab_kill_switch_handover.py::test_handover_moves_the_document_epoch" tests/proposal_assist/collab/test_collab_state.py -q 2>&1 | tail -2; git checkout -- src/call_manager/proposal_assist/collab/state.py && git diff --stat | tail -1
```

Expected: `4 failed` (the three bump tests + the zero-row test) while the mutant is in, then the checkout restores the file (`git diff --stat` shows only test files).

- [ ] **Step 8: Lint and commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run ruff check src tests && uv run ruff format --check src tests && printf '%s\n' 'feat(collab): reset_checkpoint bumps doc_epoch — a truncated log is a new document' '' 'state.get_doc_epoch reads it (0 without a row). reset_checkpoint now creates a' 'missing row and increments the epoch, so both truncate callers (branch switch' 'step 6, kill-switch handover) move every artifact they truncate off its old' 'epoch. Pinned per caller; a no-op bump fails 4 tests.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/claude-1066/-home-parlar-ai-dev-call-monitor/12803af4-7808-42cf-8c3d-65073c6c6120/scratchpad/msg2.txt && git add src/call_manager/proposal_assist/collab/state.py src/call_manager/proposal_assist/collab/store.py tests/proposal_assist/collab/test_collab_state.py tests/proposal_assist/test_branch_switch.py tests/proposal_assist/collab/test_collab_kill_switch_handover.py && git commit -q -F /tmp/claude-1066/-home-parlar-ai-dev-call-monitor/12803af4-7808-42cf-8c3d-65073c6c6120/scratchpad/msg2.txt && git show --stat HEAD | tail -7
```

Expected: the stat lists `state.py` and `store.py`, not only tests.

---

### Task 3: Wire frame 4 in `protocol.py`; `Room.handle` ignores it inbound

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/protocol.py` (constants, two functions, `__all__`)
- Test: `tests/proposal_assist/collab/test_collab_protocol.py`, `tests/proposal_assist/collab/test_collab_room.py`

**Interfaces:**
- Produces: `protocol.MESSAGE_DOC_EPOCH: int = 4`; `protocol.create_doc_epoch_message(epoch: int) -> bytes`; `protocol.read_doc_epoch_message(message: bytes) -> int` (raises `ValueError` unless the frame is type 4 with exactly one well-formed varuint). Task 6 sends the frame; Task 6's tests and Task 8 read it.

- [ ] **Step 1: Write the failing protocol tests**

Append to `tests/proposal_assist/collab/test_collab_protocol.py`:

```python


# --- Document epoch frame (spec 2026-09-02-collab-doc-epoch-design.md §3.4) ---


@pytest.mark.parametrize("epoch", [0, 1, 127, 128, 2**32, 2**62])
def test_doc_epoch_message_round_trips_through_the_bounded_reader(epoch):
    frame = P.create_doc_epoch_message(epoch)
    assert frame[0] == P.MESSAGE_DOC_EPOCH == 4
    assert P.read_doc_epoch_message(frame) == epoch


def test_doc_epoch_frame_is_neither_sync_nor_awareness():
    frame = P.create_doc_epoch_message(3)
    assert not P.is_sync_step1(frame)
    assert not P.is_sync_write(frame)
    assert not P.is_awareness(frame)


def test_read_doc_epoch_message_refuses_other_types_and_malformed_bodies():
    with pytest.raises(ValueError):
        P.read_doc_epoch_message(bytes([0, 0]))  # a SYNC frame
    with pytest.raises(ValueError):
        P.read_doc_epoch_message(bytes([4]))  # no epoch at all
    with pytest.raises(ValueError):
        P.read_doc_epoch_message(bytes([4]) + b"\x80" * 11)  # overlong varuint
    with pytest.raises(ValueError):
        P.read_doc_epoch_message(bytes([4, 5, 9]))  # trailing bytes after the epoch
```

Append to `tests/proposal_assist/collab/test_collab_room.py`:

```python


@pytest.mark.anyio
async def test_inbound_doc_epoch_frame_is_dropped_not_applied_persisted_or_broadcast():
    # Frame 4 is server→client only. A client that echoes it (or forges one)
    # changes nothing: Room.handle already ignores unknown types, and this pins
    # that the new type stays on the ignored path — not persisted, not
    # broadcast, not answered.
    room = Room((uuid.uuid4(), "draft"))
    sender, peer = FakeClient(), FakeClient()
    st = ClientState(user_id=1, can_write=True)
    room.clients[sender] = st
    room.clients[peer] = ClientState(user_id=2, can_write=True)
    log, persist = _persist_sink()
    await room.handle(
        sender=sender, state=st, message=P.create_doc_epoch_message(9), persist=persist
    )
    assert log == []
    assert sender.sent == []
    assert peer.sent == []
```

- [ ] **Step 2: Run them to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run pytest tests/proposal_assist/collab/test_collab_protocol.py tests/proposal_assist/collab/test_collab_room.py -q -k "doc_epoch" 2>&1 | tail -4
```

Expected: 9 failed with `AttributeError: module … has no attribute 'create_doc_epoch_message'` (and `MESSAGE_DOC_EPOCH`).

- [ ] **Step 3: Implement**

In `src/call_manager/proposal_assist/collab/protocol.py`, extend the module docstring's wire-format block:

```python
Wire format (pycrdt._sync):
  SYNC frame:      [YMessageType.SYNC (0)] [YSyncMessageType] [varuint len] [payload]
  AWARENESS frame: [YMessageType.AWARENESS (1)] [varuint len] [payload]
  DOC_EPOCH frame: [MESSAGE_DOC_EPOCH (4)] [varuint epoch]        (server → client only)
```

Add to `__all__` (after `"create_awareness_message"`):

```python
    "MESSAGE_DOC_EPOCH",
    "create_doc_epoch_message",
    "read_doc_epoch_message",
```

Add after `_read_var_uint` (before `safe_read_message`):

```python
# --- Document epoch frame (spec 2026-09-02-collab-doc-epoch-design.md §3.4) ---
#
# y-protocols uses 0 sync, 1 awareness, 2 auth, 3 query-awareness. 4 is free on
# both sides. The server sends one of these right before its STEP1 so the
# client can present the epoch back as `?epoch=N` on every reconnect; a Y.Doc
# from a previous document (a branch switch truncated the log) is then refused
# before it can send its state. Inbound, Room.handle ignores the type.
MESSAGE_DOC_EPOCH = 4


def create_doc_epoch_message(epoch: int) -> bytes:
    """[varuint 4][varuint epoch], written with pycrdt's own varuint writer."""
    if epoch < 0:
        raise ValueError("doc epoch must be non-negative")
    return write_var_uint(MESSAGE_DOC_EPOCH) + write_var_uint(epoch)


def read_doc_epoch_message(message: bytes) -> int:
    """The epoch carried by a DOC_EPOCH frame. Raises ValueError for any other
    type, a truncated or overlong varuint, or trailing bytes."""
    if len(message) < 1 or message[0] != MESSAGE_DOC_EPOCH:
        raise ValueError("not a doc-epoch frame")
    epoch, pos = _read_var_uint(message, 1)
    if pos != len(message):
        raise ValueError("doc-epoch frame: trailing bytes")
    return epoch
```

`write_var_uint` is already imported from `pycrdt` at the top of the module.

- [ ] **Step 4: Run the tests**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run pytest tests/proposal_assist/collab/test_collab_protocol.py tests/proposal_assist/collab/test_collab_room.py -q 2>&1 | tail -2
```

Expected: all passed (the room test passes without a room change: the type falls through to "Anything else: ignored").

- [ ] **Step 5: Lint and commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run ruff check src tests && uv run ruff format --check src tests && printf '%s\n' 'feat(collab): DOC_EPOCH wire frame (type 4) in protocol.py' '' '[varuint 4][varuint epoch], written with pycrdt write_var_uint and read back' 'through the one bounded varuint reader. Server-to-client only; Room.handle' 'keeps ignoring it inbound (pinned). Nothing sends it yet.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/claude-1066/-home-parlar-ai-dev-call-monitor/12803af4-7808-42cf-8c3d-65073c6c6120/scratchpad/msg3.txt && git add src/call_manager/proposal_assist/collab/protocol.py tests/proposal_assist/collab/test_collab_protocol.py tests/proposal_assist/collab/test_collab_room.py && git commit -q -F /tmp/claude-1066/-home-parlar-ai-dev-call-monitor/12803af4-7808-42cf-8c3d-65073c6c6120/scratchpad/msg3.txt && git show --stat HEAD | tail -5
```

---

### Task 4: `load_or_hydrate` returns a `Hydration` that carries the epoch

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/hydrate.py` (new class; every `return` in `load_or_hydrate`)
- Test: `tests/proposal_assist/collab/test_collab_hydrate.py`

**Interfaces:**
- Consumes: `state.get_doc_epoch` (Task 2).
- Produces: `hydrate.Hydration(list[bytes])` with attribute `doc_epoch: int`; `load_or_hydrate(...) -> Hydration`. Every existing caller iterates or compares it as a list and keeps working. Task 5's manager reads `getattr(updates, "doc_epoch", 0)`.

- [ ] **Step 1: Write the failing hydrate tests**

In `tests/proposal_assist/collab/test_collab_hydrate.py`, change the import line `from pycrdt import Doc` to `from pycrdt import Doc, Text`, then append:

```python


# --- The epoch rides with the rows (spec 2026-09-02-collab-doc-epoch-design.md §3.2) ---


def test_warm_path_returns_a_hydration_carrying_the_rows_epoch(pg_session_factory, tmp_path):
    from call_manager.proposal_assist.collab.hydrate import Hydration

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="hy-ep-warm@ki.se"))
        state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid = ws.id
    with pg_session_factory() as s:
        # Two truncates happened before this room open: the rows that exist
        # now belong to document 2.
        state.reset_checkpoint(s, workspace_id=wid, artifact="draft")
        state.reset_checkpoint(s, workspace_id=wid, artifact="draft")
        d = Doc()
        d["body"] = Text()
        d["body"] += "warm"
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=None, update=d.get_update()
        )
        s.commit()
    with pg_session_factory() as s:
        updates = load_or_hydrate(
            s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft",
            workspaces_dir=str(tmp_path),
        )
    assert isinstance(updates, Hydration)
    assert isinstance(updates, list)  # still a list to every existing caller
    assert len(updates) == 1
    assert updates.doc_epoch == 2


def test_cold_seed_path_carries_the_epoch_too(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="hy-ep-cold@ki.se"))
        s.commit()
        wid = ws.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## Background\nseeded.\n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        state.reset_checkpoint(s, workspace_id=wid, artifact="draft")  # epoch 1, log empty
        s.commit()
    with pg_session_factory() as s:
        updates = load_or_hydrate(
            s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft",
            workspaces_dir=str(tmp_path),
        )
        s.commit()
    assert len(updates) == 1  # the seed
    assert updates.doc_epoch == 1


def test_flag_off_and_never_reset_is_epoch_zero(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="hy-ep-off@ki.se"))
        s.commit()
        wid = ws.id
    with pg_session_factory() as s:
        updates = load_or_hydrate(
            s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft",
            workspaces_dir=str(tmp_path),
        )
    assert updates == []
    assert updates.doc_epoch == 0
```

- [ ] **Step 2: Run them to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run pytest tests/proposal_assist/collab/test_collab_hydrate.py -q -k "epoch" 2>&1 | tail -4
```

Expected: 3 failed — `ImportError: cannot import name 'Hydration'` and `AttributeError: 'list' object has no attribute 'doc_epoch'`.

- [ ] **Step 3: Implement**

In `src/call_manager/proposal_assist/collab/hydrate.py`, add after `_HYDRATE_MUTEX_TIMEOUT_SECONDS = 5`:

```python


class Hydration(list):
    """The update rows a room replays on cold start, plus the document epoch
    those rows belong to — read in the SAME mutex-held session, so the two are
    one consistent snapshot (spec 2026-09-02-collab-doc-epoch-design.md §3.2).

    A list subclass so every existing caller (which iterates, indexes, or
    compares the result to `[]`) is unchanged; only RoomManager reads
    `.doc_epoch`, and a plain list from a test loader reads as epoch 0."""

    def __init__(self, updates: list[bytes], *, doc_epoch: int) -> None:
        super().__init__(updates)
        self.doc_epoch = doc_epoch
```

Change the signature and every return of `load_or_hydrate`:

```python
def load_or_hydrate(
    session, *, stamp: FenceStamp, workspace_id: uuid.UUID, artifact: str, workspaces_dir
) -> Hydration:
```

and, right after the `acquire_commit_mutex_at_generation(...)` call (keep the existing comments above it unchanged):

```python
    # The epoch is read UNDER the mutex, next to the rows it labels: a switch
    # that truncates the log also bumps the epoch in one transaction, and the
    # mutex orders this read entirely before or entirely after it.
    doc_epoch = state.get_doc_epoch(session, workspace_id=workspace_id, artifact=artifact)
    updates = store.load_updates(session, workspace_id=workspace_id, artifact=artifact)
    if updates:
        return Hydration(updates, doc_epoch=doc_epoch)
    if not state.is_collab_enabled(session, workspace_id=workspace_id, artifact=artifact):
        return Hydration([], doc_epoch=doc_epoch)
    md = read_head_markdown(workspace_repo_dir(workspaces_dir, workspace_id), artifact)
    if md is None:
        # Collab enabled but the artifact file has never been committed. Leave the
        # room empty (Slice-1 behavior) rather than persisting a synthetic seed.
        return Hydration([], doc_epoch=doc_epoch)
    seed = pm_json_to_ydoc(markdown_to_pm_or_empty(md)).get_update()
    store.append_update(
        session, workspace_id=workspace_id, artifact=artifact, actor_id=None, update=seed
    )
    return Hydration([seed], doc_epoch=doc_epoch)
```

- [ ] **Step 4: Run the hydrate tests plus every file that calls `load_or_hydrate`**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run pytest tests/proposal_assist/collab/test_collab_hydrate.py tests/proposal_assist/collab/test_collab_hydrate_mutex_order.py tests/proposal_assist/collab/test_collab_kill_switch_handover.py tests/proposal_assist/collab/test_collab_recovery.py tests/proposal_assist/collab/test_collab_kernel_accept.py tests/proposal_assist/collab/test_collab_retire.py -q 2>&1 | tail -2
```

Expected: all passed.

- [ ] **Step 5: Lint and commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run ruff check src tests && uv run ruff format --check src tests && printf '%s\n' 'feat(collab): load_or_hydrate returns a Hydration carrying doc_epoch' '' 'A list subclass with one attribute, read under the same commit mutex as the' 'rows so epoch and log are one snapshot. Every existing caller keeps treating' 'the result as a list.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/claude-1066/-home-parlar-ai-dev-call-monitor/12803af4-7808-42cf-8c3d-65073c6c6120/scratchpad/msg4.txt && git add src/call_manager/proposal_assist/collab/hydrate.py tests/proposal_assist/collab/test_collab_hydrate.py && git commit -q -F /tmp/claude-1066/-home-parlar-ai-dev-call-monitor/12803af4-7808-42cf-8c3d-65073c6c6120/scratchpad/msg4.txt && git show --stat HEAD | tail -4
```

---

### Task 5: The room carries its epoch; the manager records it at both cold starts

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/room.py:130` (`Room.__init__`), `:556-560` (`acquire` cold start), `:606-610` (`operate` cold start)
- Test: `tests/proposal_assist/collab/test_collab_room_operate.py`

**Interfaces:**
- Consumes: `Hydration.doc_epoch` via `getattr(updates, "doc_epoch", 0)` (Task 4).
- Produces: `Room.doc_epoch: int` (0 until a cold start records it). Task 6's route compares against it and sends it.

- [ ] **Step 1: Write the failing manager tests**

Append to `tests/proposal_assist/collab/test_collab_room_operate.py`:

```python


# --- The room learns its epoch at cold start (spec 2026-09-02 doc-epoch §3.2) ---


def _hydration(epoch: int):
    from call_manager.proposal_assist.collab.hydrate import Hydration

    async def load():
        return Hydration([], doc_epoch=epoch)

    return load


async def test_acquire_records_the_loaders_epoch_on_the_room():
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    room = await mgr.acquire(
        key, load=_hydration(3), client=FakeClient(), state=ClientState(user_id=1, can_write=True)
    )
    assert room.doc_epoch == 3


async def test_operate_records_the_loaders_epoch_on_the_room():
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    async with mgr.operate(key, load=_hydration(5)) as room:
        assert room.doc_epoch == 5


async def test_a_warm_join_keeps_the_rooms_epoch_and_does_not_reload():
    # The second acquire finds the room and must NOT call its loader: the
    # epoch (like the doc) is the room's, learned once at cold start. Both
    # truncate callers tear rooms down before bumping, so a live room is
    # never behind its row.
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    c1, c2 = FakeClient(), FakeClient()
    await mgr.acquire(key, load=_hydration(3), client=c1, state=ClientState(user_id=1, can_write=True))
    room = await mgr.acquire(
        key, load=_hydration(9), client=c2, state=ClientState(user_id=2, can_write=True)
    )
    assert room.doc_epoch == 3


async def test_a_plain_list_loader_yields_epoch_zero():
    # Every fake loader in the suite returns a bare list; that must keep working
    # and read as epoch 0 (the value an artifact that was never reset has).
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    room = await mgr.acquire(
        key, load=_load, client=FakeClient(), state=ClientState(user_id=1, can_write=True)
    )
    assert room.doc_epoch == 0
```

- [ ] **Step 2: Run them to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run pytest tests/proposal_assist/collab/test_collab_room_operate.py -q -k "epoch" 2>&1 | tail -4
```

Expected: 4 failed with `AttributeError: 'Room' object has no attribute 'doc_epoch'`.

- [ ] **Step 3: Implement**

In `Room.__init__` (`room.py`, after `self.evicted = False  # tombstone…`):

```python
        # The document epoch this room's doc belongs to (spec 2026-09-02
        # doc-epoch §3.2). Set by RoomManager from the Hydration it replays;
        # 0 until then. The WS route compares a reconnecting client's `?epoch=`
        # against this before sending STEP1, and tells every client its value
        # in a DOC_EPOCH frame. A room never outlives an epoch change: both
        # truncate callers tear rooms down first.
        self.doc_epoch = 0
```

In `RoomManager.acquire`, replace the cold-start replay:

```python
            if room is None:
                room = Room(key)
                updates = await load()  # runs OUTSIDE the manager lock
                # The epoch rides with the rows (hydrate.Hydration); a bare list
                # from a test loader is epoch 0.
                room.doc_epoch = getattr(updates, "doc_epoch", 0)
                for update in updates:
                    room.doc.apply_update(update)
```

In `RoomManager.operate`, replace the cold-start replay:

```python
                if room is None:
                    room = Room(key)
                    updates = await load()  # OUTSIDE the manager lock
                    room.doc_epoch = getattr(updates, "doc_epoch", 0)
                    for update in updates:
                        room.doc.apply_update(update)
```

- [ ] **Step 4: Run the room tests**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run pytest tests/proposal_assist/collab/test_collab_room_operate.py tests/proposal_assist/collab/test_collab_room.py tests/proposal_assist/collab/test_collab_branch_teardown.py tests/proposal_assist/collab/test_collab_evict_tombstone.py -q 2>&1 | tail -2
```

Expected: all passed.

- [ ] **Step 5: Lint and commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run ruff check src tests && uv run ruff format --check src tests && printf '%s\n' 'feat(collab): Room.doc_epoch, recorded by the manager at both cold-start sites' '' 'acquire and operate read it off the Hydration they replay; a warm join keeps' 'the room value and never reloads (pinned). A plain-list loader is epoch 0.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/claude-1066/-home-parlar-ai-dev-call-monitor/12803af4-7808-42cf-8c3d-65073c6c6120/scratchpad/msg5.txt && git add src/call_manager/proposal_assist/collab/room.py tests/proposal_assist/collab/test_collab_room_operate.py && git commit -q -F /tmp/claude-1066/-home-parlar-ai-dev-call-monitor/12803af4-7808-42cf-8c3d-65073c6c6120/scratchpad/msg5.txt && git show --stat HEAD | tail -4
```

---

### Task 6: The route — parse `?epoch=`, refuse a mismatch before STEP1, send frame 4

**Files:**
- Modify: `src/call_manager/proposal_assist/web/collab_ws.py` (imports; before the origin check; first statement in the `try` at line 211; before `initial` at line 244)
- Test: `tests/proposal_assist/collab/test_collab_ws_route.py`

**Interfaces:**
- Consumes: `Room.doc_epoch` (Task 5), `protocol.create_doc_epoch_message` (Task 3), `WS_DOCUMENT_REPLACED` (already imported).
- Produces: the wire behaviour the client in Task 7 relies on: frame 4 arrives before STEP1 on every accepted connect; `?epoch=N` with N ≠ room epoch closes 4409 with no frame sent; malformed `?epoch=` closes 1008 before accept.

- [ ] **Step 1: Write the failing route tests**

Append to `tests/proposal_assist/collab/test_collab_ws_route.py`:

```python


# --- Document epoch (spec 2026-09-02-collab-doc-epoch-design.md §3.3, §6 tests 1-4) ---


def _doc_update(text: str) -> bytes:
    d = Doc()
    d["body"] = Text()
    d["body"] += text
    return d.get_update()


def _epoch_setup(_app, pg_session_factory, email: str):
    """An enabled draft whose log holds one row of `old` content. Returns
    (client, wid, oid) with the client's session cookie set."""
    from fastapi.testclient import TestClient

    from call_manager.proposal_assist.collab import store

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email=email)
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=oid, update=_doc_update("old aims")
        )
        s.commit()
    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    return client, wid, oid


def _body_text(pg_session_factory, wid) -> str:
    from call_manager.proposal_assist.collab import store

    d = Doc()
    d["body"] = Text()
    with pg_session_factory() as s:
        for u in store.load_updates(s, workspace_id=wid, artifact="draft"):
            d.apply_update(u)
    return str(d["body"])


def _truncate_and_replace(pg_session_factory, wid, oid, text: str) -> None:
    """What a branch switch does to the log (branches.py step 6) followed by
    new content on the new document — without the git choreography."""
    from call_manager.proposal_assist.collab import store

    with pg_session_factory() as s:
        store.truncate_log(s, workspace_id=wid, artifact="draft")
        collab_state.reset_checkpoint(s, workspace_id=wid, artifact="draft")
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=oid, update=_doc_update(text)
        )
        s.commit()


def test_every_accepted_connect_learns_the_epoch_before_step1(_app, pg_session_factory):
    # Spec test 3: no `?epoch=` → frame 4 (with the room's epoch) then STEP1.
    client, wid, oid = _epoch_setup(_app, pg_session_factory, "route-epoch-learn@ki.se")
    try:
        with client.websocket_connect(f"/api/ws/workspaces/{wid}/draft") as wsconn:
            first = wsconn.receive_bytes()
            assert P.read_doc_epoch_message(first) == 0
            assert P.is_sync_step1(wsconn.receive_bytes())
    finally:
        client.__exit__(None, None, None)


def test_the_reviews_reproduction_is_refused_before_step1(_app, pg_session_factory, caplog):
    # Spec test 1 — review 2026-09-02 §2.1 / probe_stale_doc_merge.py. A client
    # learned epoch 0 with "old aims" in its doc, missed the switch, and comes
    # back presenting 0 against a room on document 1. It must be closed 4409
    # having received NOTHING (no STEP1 → it can never send its STEP2), and the
    # new document must be exactly what it was.
    import logging

    import pytest
    from starlette.websockets import WebSocketDisconnect

    client, wid, oid = _epoch_setup(_app, pg_session_factory, "route-epoch-stale@ki.se")
    try:
        with client.websocket_connect(f"/api/ws/workspaces/{wid}/draft") as wsconn:
            learned = P.read_doc_epoch_message(wsconn.receive_bytes())
            assert P.is_sync_step1(wsconn.receive_bytes())
        assert learned == 0
        _truncate_and_replace(pg_session_factory, wid, oid, "new aims")
        assert _body_text(pg_session_factory, wid) == "new aims"

        with caplog.at_level(logging.INFO):
            with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(
                f"/api/ws/workspaces/{wid}/draft?epoch={learned}"
            ) as wsconn:
                wsconn.receive_bytes()  # the first thing to arrive is the close
        assert exc.value.code == 4409
        assert _body_text(pg_session_factory, wid) == "new aims"  # unchanged
        events = [r.__dict__.get("event") for r in caplog.records]
        assert "collab_stale_doc_refused" in events

        # A fresh doc (no epoch) is accepted and learns the new epoch.
        with client.websocket_connect(f"/api/ws/workspaces/{wid}/draft") as wsconn:
            assert P.read_doc_epoch_message(wsconn.receive_bytes()) == 1
            assert P.is_sync_step1(wsconn.receive_bytes())
    finally:
        client.__exit__(None, None, None)


def test_a_socket_presenting_the_current_epoch_is_accepted_and_can_persist(
    _app, pg_session_factory
):
    # Spec test 2.
    client, wid, oid = _epoch_setup(_app, pg_session_factory, "route-epoch-current@ki.se")
    try:
        _truncate_and_replace(pg_session_factory, wid, oid, "new aims")
        with client.websocket_connect(f"/api/ws/workspaces/{wid}/draft?epoch=1") as wsconn:
            assert P.read_doc_epoch_message(wsconn.receive_bytes()) == 1
            assert P.is_sync_step1(wsconn.receive_bytes())
            wsconn.send_bytes(_update_frame(" plus edit"))
    finally:
        client.__exit__(None, None, None)
    with pg_session_factory() as s:
        rows = s.execute(select(YjsUpdate).where(YjsUpdate.workspace_id == wid)).scalars().all()
    assert len(rows) == 2  # the new-document row + this socket's edit


def test_a_malformed_epoch_closes_1008_before_any_frame(_app, pg_session_factory):
    # Spec test 4.
    import pytest
    from starlette.websockets import WebSocketDisconnect

    client, wid, _oid = _epoch_setup(_app, pg_session_factory, "route-epoch-bad@ki.se")
    try:
        for bad in ("abc", "-1", "1.5", ""):
            with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(
                f"/api/ws/workspaces/{wid}/draft?epoch={bad}"
            ) as wsconn:
                wsconn.receive_bytes()
            assert exc.value.code == 1008, bad
    finally:
        client.__exit__(None, None, None)
```

- [ ] **Step 2: Run them to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run pytest tests/proposal_assist/collab/test_collab_ws_route.py -q -k "epoch or reproduction" 2>&1 | tail -6
```

Expected: 4 failed. The learn test fails on `read_doc_epoch_message` (`not a doc-epoch frame`: STEP1 arrived first); the reproduction test fails the same way; the malformed test fails because the connect is accepted.

- [ ] **Step 3: Implement**

In `src/call_manager/proposal_assist/web/collab_ws.py`:

Add the import as the FIRST of the three `from call_manager.proposal_assist.collab import …` lines (ruff's isort orders them by imported name: `protocol` < `state` < `store`; `ruff check` fails on I001 otherwise):

```python
from call_manager.proposal_assist.collab import protocol as P
from call_manager.proposal_assist.collab import state as collab_state
from call_manager.proposal_assist.collab import store
```

Add a helper after `_MAX_FRAME_BYTES = 1_000_000`:

```python


def _parse_client_epoch(raw: str | None) -> int | None:
    """`?epoch=` as the client presents it (spec 2026-09-02 doc-epoch §3.3).

    Absent → None (a fresh doc, which can merge nothing). A non-negative
    decimal integer → that value. Anything else → ValueError; the route closes
    1008 like any other malformed request."""
    if raw is None:
        return None
    if not (raw.isascii() and raw.isdigit()):
        raise ValueError(raw)
    return int(raw)
```

At the top of `collab_ws`, right after the `artifact not in _VALID_ARTIFACTS` block and before the origin check:

```python
    try:
        client_epoch = _parse_client_epoch(websocket.query_params.get("epoch"))
    except ValueError:
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return
```

Inside the `try:` that owns `finally: release` (line 211), as its FIRST statement, before the `# Close the connect-straddle TOCTOU` comment:

```python
    try:
        # Document epoch (spec 2026-09-02-collab-doc-epoch-design.md §3.3): a
        # client that presents an epoch holds a Y.Doc that synced against THAT
        # document. If the room is on a different one — a branch switch or the
        # kill-switch handover truncated the log while this client was in
        # reconnect backoff and never saw the 4409 — its STEP2 would merge the
        # old document into the new one (review 2026-09-02 §2.1, reproduced).
        # Refuse BEFORE STEP1, so no STEP2 is ever solicited. Inside this try so
        # `finally` deregisters the client. 4409 makes the provider discard the
        # doc and the hook build a fresh pair, which connects with no epoch.
        if client_epoch is not None and client_epoch != room.doc_epoch:
            logger.info(
                "collab.stale_doc_refused",
                extra={
                    "event": "collab_stale_doc_refused",
                    "workspace_id": str(workspace_id),
                    "artifact": artifact,
                    "client_epoch": client_epoch,
                    "room_epoch": room.doc_epoch,
                },
            )
            with contextlib.suppress(Exception):
                await websocket.close(code=WS_DOCUMENT_REPLACED)
            return
```

Before the initial STEP1 (replace the two lines `async with room.lock: initial = …` / `await websocket.send_bytes(initial)` with):

```python
        # Teach the epoch BEFORE the STEP1 so a client that drops mid-handshake
        # still presents the right value next time; an old client ignores the
        # unknown type (spec §5).
        await websocket.send_bytes(P.create_doc_epoch_message(room.doc_epoch))
        async with room.lock:
            initial = room.initial_message()
        await websocket.send_bytes(initial)
```

- [ ] **Step 4: Run the route tests and the close-code source pins**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run pytest tests/proposal_assist/collab/test_collab_ws_route.py tests/proposal_assist/collab/test_collab_ws_close_codes.py tests/proposal_assist/collab/test_collab_ws_auth.py tests/proposal_assist/collab/test_collab_ws_retire_route.py tests/proposal_assist/collab/test_collab_permission_routes.py tests/proposal_assist/collab/test_collab_revoke_user.py -q 2>&1 | tail -3
```

Expected: all passed. If any PRE-EXISTING route test now fails on `is_sync_step1(first)`, that test read the first frame as STEP1: change it to read two frames (`P.read_doc_epoch_message(wsconn.receive_bytes())` then the STEP1). Fix every such site in this task; list them in the commit message.

- [ ] **Step 5: Sweep the other WebSocket tests for "first frame is STEP1"**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && grep -rln "websocket_connect" tests | sort && uv run pytest $(grep -rln "websocket_connect" tests | tr '\n' ' ') -q 2>&1 | tail -3
```

Expected: every listed file passes. Fix any that assumed STEP1 first, as in Step 4.

- [ ] **Step 6: Lint and commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run ruff check src tests && uv run ruff format --check src tests && printf '%s\n' 'fix(collab): refuse a stale Y.Doc on reconnect — the review 2026-09-02 §2.1 merge' '' 'The WS route parses ?epoch= (malformed → 1008), compares it with the room' 'epoch as the first statement inside the try that owns release, and closes' '4409 on mismatch before any STEP1 — so the stale document is never asked for' 'its state. Every accepted connect gets a DOC_EPOCH frame before STEP1.' '' 'The review reproduction (old doc reconnecting after a truncate) now closes' '4409 with nothing received, and the new document is byte-unchanged.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/claude-1066/-home-parlar-ai-dev-call-monitor/12803af4-7808-42cf-8c3d-65073c6c6120/scratchpad/msg6.txt && git add -A src tests && git commit -q -F /tmp/claude-1066/-home-parlar-ai-dev-call-monitor/12803af4-7808-42cf-8c3d-65073c6c6120/scratchpad/msg6.txt && git show --stat HEAD | tail -6
```

Expected: the stat lists `collab_ws.py` and `test_collab_ws_route.py` (plus any test fixed in Steps 4–5).

---

### Task 7: The client — learn the epoch, present it on reconnect

**Files:**
- Modify: `frontend/src/collab/collabProvider.ts`
- Test: `frontend/src/collab/collabProvider.test.ts`

**Interfaces:**
- Consumes: the server's frame 4 (Task 6).
- Produces: `CollabProvider.docEpoch: number | null` (read-only getter). Reconnects open `url + "?epoch=" + docEpoch` once learned. Nothing else in the frontend changes: `useCollabDoc` still builds a fresh pair on 4409, which connects bare.

- [ ] **Step 1: Write the failing vitest tests**

Append to `frontend/src/collab/collabProvider.test.ts`:

```ts

describe("document epoch — a retained doc says which document it holds", () => {
  // Spec 2026-09-02-collab-doc-epoch-design.md §4 / §6 tests 9-11. The server
  // sends [varuint 4][varuint epoch] before its STEP1. A client that learned
  // an epoch presents it on every reconnect so a doc from before a branch
  // switch is refused (4409) before it can send its state.
  const MESSAGE_DOC_EPOCH = 4;
  const BARE = "ws://x/api/ws/workspaces/w/draft";
  function epochFrame(epoch: number): Uint8Array {
    const e = encoding.createEncoder();
    encoding.writeVarUint(e, MESSAGE_DOC_EPOCH);
    encoding.writeVarUint(e, epoch);
    return encoding.toUint8Array(e);
  }

  it("connects bare first, learns the epoch, and presents it on the next reconnect", () => {
    vi.useFakeTimers();
    const { p, sockets } = makeProvider(true);
    expect(p.docEpoch).toBeNull();
    expect(sockets[0].url).toBe(BARE);
    sockets[0].open();
    sockets[0].deliver(epochFrame(7));
    expect(p.docEpoch).toBe(7);
    sockets[0].drop(1006);
    vi.advanceTimersByTime(50);
    expect(sockets).toHaveLength(2);
    expect(sockets[1].url).toBe(`${BARE}?epoch=7`);
    p.destroy();
    vi.useRealTimers();
  });

  it("a frame 4 does not latch synced and is not echoed", () => {
    const { p, sockets } = makeProvider(true);
    const s = sockets[0]; s.open(); s.sent.length = 0;
    s.deliver(epochFrame(2));
    expect(p.synced).toBe(false);
    expect(s.sent).toHaveLength(0);
    p.destroy();
  });

  it("after 4409 this provider stays down and a NEW provider opens the bare URL", () => {
    vi.useFakeTimers();
    const onDocumentReplaced = vi.fn();
    const { p, sockets } = makeProvider(true, { onDocumentReplaced });
    sockets[0].open();
    sockets[0].deliver(epochFrame(3));
    sockets[0].drop(WS_DOCUMENT_REPLACED);
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(1);              // the old doc never reconnects
    expect(onDocumentReplaced).toHaveBeenCalledOnce();
    p.destroy();
    const fresh = makeProvider(true);             // what useCollabDoc builds next
    expect(fresh.p.docEpoch).toBeNull();
    expect(fresh.sockets[0].url).toBe(BARE);
    fresh.p.destroy();
    vi.useRealTimers();
  });

  it("appends with & when the URL already carries a query string", () => {
    vi.useFakeTimers();
    const sockets: FakeSocket[] = [];
    const p = new CollabProvider({
      url: "ws://x/y?token=t", doc: new Y.Doc(), canWrite: true,
      reconnectBaseMs: 10, reconnectMaxMs: 20,
      WebSocketImpl: class extends FakeSocket {
        constructor(u: string) { super(u); sockets.push(this); }
      } as unknown as typeof WebSocket,
    });
    sockets[0].open();
    sockets[0].deliver(epochFrame(1));
    sockets[0].drop(1006);
    vi.advanceTimersByTime(50);
    expect(sockets[1].url).toBe("ws://x/y?token=t&epoch=1");
    p.destroy();
    vi.useRealTimers();
  });
});
```

- [ ] **Step 2: Run them to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch/frontend && pnpm exec vitest run src/collab/collabProvider.test.ts 2>&1 | grep -E "✓|×|✗|Tests|docEpoch" | tail -12
```

Expected: the four new tests fail (`p.docEpoch` is `undefined`, not `null`; the reconnect URL is bare).

- [ ] **Step 3: Implement**

In `frontend/src/collab/collabProvider.ts`:

After `const MESSAGE_AWARENESS = 1;`:

```ts
/** Server → client only: [varuint 4][varuint epoch]. Which DOCUMENT the room
 *  holds (a branch switch or kill-switch handover starts a new one). We present
 *  it back as `?epoch=N` on every reconnect so a doc from a previous document is
 *  refused (4409) before it can send its state. y-protocols uses 0-3; 4 is free. */
const MESSAGE_DOC_EPOCH = 4;
```

Among the private fields (after `private _synced = false;`):

```ts
  // The document epoch this doc synced against, learned from the server's
  // DOC_EPOCH frame. null until then — a fresh doc can merge nothing, so it
  // connects bare and learns the current value. Never persisted: a page
  // reload is a fresh doc by design.
  private _docEpoch: number | null = null;
```

After the `get synced()` accessor:

```ts
  /** The document epoch learned from the server, or null before the first
   *  DOC_EPOCH frame. */
  get docEpoch(): number | null {
    return this._docEpoch;
  }
```

In `connect()`, replace `const ws = new this.WS(this.url);` with:

```ts
    const url =
      this._docEpoch === null
        ? this.url
        : `${this.url}${this.url.includes("?") ? "&" : "?"}epoch=${this._docEpoch}`;
    const ws = new this.WS(url);
```

In `ws.onmessage`, right after `const messageType = decoding.readVarUint(dec);` and before the awareness branch:

```ts
        if (messageType === MESSAGE_DOC_EPOCH) {
          // Neither sync nor awareness: applies nothing, latches nothing, is
          // never echoed. Remembered for the next connect's URL.
          this._docEpoch = decoding.readVarUint(dec);
          return;
        }
```

- [ ] **Step 4: Run the tests and the type check**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch/frontend && pnpm exec vitest run src/collab 2>&1 | tail -4 && pnpm exec tsc --noEmit -p tsconfig.json && echo TSC-OK
```

Expected: all collab tests pass; `TSC-OK`.

- [ ] **Step 5: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && printf '%s\n' 'feat(collab-client): learn the document epoch, present it on reconnect' '' 'CollabProvider reads the DOC_EPOCH frame (type 4) into docEpoch and opens' 'every later connect with ?epoch=N. A fresh provider (what useCollabDoc builds' 'after 4409) connects bare and learns the new value. Nothing else changes.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/claude-1066/-home-parlar-ai-dev-call-monitor/12803af4-7808-42cf-8c3d-65073c6c6120/scratchpad/msg7.txt && git add frontend/src/collab/collabProvider.ts frontend/src/collab/collabProvider.test.ts && git commit -q -F /tmp/claude-1066/-home-parlar-ai-dev-call-monitor/12803af4-7808-42cf-8c3d-65073c6c6120/scratchpad/msg7.txt && git show --stat HEAD | tail -4
```

---

### Task 8: Mutation proof (spec §6 test 8)

**Files:** scratch only — `/tmp/claude-1066/-home-parlar-ai-dev-call-monitor/12803af4-7808-42cf-8c3d-65073c6c6120/scratchpad/mutate_doc_epoch.sh`. Nothing is committed by this task.

Each of the four mechanisms is disabled in turn; the named test must FAIL. A `MISSED` line means either a hollow gate or a wrong anchor — check `git diff --stat` inside the loop output before believing either.

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# Mutation proof for the doc_epoch slice. Each mutant must make its test FAIL.
set -u
W=/home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch
cd "$W" || exit 1
ROUTE=src/call_manager/proposal_assist/web/collab_ws.py
ROOM=src/call_manager/proposal_assist/collab/room.py
STATE=src/call_manager/proposal_assist/collab/state.py

run() { # name file sed-expr test-selector
  local name=$1 file=$2 expr=$3 sel=$4
  cp "$file" "$file.bak"
  sed -i "$expr" "$file"
  if cmp -s "$file" "$file.bak"; then echo "BAD-ANCHOR  $name (sed changed nothing)"; mv "$file.bak" "$file"; return; fi
  if uv run pytest $sel -q -p no:cacheprovider >/dev/null 2>&1; then echo "MISSED      $name"; else echo "CAUGHT      $name"; fi
  mv "$file.bak" "$file"
}

# Line number of the N-th line containing a fixed string (empty if absent).
nth() { grep -n -F "$2" "$1" | sed -n "${3}p" | cut -d: -f1; }

run "route: mismatch check disabled" "$ROUTE" \
  's/if client_epoch is not None and client_epoch != room.doc_epoch:/if False:/' \
  "tests/proposal_assist/collab/test_collab_ws_route.py::test_the_reviews_reproduction_is_refused_before_step1"

run "route: frame 4 never sent" "$ROUTE" \
  's/await websocket.send_bytes(P.create_doc_epoch_message(room.doc_epoch))/pass/' \
  "tests/proposal_assist/collab/test_collab_ws_route.py::test_every_accepted_connect_learns_the_epoch_before_step1"

run "route: frame 4 sends the wrong value" "$ROUTE" \
  's/P.create_doc_epoch_message(room.doc_epoch)/P.create_doc_epoch_message(0)/' \
  "tests/proposal_assist/collab/test_collab_ws_route.py::test_the_reviews_reproduction_is_refused_before_step1"

run "route: malformed epoch accepted" "$ROUTE" \
  's/raise ValueError(raw)/return None/' \
  "tests/proposal_assist/collab/test_collab_ws_route.py::test_a_malformed_epoch_closes_1008_before_any_frame"

run "state: reset_checkpoint no longer bumps" "$STATE" \
  's/row.doc_epoch = (row.doc_epoch or 0) + 1/pass/' \
  "tests/proposal_assist/collab/test_collab_state.py::test_reset_checkpoint_bumps_the_epoch_and_clears_the_watermark"

# The `if row is None:` INSIDE reset_checkpoint (there is an earlier one in
# advance_checkpoint): first such line after the def. An early return there
# means no row is created and the epoch never moves.
d=$(nth "$STATE" 'def reset_checkpoint' 1); : "${d:?reset_checkpoint not found}"
m=$(awk -v s="$d" 'NR>s && /if row is None:/ {print NR; exit}' "$STATE"); : "${m:?if-row-is-None not found}"
run "state: reset_checkpoint does not create the row" "$STATE" \
  "${m}s/if row is None:/if row is None:\n        return  # MUTANT/" \
  "tests/proposal_assist/collab/test_collab_state.py::test_reset_checkpoint_creates_a_missing_row_so_the_epoch_still_moves"

# room.py has the recording line TWICE: acquire first, operate second.
a=$(nth "$ROOM" 'room.doc_epoch = getattr(updates, "doc_epoch", 0)' 1); : "${a:?acquire site not found}"
run "manager: acquire forgets the epoch" "$ROOM" \
  "${a}s/getattr(updates, \"doc_epoch\", 0)/0/" \
  "tests/proposal_assist/collab/test_collab_room_operate.py::test_acquire_records_the_loaders_epoch_on_the_room"

o=$(nth "$ROOM" 'room.doc_epoch = getattr(updates, "doc_epoch", 0)' 2); : "${o:?operate site not found}"
run "manager: operate forgets the epoch" "$ROOM" \
  "${o}s/getattr(updates, \"doc_epoch\", 0)/0/" \
  "tests/proposal_assist/collab/test_collab_room_operate.py::test_operate_records_the_loaders_epoch_on_the_room"

git diff --stat | tail -1
```

Every mutant is anchored on an exact line from Tasks 2, 5 and 6. `set -u` plus the `:?` guards abort the script (instead of mutating the wrong line) if an anchor is missing; a `BAD-ANCHOR` line means the sed pattern did not match on the line it was pointed at. Either way: open the file and compare with the plan's code before changing the script.

- [ ] **Step 2: Run it**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && bash /tmp/claude-1066/-home-parlar-ai-dev-call-monitor/12803af4-7808-42cf-8c3d-65073c6c6120/scratchpad/mutate_doc_epoch.sh
```

Expected: eight `CAUGHT` lines, no `MISSED`, no `BAD-ANCHOR`, and the final `git diff --stat` line is empty (every backup restored).

If a line reads `MISSED`: apply that mutant by hand, run the named test with `-x -vv`, and read the assertion that should have fired. Strengthen the test, do not weaken the mutant. Re-run until eight `CAUGHT`.

- [ ] **Step 3: Record the result in the spec**

Append to `docs/superpowers/specs/2026-09-02-collab-doc-epoch-design.md`, at the end of §6:

```markdown

**Mutation proof (run 2026-09-02 at authoring time):** eight mutants — route
mismatch check disabled, frame 4 not sent, frame 4 sends 0, malformed epoch
accepted, `reset_checkpoint` no bump, `reset_checkpoint` no row creation,
`acquire` forgets the epoch, `operate` forgets the epoch — each made its named
test fail. Script: `mutate_doc_epoch.sh` (scratch, not committed).
```

Change the spec's `**Status:**` line to `**Status:** implemented on feat/collab-doc-epoch (unmerged)`.

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && printf '%s\n' 'docs(spec): doc_epoch — record the mutation proof' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/claude-1066/-home-parlar-ai-dev-call-monitor/12803af4-7808-42cf-8c3d-65073c6c6120/scratchpad/msg8.txt && git add docs/superpowers/specs/2026-09-02-collab-doc-epoch-design.md && git commit -q -F /tmp/claude-1066/-home-parlar-ai-dev-call-monitor/12803af4-7808-42cf-8c3d-65073c6c6120/scratchpad/msg8.txt && git log --oneline master..HEAD
```

Expected: eight commits on the branch.

---

### Task 9: Full verification

**Files:** none.

- [ ] **Step 1: Confirm the tree and interpreter**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && pwd && git status --short | wc -l && uv run python --version
```

Expected: the worktree path, `0`, `Python 3.12.x`.

- [ ] **Step 2: Run the full backend suite**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run pytest -n 16 --dist loadfile -q 2>&1 | tail -3
```

Expected: `N passed, 1 skipped` where N = 4760 + the tests this plan added (3 migration + 3 state + 1 switch + 1 handover + 6 protocol parametrized + 2 protocol + 1 room + 3 hydrate + 4 manager + 4 route = 28) = **4788**, and `1 skipped` (the API-key-gated test). A `connection refused` failure is the testcontainers-under-load flake: rerun that file alone before reading it as a regression.

- [ ] **Step 3: Run the frontend suite, type check and build**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch/frontend && pnpm exec vitest run 2>&1 | tail -3 && pnpm build 2>&1 | tail -2
```

Expected: all tests pass; `vite build` reports `built in …`.

- [ ] **Step 4: Lint everything**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && uv run ruff check src tests alembic scripts && uv run ruff format --check src tests alembic scripts && echo LINT-OK
```

Expected: `LINT-OK`.

- [ ] **Step 5: Bundle and report**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/doc-epoch && git bundle create ~/git-bundles/call-monitor-2026-09-02-doc-epoch.bundle --all 2>&1 | tail -1 && git log --oneline master..HEAD
```

Then report to the user: the branch name, the eight commits, the suite totals from Steps 2–3, and the mutation result. Do NOT merge; that is the user's call. The merge command, when they say go:

```bash
cd /home/parlar_ai/dev/call-monitor && git status --short | wc -l && git merge --no-ff feat/collab-doc-epoch -m "Merge branch 'feat/collab-doc-epoch'" && git worktree remove .worktrees/doc-epoch && git branch -d feat/collab-doc-epoch
```

If `master` moved under the branch (another session), run `git merge-tree $(git merge-base master feat/collab-doc-epoch) master feat/collab-doc-epoch | grep -c '^<<<<<<<'` first; `0` means a clean merge.
