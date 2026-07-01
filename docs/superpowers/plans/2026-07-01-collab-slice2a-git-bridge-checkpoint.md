# Collaborative Editing — Slice 2a: git↔room bridge + checkpoint layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the live Yjs room (Slice 1) to git in both directions — **hydrate** a room from git HEAD markdown on first open, and **checkpoint** the live room back to a git commit (with `ArtifactVersion` + `WorkspaceEvent` provenance) on last-client-leaving and on an explicit "Save a version" — all behind an **off-by-default per-artifact flag**. Also close the Slice-0 spike's deferred **Node write-path validation**.

**Architecture:** Both directions chain the Slice-0 bridge. Hydration: git markdown → `markdown_to_pm` → `pm_json_to_ydoc` → seed update persisted as the room's first `yjs_updates` row (so the Yjs store becomes canonical thereafter). Checkpoint: live `Doc` → `ydoc_to_pm_json` → `pm_to_markdown` → `write_and_commit_files`, co-authored from the `yjs_updates.actor_id` log via a per-artifact watermark. A new `collab_artifact_state` table holds the flag + the checkpoint watermark + last checkpoint SHA. The **room-aware write kernel and the git-canonical switchover are NOT in this slice** (2b) — so this flag must stay off in prod until 2b lands.

**Tech Stack:** Python 3.12, FastAPI/Starlette WebSockets, `pycrdt` 0.14.0, SQLAlchemy 2.x + Alembic + PostgreSQL, pytest + testcontainers PG; frontend `yjs`/`y-prosemirror` (already present) for the cross-runtime write-path check.

## Global Constraints

- **The collab flag is OFF by default and MUST NOT be enabled in production until Slice 2b.** Without the room-aware write kernel, the existing git-first write paths (`save_section`, `human_cite`, spec-lock) would conflict with checkpoints. 2a is a tested substrate; enabling it only routes the *new WS editor* (which has no UI yet) through git.
- **No new backend deps.** `pycrdt` already present; the bridge is in `proposal_assist/collab/`.
- **Checkpoint author identity goes in the commit MESSAGE.** `write_and_commit_files(repo_dir, files, message, *, env=None) -> sha` has **no** author param (git author is always `call-manager@local`). Encode contributing user ids in the message string (as `save.py` does).
- **`ArtifactVersion.created_by` is CHECK-constrained** to `{"agent","user","system","reconciled","unrecoverable"}` — a checkpoint uses `"user"` when it has human authors, else `"system"`. Do NOT invent a new value (would need a migration).
- **`WorkspaceEvent.kind` is free Text** (no CHECK) — the new `"collab_checkpoint"` kind needs only an activity-feed `_RENDER` entry, no migration.
- **The Slice-0 bridge drops raw HTML** on `markdown_to_pm`; a first hydration of an artifact containing raw HTML loses it. Acceptable (existing content is demo; documented).
- **The `split_sections` invariant** (`"".join(s.text) == doc`, `##` boundaries) must hold on every checkpoint's markdown output — property-tested.
- **Hydration idempotency** relies on the Slice-1 per-key creation lock in `RoomManager.acquire` (the seed is persisted exactly once per cold start). Do not remove it.
- **Sync DB in async WS handler** → `run_in_threadpool` (as Slice 1). Checkpoint on the event loop (last-client-leaving) likewise offloads DB+git.
- **Tests** under `tests/proposal_assist/collab/`; a workspace git repo is set up with the `_init_repo` pattern from `tests/proposal_assist/proposals/test_accepting.py` (`git init -b main`, config user, commit `proposal.md`), `repo = tmp_path/str(ws.id)/"repo"`, `workspaces_dir=str(tmp_path)`. Whole-suite testcontainers may flake — verify via grouped dir runs.
- `ruff check .` must pass.

---

## File Structure

- Modify `src/call_manager/db/models.py` — add `CollabArtifactState`.
- Create `alembic/versions/0060_collab_artifact_state.py`.
- Create `src/call_manager/proposal_assist/collab/state.py` — flag + checkpoint-watermark accessors.
- Modify `src/call_manager/proposal_assist/collab/store.py` — add `max_update_id`, `distinct_actors_since`.
- Create `src/call_manager/proposal_assist/collab/hydrate.py` — `load_or_hydrate` + `build_doc`.
- Create `src/call_manager/proposal_assist/collab/checkpoint.py` — `serialize_doc_to_markdown`, `checkpoint_artifact`.
- Modify `src/call_manager/proposal_assist/collab/room.py` — `RoomManager.release(..., on_evict=None)` + `get`.
- Modify `src/call_manager/proposal_assist/web/collab_ws.py` — hydrate in `_load`; checkpoint on last-leave.
- Create `src/call_manager/proposal_assist/web/collab_checkpoint.py` — explicit "Save a version" endpoint; mount in `main.py`.
- Modify `src/call_manager/proposal_assist/activity/feed.py` — render `"collab_checkpoint"`.
- Create `frontend/scripts/verify_python_writepath.mjs` + `frontend/scripts/writepath.test.mjs` + committed `tests/proposal_assist/collab/goldens/python_writepath.json`.
- Modify `src/call_manager/proposal_assist/collab/yjs_bridge.py` — `pm_json_to_ydoc(doc, *, client_id=None)` (additive).
- Tests: `test_collab_state.py`, `test_collab_store_checkpoint.py`, `test_collab_hydrate.py`, `test_collab_checkpoint.py`, `test_collab_checkpoint_triggers.py`, `test_collab_writepath.py`.
- Create `docs/superpowers/notes/2026-07-01-collab-slice2a-notes.md`.

**Interface summary (locked across tasks):**

```
# state.py
def is_collab_enabled(session, *, workspace_id: uuid.UUID, artifact: str) -> bool
def enable_collab(session, *, workspace_id: uuid.UUID, artifact: str) -> None
def get_checkpoint_watermark(session, *, workspace_id: uuid.UUID, artifact: str) -> int   # 0 if none
def advance_checkpoint(session, *, workspace_id, artifact, up_to_update_id: int, commit_sha: str) -> None

# store.py (added)
def max_update_id(session, *, workspace_id: uuid.UUID, artifact: str) -> int               # 0 if none
def distinct_actors_since(session, *, workspace_id, artifact, after_id: int) -> list[int]   # non-null, sorted

# hydrate.py
def load_or_hydrate(session, *, workspace_id, artifact, workspaces_dir) -> list[bytes]
def build_doc(updates: list[bytes]) -> pycrdt.Doc

# checkpoint.py
def serialize_doc_to_markdown(doc: pycrdt.Doc) -> str
@dataclass CheckpointResult: committed: bool; commit_sha: str; authors: list[int]
def checkpoint_artifact(session, *, workspace_id, artifact, doc, workspaces_dir) -> CheckpointResult

# room.py (added / changed)
class RoomManager:
    def get(self, key) -> Room | None
    async def release(self, key, *, client, on_evict=None) -> None   # on_evict: async (Room)->None, run OUTSIDE the lock
```

---

### Task 1: `CollabArtifactState` model + migration 0060 + `state.py`

**Files:** Modify `db/models.py`; Create `alembic/versions/0060_collab_artifact_state.py`, `collab/state.py`; Test `tests/proposal_assist/collab/test_collab_state.py`.

One row per `(workspace, artifact)` holding: the flag (`enabled_at` — NULL = off), the checkpoint watermark (`last_checkpoint_update_id`), and the last checkpoint commit. Composite PK `(workspace_id, artifact)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/collab/test_collab_state.py
from call_manager.proposal_assist.collab import state
from tests.proposal_assist.conftest import make_user, make_workspace


def test_flag_defaults_off_and_enable_is_idempotent(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="st-o@ki.se"))
        s.commit()
        wid = ws.id
    with pg_session_factory() as s:
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is False
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        state.enable_collab(s, workspace_id=wid, artifact="draft")  # idempotent
        s.commit()
    with pg_session_factory() as s:
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is True
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="spec") is False  # per-artifact


def test_watermark_roundtrips(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="st-w@ki.se"))
        s.commit()
        wid = ws.id
    with pg_session_factory() as s:
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == 0
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        state.advance_checkpoint(s, workspace_id=wid, artifact="draft", up_to_update_id=42, commit_sha="abc123")
        s.commit()
    with pg_session_factory() as s:
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == 42
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: ...collab.state`).

- [ ] **Step 3: Add the model** (in `db/models.py`, near `YjsUpdate`; match the file's declarative style):

```python
class CollabArtifactState(Base):
    """Per-(workspace, artifact) collaborative-editing state: the feature flag
    (`enabled_at`, NULL = off) plus the checkpoint watermark (the highest
    yjs_updates.id already checkpointed to git) and last checkpoint commit."""

    __tablename__ = "collab_artifact_state"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    artifact: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checkpoint_update_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_checkpoint_commit_sha: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("artifact IN ('draft','spec')", name="ck_collab_artifact_state_artifact"),
    )
```

- [ ] **Step 4: Migration**

```python
# alembic/versions/0060_collab_artifact_state.py
"""collab artifact state (flag + checkpoint watermark)

Revision ID: 0060
Revises: 0059
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collab_artifact_state",
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("artifact", sa.Text(), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checkpoint_update_id", sa.BigInteger(), nullable=True),
        sa.Column("last_checkpoint_commit_sha", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.CheckConstraint("artifact IN ('draft','spec')", name="ck_collab_artifact_state_artifact"),
        sa.PrimaryKeyConstraint("workspace_id", "artifact"),
    )


def downgrade() -> None:
    op.drop_table("collab_artifact_state")
```

- [ ] **Step 5: Implement `state.py`**

```python
# src/call_manager/proposal_assist/collab/state.py
"""Per-(workspace, artifact) collab flag + checkpoint watermark accessors (sync)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from call_manager.db.models import CollabArtifactState


def _get(session: Session, workspace_id: uuid.UUID, artifact: str) -> CollabArtifactState | None:
    return session.get(CollabArtifactState, (workspace_id, artifact))


def is_collab_enabled(session: Session, *, workspace_id: uuid.UUID, artifact: str) -> bool:
    row = _get(session, workspace_id, artifact)
    return bool(row and row.enabled_at is not None)


def enable_collab(session: Session, *, workspace_id: uuid.UUID, artifact: str) -> None:
    row = _get(session, workspace_id, artifact)
    if row is None:
        row = CollabArtifactState(workspace_id=workspace_id, artifact=artifact)
        session.add(row)
    if row.enabled_at is None:  # sticky: don't overwrite an existing enable time
        row.enabled_at = datetime.now(UTC)


def get_checkpoint_watermark(session: Session, *, workspace_id: uuid.UUID, artifact: str) -> int:
    row = _get(session, workspace_id, artifact)
    return row.last_checkpoint_update_id if row and row.last_checkpoint_update_id else 0


def advance_checkpoint(
    session: Session, *, workspace_id: uuid.UUID, artifact: str, up_to_update_id: int, commit_sha: str
) -> None:
    row = _get(session, workspace_id, artifact)
    if row is None:
        row = CollabArtifactState(workspace_id=workspace_id, artifact=artifact)
        session.add(row)
    row.last_checkpoint_update_id = up_to_update_id
    row.last_checkpoint_commit_sha = commit_sha
```

- [ ] **Step 6: Run — expect PASS.** `uv run pytest tests/proposal_assist/collab/test_collab_state.py -q`

- [ ] **Step 7: Commit** (`feat(collab): collab_artifact_state flag + checkpoint watermark (mig 0060)`).

---

### Task 2: store helpers — `max_update_id`, `distinct_actors_since`

**Files:** Modify `collab/store.py`; Test `tests/proposal_assist/collab/test_collab_store_checkpoint.py`.

**Interfaces:** Produces `max_update_id`, `distinct_actors_since` used by `checkpoint.py`.

- [ ] **Step 1: Failing test**

```python
# tests/proposal_assist/collab/test_collab_store_checkpoint.py
from call_manager.proposal_assist.collab import store
from tests.proposal_assist.conftest import make_user, make_workspace


def test_max_update_id_and_distinct_actors(pg_session_factory):
    with pg_session_factory() as s:
        o = make_user(s, email="mx-o@ki.se")
        u2 = make_user(s, email="mx-2@ki.se")
        ws = make_workspace(s, owner=o)
        s.commit()
        wid, oid, u2id = ws.id, o.id, u2.id
    with pg_session_factory() as s:
        assert store.max_update_id(s, workspace_id=wid, artifact="draft") == 0
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None, update=b"\x01")  # seed
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=oid, update=b"\x02")
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=u2id, update=b"\x03")
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=oid, update=b"\x04")
        s.commit()
        mx = store.max_update_id(s, workspace_id=wid, artifact="draft")
        assert mx == 4
        # distinct non-null actors after the seed (id 1):
        assert store.distinct_actors_since(s, workspace_id=wid, artifact="draft", after_id=1) == sorted([oid, u2id])
        # after id 4: none
        assert store.distinct_actors_since(s, workspace_id=wid, artifact="draft", after_id=4) == []
```

- [ ] **Step 2: Run — expect FAIL** (AttributeError: no `max_update_id`).

- [ ] **Step 3: Implement (append to `store.py`)**

```python
from sqlalchemy import func

def max_update_id(session: Session, *, workspace_id: uuid.UUID, artifact: str) -> int:
    val = session.execute(
        select(func.max(YjsUpdate.id)).where(
            YjsUpdate.workspace_id == workspace_id, YjsUpdate.artifact == artifact
        )
    ).scalar_one_or_none()
    return int(val) if val is not None else 0


def distinct_actors_since(
    session: Session, *, workspace_id: uuid.UUID, artifact: str, after_id: int
) -> list[int]:
    rows = (
        session.execute(
            select(YjsUpdate.actor_id)
            .where(
                YjsUpdate.workspace_id == workspace_id,
                YjsUpdate.artifact == artifact,
                YjsUpdate.id > after_id,
                YjsUpdate.actor_id.is_not(None),
            )
            .distinct()
        )
        .scalars()
        .all()
    )
    return sorted(int(a) for a in rows)
```

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit** (`feat(collab): store helpers for checkpoint (max id + distinct actors)`).

---

### Task 3: Hydration — `hydrate.py` + wire into the WS route

**Files:** Create `collab/hydrate.py`; Modify `web/collab_ws.py`; Test `tests/proposal_assist/collab/test_collab_hydrate.py`.

**Interfaces:** Consumes `state.is_collab_enabled`, `store.load_updates`/`append_update`, `markdown_bridge.markdown_to_pm`, `yjs_bridge.pm_json_to_ydoc`, `paths.workspace_repo_dir`, `artifacts.filename_for`. Produces `load_or_hydrate`, `build_doc`.

Cold-start rule: replay the store if it has updates; else, if collab is enabled, hydrate from git HEAD markdown and **persist the seed as update #1** (actor `None`); else return `[]` (Slice-1 empty room). The route's `_load` calls this instead of `store.load_updates`.

- [ ] **Step 1: Failing test**

```python
# tests/proposal_assist/collab/test_collab_hydrate.py
import subprocess
from pathlib import Path

from pycrdt import Doc

from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.hydrate import build_doc, load_or_hydrate
from call_manager.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
from tests.proposal_assist.conftest import make_user, make_workspace


def _init_repo(repo: Path, content: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def test_hydrates_from_git_when_store_empty_and_enabled(pg_session_factory, tmp_path):
    md = "preamble\n\n## Background\nhello **world**.\n"
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="hy-o@ki.se"))
        s.commit()
        wid = ws.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", md)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        updates = load_or_hydrate(s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path))
        s.commit()
        assert len(updates) == 1  # a single seed update
    # the seed was persisted (so the store is now canonical)
    with pg_session_factory() as s:
        assert len(store.load_updates(s, workspace_id=wid, artifact="draft")) == 1
        # and the hydrated doc reconstructs the git markdown's PM JSON
        doc = build_doc(updates)
        assert ydoc_to_pm_json(doc) == markdown_to_pm(md)


def test_no_hydrate_when_flag_off(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="hy-off@ki.se"))
        s.commit()
        wid = ws.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## X\nbody\n")
    with pg_session_factory() as s:
        assert load_or_hydrate(s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path)) == []
        assert store.load_updates(s, workspace_id=wid, artifact="draft") == []  # nothing persisted


def test_existing_updates_are_replayed_not_rehydrated(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="hy-ex@ki.se"))
        s.commit()
        wid = ws.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## X\nGIT.\n")
    # pre-existing live edits in the store (different from git):
    d = Doc()
    from pycrdt import Text
    d["body"] = Text()
    cap: list[bytes] = []
    sub = d.observe(lambda e: cap.append(e.update))
    d["body"] += "LIVE"
    d.unobserve(sub)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None, update=cap[-1])
        s.commit()
    with pg_session_factory() as s:
        updates = load_or_hydrate(s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path))
        assert updates == cap  # replayed the store, did NOT re-hydrate from git
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: ...collab.hydrate`).

- [ ] **Step 3: Implement `hydrate.py`**

```python
# src/call_manager/proposal_assist/collab/hydrate.py
"""git -> room hydration. On the first open of a collab-enabled artifact whose
Yjs store is empty, seed the room from git HEAD markdown and persist that seed
as update #1 so the store is canonical thereafter."""

from __future__ import annotations

import uuid
from pathlib import Path

from pycrdt import Doc

from call_manager.proposal_assist.artifacts import Artifact, filename_for
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from call_manager.proposal_assist.paths import workspace_repo_dir

_ARTIFACT = {"draft": Artifact.DRAFT, "spec": Artifact.SPEC}


def build_doc(updates: list[bytes]) -> Doc:
    doc = Doc()
    for u in updates:
        doc.apply_update(u)
    return doc


def _read_head_markdown(workspaces_dir, workspace_id: uuid.UUID, artifact: str) -> str:
    repo = workspace_repo_dir(workspaces_dir, workspace_id)
    path = Path(repo) / filename_for(_ARTIFACT[artifact])
    return path.read_text() if path.exists() else ""


def load_or_hydrate(session, *, workspace_id: uuid.UUID, artifact: str, workspaces_dir) -> list[bytes]:
    updates = store.load_updates(session, workspace_id=workspace_id, artifact=artifact)
    if updates:
        return updates
    if not state.is_collab_enabled(session, workspace_id=workspace_id, artifact=artifact):
        return []
    md = _read_head_markdown(workspaces_dir, workspace_id, artifact)
    seed = pm_json_to_ydoc(markdown_to_pm(md)).get_update()
    store.append_update(session, workspace_id=workspace_id, artifact=artifact, actor_id=None, update=seed)
    return [seed]
```

- [ ] **Step 4: Wire into the WS route** — in `web/collab_ws.py`, change the `_load` closure to hydrate. Replace the `store.load_updates` call:

```python
from call_manager.proposal_assist.collab.hydrate import load_or_hydrate

    workspaces_dir = websocket.app.state.settings.workspaces_dir

    async def _load() -> list[bytes]:
        def _read():
            with session_factory() as s:
                updates = load_or_hydrate(
                    s, workspace_id=workspace_id, artifact=artifact, workspaces_dir=workspaces_dir
                )
                s.commit()  # persist the seed if hydration happened
                return updates

        return await run_in_threadpool(_read)
```

(The per-key creation lock in `RoomManager.acquire` guarantees `_load` runs once per cold start, so the seed is persisted exactly once.)

- [ ] **Step 5: Run — expect PASS** (`test_collab_hydrate.py`), and re-run `test_collab_ws_route.py` + `test_collab_recovery.py` to confirm no regression (flag off by default → those tests, which never enable collab, still get empty rooms).

- [ ] **Step 6: Commit** (`feat(collab): git->room hydration (seed from HEAD markdown, persist as update #1)`).

---

### Task 4: Checkpoint core — `checkpoint.py`

**Files:** Create `collab/checkpoint.py`; Test `tests/proposal_assist/collab/test_collab_checkpoint.py`.

**Interfaces:** Consumes the bridge (`ydoc_to_pm_json`, `pm_to_markdown`), `store.max_update_id`/`distinct_actors_since`, `state.get_checkpoint_watermark`/`advance_checkpoint`, `gitio.write_and_commit_files`/`current_head`, `paths.workspace_repo_dir`, `artifacts.filename_for`, `db.models.ArtifactVersion`/`WorkspaceEvent`. Produces `serialize_doc_to_markdown`, `checkpoint_artifact`, `CheckpointResult`.

- [ ] **Step 1: Failing test**

```python
# tests/proposal_assist/collab/test_collab_checkpoint.py
import subprocess
from pathlib import Path

from pycrdt import Doc
from sqlalchemy import select

from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.checkpoint import checkpoint_artifact, serialize_doc_to_markdown
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.db.models import ArtifactVersion, WorkspaceEvent
from tests.proposal_assist.conftest import make_user, make_workspace


def _init_repo(repo: Path, content: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _seed_room_with_edit(pg_session_factory, wid, actor_id, md) -> Doc:
    # Build a REAL proposal doc through the bridge (the 'prosemirror' XmlFragment
    # the checkpoint serializer reads), persist its full-state update tagged with
    # the author, and return the doc.
    doc = pm_json_to_ydoc(markdown_to_pm(md))
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=actor_id, update=doc.get_update()
        )
        s.commit()
    return doc


def test_checkpoint_commits_serialized_doc_with_provenance(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        o = make_user(s, email="cp-o@ki.se")
        ws = make_workspace(s, owner=o)
        s.commit()
        wid, oid = ws.id, o.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## Background\noriginal.\n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()

    doc = _seed_room_with_edit(pg_session_factory, wid, oid, "## Background\n\nrewritten by owner.\n")
    expected_md = serialize_doc_to_markdown(doc)

    with pg_session_factory() as s:
        res = checkpoint_artifact(
            s, workspace_id=wid, artifact="draft", doc=doc, workspaces_dir=str(tmp_path)
        )
        s.commit()
    assert res.committed is True
    assert res.authors == [oid]
    # git now holds the serialized markdown
    disk = (Path(tmp_path) / str(wid) / "repo" / PROPOSAL_FILENAME).read_text()
    assert disk == expected_md
    assert "rewritten by owner." in disk  # real content actually reached git (not a vacuous empty doc)
    # split_sections invariant holds on the checkpoint output
    assert "".join(sec.text for sec in split_sections(disk)) == disk
    with pg_session_factory() as s:
        av = s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid)).scalars().all()
        assert len(av) == 1 and av[0].commit_sha == res.commit_sha and av[0].created_by == "user"
        ev = s.execute(
            select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == wid, WorkspaceEvent.kind == "collab_checkpoint")
        ).scalars().all()
        assert len(ev) == 1 and ev[0].payload["authors"] == [oid]
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == 1


def test_second_checkpoint_with_no_new_updates_is_noop(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        o = make_user(s, email="cp-noop@ki.se")
        ws = make_workspace(s, owner=o)
        s.commit()
        wid, oid = ws.id, o.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## X\norig.\n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    doc = _seed_room_with_edit(pg_session_factory, wid, oid, "## X\n\nedited.\n")
    with pg_session_factory() as s:
        checkpoint_artifact(s, workspace_id=wid, artifact="draft", doc=doc, workspaces_dir=str(tmp_path))
        s.commit()
    with pg_session_factory() as s:
        res2 = checkpoint_artifact(s, workspace_id=wid, artifact="draft", doc=doc, workspaces_dir=str(tmp_path))
        s.commit()
    assert res2.committed is False  # watermark already at max update id -> no-op
    with pg_session_factory() as s:
        av = s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid)).scalars().all()
        assert len(av) == 1  # no second version
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: ...collab.checkpoint`).

- [ ] **Step 3: Implement `checkpoint.py`**

```python
# src/call_manager/proposal_assist/collab/checkpoint.py
"""room -> git checkpoint: serialize the live Doc to markdown, commit it, and
record provenance (ArtifactVersion + WorkspaceEvent), co-authored from the
per-update actor log since the last checkpoint watermark."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from pycrdt import Doc
from sqlalchemy.orm import Session

from call_manager.db.models import ArtifactVersion, WorkspaceEvent
from call_manager.proposal_assist.artifacts import Artifact, filename_for
from call_manager.proposal_assist.citations.gitio import current_head, write_and_commit_files
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.markdown_bridge import pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json
from call_manager.proposal_assist.paths import workspace_repo_dir

_ARTIFACT = {"draft": Artifact.DRAFT, "spec": Artifact.SPEC}


@dataclass
class CheckpointResult:
    committed: bool
    commit_sha: str
    authors: list[int]


def serialize_doc_to_markdown(doc: Doc) -> str:
    return pm_to_markdown(ydoc_to_pm_json(doc))


def checkpoint_artifact(
    session: Session, *, workspace_id: uuid.UUID, artifact: str, doc: Doc, workspaces_dir
) -> CheckpointResult:
    max_id = store.max_update_id(session, workspace_id=workspace_id, artifact=artifact)
    watermark = state.get_checkpoint_watermark(session, workspace_id=workspace_id, artifact=artifact)
    repo = workspace_repo_dir(workspaces_dir, workspace_id)

    if max_id <= watermark:
        return CheckpointResult(committed=False, commit_sha=current_head(repo), authors=[])

    authors = store.distinct_actors_since(
        session, workspace_id=workspace_id, artifact=artifact, after_id=watermark
    )
    md = serialize_doc_to_markdown(doc)
    fname = filename_for(_ARTIFACT[artifact])
    head_before = current_head(repo)
    author_tag = ",".join(str(a) for a in authors) or "system"
    commit_sha = write_and_commit_files(
        repo, {fname: md}, f"collab checkpoint: {artifact} (authors:{author_tag})"
    )
    changed = commit_sha != head_before
    if changed:
        session.add(
            ArtifactVersion(
                workspace_id=workspace_id,
                commit_sha=commit_sha,
                created_by="user" if authors else "system",
                message=f"collab checkpoint: {artifact}",
            )
        )
        session.add(
            WorkspaceEvent(
                workspace_id=workspace_id,
                kind="collab_checkpoint",
                actor_id=authors[0] if len(authors) == 1 else None,
                payload={"artifact": artifact, "commit_sha": commit_sha, "authors": authors},
            )
        )
    state.advance_checkpoint(
        session, workspace_id=workspace_id, artifact=artifact, up_to_update_id=max_id, commit_sha=commit_sha
    )
    return CheckpointResult(committed=changed, commit_sha=commit_sha, authors=authors)
```

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit** (`feat(collab): checkpoint core (serialize live doc -> git commit + provenance)`).

---

### Task 5: Triggers — last-client-leaving + explicit endpoint + activity feed

**Files:** Modify `collab/room.py` (release `on_evict` + `get`), `web/collab_ws.py` (checkpoint on last leave), `activity/feed.py` (render kind); Create `web/collab_checkpoint.py` (explicit endpoint) + mount in `main.py`; Test `tests/proposal_assist/collab/test_collab_checkpoint_triggers.py`.

- [ ] **Step 1: `RoomManager` — `get` + `release(on_evict=)`** (in `room.py`):

```python
    def get(self, key: RoomKey) -> Room | None:
        return self._rooms.get(key)

    async def release(self, key: RoomKey, *, client, on_evict=None) -> None:
        """Deregister a client; evict the room when the last one leaves. If a
        room is evicted and `on_evict` is given, call it (e.g. to checkpoint)
        OUTSIDE the manager lock — never hold the global lock across I/O."""
        async with self._lock:
            room = self._rooms.get(key)
            if room is None:
                return
            room.clients.discard(client)
            evicted = self._rooms.pop(key) if not room.clients else None
        if evicted is not None and on_evict is not None:
            await on_evict(evicted)
```

(Existing callers that pass no `on_evict` are unaffected — default `None`.)

- [ ] **Step 2: Route — checkpoint on last leave** (in `web/collab_ws.py`): after resolving `is_member`/`can_write`, resolve `collab_enabled`, and pass an `on_evict` checkpoint closure to `release`:

```python
from call_manager.proposal_assist.collab import state as collab_state
from call_manager.proposal_assist.collab.checkpoint import checkpoint_artifact

    def _enabled():
        with session_factory() as s:
            return collab_state.is_collab_enabled(s, workspace_id=workspace_id, artifact=artifact)

    collab_enabled = await run_in_threadpool(_enabled)

    async def _checkpoint_on_evict(room) -> None:
        def _cp():
            with session_factory() as s:
                checkpoint_artifact(
                    s, workspace_id=workspace_id, artifact=artifact, doc=room.doc, workspaces_dir=workspaces_dir
                )
                s.commit()
        await run_in_threadpool(_cp)

    # ... in the finally:
    finally:
        await MANAGER.release(
            key, client=websocket, on_evict=_checkpoint_on_evict if collab_enabled else None
        )
```

- [ ] **Step 3: Explicit endpoint** (`web/collab_checkpoint.py`):

```python
# src/call_manager/proposal_assist/web/collab_checkpoint.py
"""Explicit "Save a version" checkpoint endpoint (HTTP)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import User
from call_manager.proposal_assist.collab import state as collab_state
from call_manager.proposal_assist.collab.checkpoint import checkpoint_artifact
from call_manager.proposal_assist.collab.hydrate import build_doc, load_or_hydrate
from call_manager.proposal_assist.collab.ws_auth import resolve_access

router = APIRouter()
_VALID = {"draft", "spec"}


@router.post("/api/workspaces/{workspace_id}/collab/{artifact}/checkpoint")
async def save_version(
    workspace_id: uuid.UUID, artifact: str, request: Request, user: User = Depends(get_current_user)
):
    if artifact not in _VALID:
        raise HTTPException(status_code=404, detail="unknown artifact")
    session_factory = request.app.state.session_factory
    workspaces_dir = request.app.state.settings.workspaces_dir

    def _do():
        with session_factory() as s:
            if not collab_state.is_collab_enabled(s, workspace_id=workspace_id, artifact=artifact):
                raise HTTPException(status_code=409, detail="collab not enabled for this artifact")
            is_member, can_write = resolve_access(
                s, user=user, workspace_id=workspace_id, artifact=artifact
            )
            if not is_member:
                raise HTTPException(status_code=404, detail="workspace not found")
            if not can_write:
                raise HTTPException(status_code=403, detail="read-only")
            # Store is always in sync with the live room (Slice 1 persists every
            # update), so rebuilding the doc from the store is equivalent to the
            # live room.doc and works whether or not a room is currently open.
            updates = load_or_hydrate(
                s, workspace_id=workspace_id, artifact=artifact, workspaces_dir=workspaces_dir
            )
            doc = build_doc(updates)
            res = checkpoint_artifact(
                s, workspace_id=workspace_id, artifact=artifact, doc=doc, workspaces_dir=workspaces_dir
            )
            s.commit()
            return {"committed": res.committed, "commit_sha": res.commit_sha, "authors": res.authors}

    return await run_in_threadpool(_do)
```

Mount in `main.py` (before `mount_spa`, near `collab_ws`): `from ...web import collab_checkpoint` + `app.include_router(collab_checkpoint.router)`.

- [ ] **Step 4: Activity feed** — render the new kind. In `activity/feed.py`, add a `_RENDER` entry for `"collab_checkpoint"` (category/summary consistent with the existing `"user_edit"` entry — e.g. category "edit", summary "saved a version"). Follow the exact `_RENDER` shape used by neighbouring kinds; a checkpoint payload has `{artifact, commit_sha, authors}` (no `section_index`/`heading`).

- [ ] **Step 5: Tests** (`test_collab_checkpoint_triggers.py`): use the WS `TestClient` + a workspace git repo + `state.enable_collab`. Cases:
  - **last-leave checkpoint:** enable collab; connect as owner; send an update; disconnect; assert a commit + `ArtifactVersion` + `collab_checkpoint` event now exist and the room is evicted.
  - **explicit endpoint:** enable collab; seed an update via the store; `POST /api/workspaces/{id}/collab/draft/checkpoint` with the owner cookie → 200 `committed=True`; a read-only member (setup phase) → 403; collab-disabled artifact → 409; non-member → 404.
  - **flag off → no checkpoint on disconnect:** without `enable_collab`, connect/disconnect produces no commit/version (Slice-1 behavior preserved).

Mirror the `client_authed_owner`/`_cookie`/`_init_repo` patterns from `test_collab_ws_route.py`. Point `_app.state.settings.workspaces_dir` at `tmp_path` (patch it on the app, as the route reads `app.state.settings.workspaces_dir`).

- [ ] **Step 6: Run** the triggers suite + the full `tests/proposal_assist/collab/` suite green.

- [ ] **Step 7: Commit** (`feat(collab): checkpoint triggers (last-leave + explicit endpoint) + activity feed`).

---

### Task 6: Node write-path validation (spike deferral) + Slice-2a notes

**Files:** Modify `collab/yjs_bridge.py` (`pm_json_to_ydoc` client_id param); Create `frontend/scripts/verify_python_writepath.mjs`, `frontend/scripts/writepath.test.mjs`, `tests/proposal_assist/collab/goldens/python_writepath.json`, `tests/proposal_assist/collab/test_collab_writepath.py`; Create the notes doc.

Closes the spike finding: a ydoc built by Python `pm_json_to_ydoc` must be read **identically** by a real browser `y-prosemirror`. Deterministic bytes via a fixed `client_id` so the artifact is committable and CI reads it.

- [ ] **Step 1: Additive `client_id` on `pm_json_to_ydoc`** — `def pm_json_to_ydoc(doc: dict, *, client_id: int | None = None) -> pycrdt.Doc:` passing `client_id` to `pycrdt.Doc(...)`. Default `None` preserves current behavior; existing callers/tests unaffected. Add a unit assertion that a fixed `client_id` yields byte-stable `get_update()` across two builds (if pycrdt proves non-deterministic even with a fixed client_id, drop the byte-stability assertion and have the Python producer test below assert only the Python round-trip + regenerate the artifact — note this in the test).

- [ ] **Step 2: Python producer test** (`test_collab_writepath.py`): for each Slice-0 fixture in `fixtures.CORPUS` (reuse it / the goldens' `pm_json`), build `pm_json_to_ydoc(pm, client_id=1).get_update()`, base64-encode, assemble `[{"name", "pm_json", "update_b64"}]`, and (a) assert `ydoc_to_pm_json(build) == pm` (Python round-trip), (b) write `goldens/python_writepath.json` and assert it equals the committed file (deterministic via `client_id=1`). This pins the Python producer and keeps the committed artifact fresh.

- [ ] **Step 3: Node verifier** (`frontend/scripts/verify_python_writepath.mjs`): read `../../tests/proposal_assist/collab/goldens/python_writepath.json`; for each entry, `const d = new Y.Doc(); Y.applyUpdate(d, Buffer.from(update_b64, "base64")); const pm = yDocToProsemirrorJSON(d, "prosemirror");` and deep-equal `canonicalize(pm) === canonicalize(entry.pm_json)` using the SAME canonicalization as Slice-0 `gen_collab_goldens.mjs` (strip empty `attrs:{}` / null attrs). Exit non-zero on any mismatch; print a per-fixture pass line. Import `Y` from `yjs` and `yDocToProsemirrorJSON` from `y-prosemirror` (both present).

- [ ] **Step 4: Vitest wrapper** (`frontend/scripts/writepath.test.mjs`): a `test()` that runs the verifier's per-fixture comparison (import the comparison function from the .mjs, or replicate it) and `expect(mismatches).toEqual([])`, so `pnpm vitest` includes the cross-runtime check in CI. Confirm vitest's config globs `.mjs` test files (if it only globs `frontend/src/**`, place the test where vitest discovers it and import the scripts by relative path).

- [ ] **Step 5: Run both sides**
  - `uv run pytest tests/proposal_assist/collab/test_collab_writepath.py -q` → PASS.
  - `cd frontend && pnpm vitest run writepath` → PASS (all fixtures read back identical). Also run `node scripts/verify_python_writepath.mjs` and confirm exit 0.

- [ ] **Step 6: Slice-2a notes** (`docs/superpowers/notes/2026-07-01-collab-slice2a-notes.md`): what shipped (hydration seed-as-update-#1; checkpoint core + provenance + co-authorship via watermark; triggers last-leave + explicit; `collab_artifact_state` flag; Node write-path GO); the **off-by-default flag MUST stay off in prod until 2b** (no write kernel yet); deferrals — **write kernel + accept/cite/spec-lock routing + git-canonical switchover + sticky-forward enforcement → 2b**; **mid-session permission downgrade → 2c**; **debounced-idle checkpoint trigger, Tier-1/Tier-2 post-checkpoint re-validation, Yjs-log compaction → follow-ups**; raw-HTML-dropped-on-hydration caveat.

- [ ] **Step 7: Commit** (`test(collab): Node write-path validation + Slice-2a notes`).

---

## Final verification (before whole-branch review)

```bash
uv run pytest tests/proposal_assist/collab/ -q     # Slice 0 + 1 + 2a green
uv run ruff check .
cd frontend && pnpm vitest run writepath && pnpm exec tsc --noEmit
```

---

## Self-Review (author checklist — completed)

**1. Spec coverage (Slice-2a subset):** hydration git→room (Task 3) ✓; checkpoint room→git with ArtifactVersion+WorkspaceEvent co-authorship (Task 4) ✓; triggers last-leave + explicit (Task 5) ✓; off-by-default per-artifact flag (Task 1) ✓; split_sections invariant on output (Task 4 test) ✓; Node write-path validation (Task 6) ✓. Deferred-and-documented: write kernel/consumers/switchover (2b), mid-session downgrade (2c), debounced-idle + Tier-1/2 re-fire + compaction (follow-ups).

**2. Placeholder scan:** every code step carries complete code; the two "confirm/adjust" notes (pycrdt byte-determinism under fixed client_id; vitest `.mjs` globbing) are explicit verification points, not hand-waves.

**3. Type consistency:** `load_or_hydrate`/`build_doc`, `checkpoint_artifact(doc=...)`/`CheckpointResult`, `state.*` signatures, `store.max_update_id`/`distinct_actors_since`, `RoomManager.release(on_evict=)`/`get` are used identically across Tasks 3–5. `artifact` is the string `"draft"`/`"spec"` throughout, mapped to `Artifact` only at the git-filename boundary (`_ARTIFACT`).

**4. Safety invariant flagged for the implementer:** the flag stays OFF by default; enabling it in prod before 2b would let the old git-first write paths (`save_section`/`human_cite`/spec-lock) fight checkpoints — 2a is a substrate, verified only via tests that opt in with `enable_collab`.
