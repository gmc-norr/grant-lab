# Cockpit SP2a — Propose→Accept Substrate (prose path) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the backend `proposed_change` substrate — a table, a propose service, an accept/reject service that replays the existing `save_section` mutation as a named approver under the commit mutex, sha-driven staleness, and removal of the `save_section` turn-freeze hard guard — so AI mutations can land as reviewable proposals instead of direct commits.

**Architecture:** A new `ProposedChange` SQLAlchemy model + Alembic migration. A `proposing.py` service writes pending rows (capturing the current `base_commit_sha`/`base_section_sha`). An `accepting.py` service replays `save_section` for the **draft** target and records the approver + resulting commit; `StaleSectionError` from `save_section` is the authoritative stale guard. A `refresh_staleness` helper proactively flips out-of-date pending rows. FastAPI accept/reject/list endpoints (member-gated) expose it. The workspace-wide `_turn_in_flight` hard guard is deleted from `save_section` (mechanism B means the agent no longer commits, so the freeze is obsolete).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, pytest + testcontainers Postgres, git-backed per-workspace repos.

**Scope note:** This is the **first** of several SP2a plans. **Out of scope here** (later plans): the agent-facing `propose_edit`/`propose_citation` MCP tools + SDK allowlist swap + the **citation** proposal path (Plan SP2a-2); the Lexical editor (gated by the editor round-trip spike); select-to-cite; the React review-inbox UI. This plan exercises only the **draft** target and **prose** kind; the `target_artifact`/`kind` columns are spine-aware so the spec path and citation path drop in without schema change.

---

### Task 1: `ProposedChange` model + migration

**Files:**
- Modify: `src/call_manager/db/models.py` (add the model near the other workspace-scoped models, e.g. after `ArtifactVersion` ~line 948)
- Create: `alembic/versions/0051_proposed_changes.py`
- Test: `tests/proposal_assist/test_proposed_change_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/test_proposed_change_model.py
import uuid

from call_manager.db.models import ProposedChange
from tests.proposal_assist.conftest import make_workspace


def test_proposed_change_row_roundtrips(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        pc = ProposedChange(
            workspace_id=ws.id,
            target_artifact="draft",
            section_index=1,
            base_commit_sha="a" * 40,
            base_section_sha="b" * 64,
            kind="prose_edit",
            payload={"new_text": "## Methods\nrewritten.\n"},
            origin_kind="interactive_turn",
        )
        s.add(pc)
        s.commit()
        got = s.get(ProposedChange, pc.id)
        assert got.status == "pending"
        assert got.payload["new_text"].startswith("## Methods")
        assert got.created_at is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_proposed_change_model.py -v`
Expected: FAIL with `ImportError: cannot import name 'ProposedChange'`

- [ ] **Step 3: Add the model**

Add to `src/call_manager/db/models.py` (the imports it needs — `JSONB`, `UUID`, `CheckConstraint`, `Index`, `func`, `text`, `BigInteger`, `Integer`, `Text`, `DateTime`, `ForeignKey`, `Mapped`, `mapped_column` — are already imported at the top of the file):

```python
class ProposedChange(Base):
    __tablename__ = "proposed_changes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','accepted','rejected','superseded','stale')",
            name="ck_proposed_changes_status",
        ),
        CheckConstraint(
            "target_artifact IN ('draft','spec')",
            name="ck_proposed_changes_target_artifact",
        ),
        CheckConstraint(
            "kind IN ('prose_edit','citation_insert','citation_remove','citation_update')",
            name="ck_proposed_changes_kind",
        ),
        CheckConstraint(
            "origin_kind IN ('interactive_turn','coherence_session',"
            "'evaluator_session','human')",
            name="ck_proposed_changes_origin_kind",
        ),
        Index("ix_proposed_changes_ws_status", "workspace_id", "status"),
        Index(
            "ix_proposed_changes_ws_target",
            "workspace_id",
            "target_artifact",
            "section_index",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    target_artifact: Mapped[str] = mapped_column(Text, nullable=False)
    section_index: Mapped[int] = mapped_column(Integer, nullable=False)
    base_commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    base_section_sha: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    origin_kind: Mapped[str] = mapped_column(Text, nullable=False)
    origin_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    triggering_turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_turns.id", ondelete="SET NULL"),
        nullable=True,
    )
    triggering_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resulting_commit_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

- [ ] **Step 4: Write the migration**

```python
# alembic/versions/0051_proposed_changes.py
"""proposed_changes table

Revision ID: 0051
Revises: 0050
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proposed_changes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("target_artifact", sa.Text(), nullable=False),
        sa.Column("section_index", sa.Integer(), nullable=False),
        sa.Column("base_commit_sha", sa.Text(), nullable=False),
        sa.Column("base_section_sha", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("origin_kind", sa.Text(), nullable=False),
        sa.Column("origin_session_id", sa.Text(), nullable=True),
        sa.Column("origin_job_id", UUID(as_uuid=True), nullable=True),
        sa.Column("triggering_turn_id", UUID(as_uuid=True), nullable=True),
        sa.Column("triggering_prompt", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("resolved_by", sa.BigInteger(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resulting_commit_sha", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE", name="fk_pc_workspace"
        ),
        sa.ForeignKeyConstraint(
            ["triggering_turn_id"], ["chat_turns.id"], ondelete="SET NULL", name="fk_pc_turn"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL", name="fk_pc_created_by"
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"], ["users.id"], ondelete="SET NULL", name="fk_pc_resolved_by"
        ),
    )
    op.create_check_constraint(
        "ck_proposed_changes_status",
        "proposed_changes",
        "status IN ('pending','accepted','rejected','superseded','stale')",
    )
    op.create_check_constraint(
        "ck_proposed_changes_target_artifact",
        "proposed_changes",
        "target_artifact IN ('draft','spec')",
    )
    op.create_check_constraint(
        "ck_proposed_changes_kind",
        "proposed_changes",
        "kind IN ('prose_edit','citation_insert','citation_remove','citation_update')",
    )
    op.create_check_constraint(
        "ck_proposed_changes_origin_kind",
        "proposed_changes",
        "origin_kind IN ('interactive_turn','coherence_session','evaluator_session','human')",
    )
    op.create_index(
        "ix_proposed_changes_ws_status", "proposed_changes", ["workspace_id", "status"]
    )
    op.create_index(
        "ix_proposed_changes_ws_target",
        "proposed_changes",
        ["workspace_id", "target_artifact", "section_index"],
    )


def downgrade() -> None:
    op.drop_table("proposed_changes")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/proposal_assist/test_proposed_change_model.py -v`
Expected: PASS (the testcontainer creates tables from metadata; if the suite instead runs migrations, run `uv run alembic upgrade head` first).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/db/models.py alembic/versions/0051_proposed_changes.py tests/proposal_assist/test_proposed_change_model.py
git commit -m "feat(proposal-assist): add proposed_changes table"
```

---

### Task 2: Remove the turn-freeze hard guard from `save_section` (partial — see scope note)

**Files:**
- Modify: `src/call_manager/proposal_assist/editing/save.py:163-166` (remove the hard guard)
- Test: `tests/proposal_assist/test_save_section_no_freeze.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/test_save_section_no_freeze.py
import subprocess
import uuid
from pathlib import Path

from call_manager.db.models import TurnJob
from call_manager.proposal_assist.editing.save import save_section
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from tests.proposal_assist.conftest import make_workspace


def _init_repo(repo_dir: Path, content: str) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def test_save_section_succeeds_while_turn_in_flight(pg_session_factory, tmp_path):
    from call_manager.proposal_assist.editing.sectionizer import split_sections

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        # A queued turn is in flight for this workspace.
        s.add(TurnJob(workspace_id=ws.id, status="queued"))
        s.flush()

        content = "preamble\n\n## Methods\noriginal.\n"
        repo = Path(tmp_path) / str(ws.id) / "repo"
        head = _init_repo(repo, content)
        sec_sha = split_sections(content)[1].sha

        result = save_section(
            session=s,
            workspace=ws,
            user_id=ws.owner_id,
            section_index=1,
            new_text="## Methods\nrewritten.\n",
            base_commit_sha=head,
            base_section_sha=sec_sha,
            workspaces_dir=str(tmp_path),
        )
        assert result.commit_sha  # no TurnInFlightError was raised
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_save_section_no_freeze.py -v`
Expected: FAIL — `TurnInFlightError: an agent turn is in flight ...`

- [ ] **Step 3: Remove the hard guard**

In `src/call_manager/proposal_assist/editing/save.py`, delete these lines (currently 163-166):

```python
    if _turn_in_flight(session, workspace.id):
        raise TurnInFlightError(
            f"an agent turn is in flight for workspace {workspace.id}; retry after it finishes"
        )
```

**Scope of this task:** this removes ONLY the hard guard inside `save_section`. The workspace-wide freeze is *also* enforced by two **soft pre-checks not touched here**, removed (with their own tests) in the route-porting / citation plans: the section-edit web route (`web/sections.py:186` — blocks opening the editor while a turn is queued/running) and the annotation citation-insert path (`analysis/insert.py:114`). Leave the `_turn_in_flight` function (`save.py:68-83`) and `TurnInFlightError` in place — those soft checks still import them. Add a one-line comment where the deleted guard was:

```python
    # NOTE: the workspace-wide turn-freeze was removed (mechanism B: the agent
    # no longer commits to proposal.md). Concurrency now rests on the commit
    # mutex + base_section_sha staleness alone.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/proposal_assist/test_save_section_no_freeze.py -v`
Expected: PASS

- [ ] **Step 5: Check for now-stale tests**

Run: `uv run pytest tests/proposal_assist/ -k "turn_in_flight or in_flight" -v`
Expected: any test asserting `save_section` raises `TurnInFlightError` now fails. Update each such test to assert success (mirroring Step 1). If a test covers the *web* soft pre-check (not `save_section`), leave it — that path is unchanged.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(proposal-assist): delete workspace-wide turn-freeze from save_section"
```

---

### Task 3: Propose service (prose) — write a pending row

**Files:**
- Create: `src/call_manager/proposal_assist/proposals/__init__.py` (empty)
- Create: `src/call_manager/proposal_assist/proposals/proposing.py`
- Test: `tests/proposal_assist/proposals/test_proposing.py` (create `tests/proposal_assist/proposals/__init__.py` too)

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/proposals/test_proposing.py
import subprocess
import uuid
from pathlib import Path

from call_manager.db.models import ProposedChange
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.proposals.proposing import (
    capture_base,
    propose_prose_edit,
)
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from tests.proposal_assist.conftest import make_workspace


def _init_repo(repo_dir: Path, content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def test_propose_prose_edit_captures_base(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        content = "preamble\n\n## Methods\noriginal.\n"
        repo = Path(tmp_path) / str(ws.id) / "repo"
        _init_repo(repo, content)

        base_commit, base_section = capture_base(repo, 1)
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\nrewritten.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            origin_kind="interactive_turn",
        )
        s.flush()

        got = s.get(ProposedChange, pc.id)
        assert got.status == "pending"
        assert got.target_artifact == "draft"
        assert got.kind == "prose_edit"
        assert got.base_section_sha == split_sections(content)[1].sha
        assert len(got.base_commit_sha) == 40
        assert got.payload == {"new_text": "## Methods\nrewritten.\n"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/proposal_assist/proposals/test_proposing.py -v`
Expected: FAIL with `ModuleNotFoundError: ... proposing`

- [ ] **Step 3: Implement the service**

```python
# src/call_manager/proposal_assist/proposals/proposing.py
"""Write pending proposed_change rows. Cheap (a DB insert) — no git write,
no commit mutex. The caller owns the transaction.

CRITICAL (anti-clobber): the base (commit + section sha) MUST be the snapshot
the proposer actually *read*, NOT recomputed live at propose time. With the
turn-freeze gone a human can edit the section between the proposer's read and
its propose call; recomputing live would capture that human edit as the base,
so accept would replay over it silently. Callers snapshot the base at READ time
via `capture_base` (the agent uses its `agent_view_head` + the section sha it
saw). A base that has since moved makes accept go `stale` instead of clobbering.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from call_manager.db.models import ProposedChange
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME


def capture_base(repo_dir: Path, section_index: int) -> tuple[str, str]:
    """Snapshot (head_commit_sha, section_sha) of the draft at READ time.
    Call this when the proposer *reads* the section, not when it proposes."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    proposal = (repo_dir / PROPOSAL_FILENAME).read_text()
    sections = split_sections(proposal)
    return head, sections[section_index].sha


def propose_prose_edit(
    *,
    session: Session,
    workspace_id: uuid.UUID,
    section_index: int,
    new_text: str,
    base_commit_sha: str,
    base_section_sha: str,
    origin_kind: str,
    origin_session_id: str | None = None,
    origin_job_id: uuid.UUID | None = None,
    triggering_turn_id: uuid.UUID | None = None,
    triggering_prompt: str | None = None,
    created_by: int | None = None,
) -> ProposedChange:
    pc = ProposedChange(
        workspace_id=workspace_id,
        target_artifact="draft",
        section_index=section_index,
        base_commit_sha=base_commit_sha,
        base_section_sha=base_section_sha,
        kind="prose_edit",
        payload={"new_text": new_text},
        origin_kind=origin_kind,
        origin_session_id=origin_session_id,
        origin_job_id=origin_job_id,
        triggering_turn_id=triggering_turn_id,
        triggering_prompt=triggering_prompt,
        created_by=created_by,
    )
    session.add(pc)
    session.flush()
    return pc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/proposal_assist/proposals/test_proposing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/proposals/ tests/proposal_assist/proposals/
git commit -m "feat(proposal-assist): propose_prose_edit service writes pending proposed_change"
```

---

### Task 4: Accept service (prose) — replay `save_section` as approver

**Files:**
- Create: `src/call_manager/proposal_assist/proposals/accepting.py`
- Test: `tests/proposal_assist/proposals/test_accepting.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/proposals/test_accepting.py
import subprocess
from pathlib import Path

import pytest

from call_manager.db.models import ProposedChange
from call_manager.proposal_assist.editing.save import save_section
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.proposals.accepting import (
    ProposedChangeNotFound,
    ProposedChangeNotPending,
    accept_proposed_change,
)
from call_manager.proposal_assist.proposals.proposing import (
    capture_base,
    propose_prose_edit,
)
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from tests.proposal_assist.conftest import make_user, make_workspace


def _init_repo(repo_dir: Path, content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def _setup(s, tmp_path):
    ws = make_workspace(s)
    s.flush()
    repo = Path(tmp_path) / str(ws.id) / "repo"
    _init_repo(repo, "preamble\n\n## Methods\noriginal.\n")
    return ws, repo


def test_accept_prose_applies_and_records_approver(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_prose_edit(
            session=s, workspace_id=ws.id, section_index=1,
            new_text="## Methods\nrewritten.\n", base_commit_sha=base_commit,
            base_section_sha=base_section, origin_kind="interactive_turn",
        )
        s.flush()

        accept_proposed_change(
            session=s, proposed_change_id=pc.id, workspace_id=ws.id,
            approver=approver, workspaces_dir=str(tmp_path),
        )
        s.flush()

        got = s.get(ProposedChange, pc.id)
        assert got.status == "accepted"
        assert got.resolved_by == approver.id
        assert got.resulting_commit_sha
        assert "rewritten." in (repo / PROPOSAL_FILENAME).read_text()


def test_accept_non_pending_is_rejected(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_prose_edit(
            session=s, workspace_id=ws.id, section_index=1,
            new_text="## Methods\nrewritten.\n", base_commit_sha=base_commit,
            base_section_sha=base_section, origin_kind="interactive_turn",
        )
        pc.status = "rejected"
        s.flush()
        with pytest.raises(ProposedChangeNotPending):
            accept_proposed_change(
                session=s, proposed_change_id=pc.id, workspace_id=ws.id,
                approver=approver, workspaces_dir=str(tmp_path),
            )


def test_accept_does_not_clobber_human_edit_race(pg_session_factory, tmp_path):
    # Proposer READS at the original base; a human edits the SAME section before
    # the proposal is accepted. Accept must go stale, not overwrite (finding #1).
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()

        base_commit, base_section = capture_base(repo, 1)  # T0: proposer's read

        # T1: a human edits the section and commits — section sha moves.
        save_section(
            session=s, workspace=ws, user_id=ws.owner_id, section_index=1,
            new_text="## Methods\nhuman-edited.\n", base_commit_sha=base_commit,
            base_section_sha=base_section, workspaces_dir=str(tmp_path),
        )
        s.flush()

        # T2: proposer proposes using the now-stale T0 snapshot.
        pc = propose_prose_edit(
            session=s, workspace_id=ws.id, section_index=1,
            new_text="## Methods\nagent-version.\n", base_commit_sha=base_commit,
            base_section_sha=base_section, origin_kind="interactive_turn",
        )
        s.flush()

        accept_proposed_change(
            session=s, proposed_change_id=pc.id, workspace_id=ws.id,
            approver=approver, workspaces_dir=str(tmp_path),
        )
        s.flush()

        assert s.get(ProposedChange, pc.id).status == "stale"
        text = (repo / PROPOSAL_FILENAME).read_text()
        assert "human-edited." in text  # human edit preserved
        assert "agent-version." not in text  # agent proposal NOT applied


def test_accept_rejects_mismatched_workspace(pg_session_factory, tmp_path):
    # A member of workspace B cannot accept a proposed_change from workspace A
    # by calling it under B's path (finding #2).
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        other = make_workspace(s)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_prose_edit(
            session=s, workspace_id=ws.id, section_index=1,
            new_text="## Methods\nx.\n", base_commit_sha=base_commit,
            base_section_sha=base_section, origin_kind="interactive_turn",
        )
        s.flush()
        with pytest.raises(ProposedChangeNotFound):
            accept_proposed_change(
                session=s, proposed_change_id=pc.id, workspace_id=other.id,
                approver=approver, workspaces_dir=str(tmp_path),
            )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/proposal_assist/proposals/test_accepting.py -v`
Expected: FAIL with `ModuleNotFoundError: ... accepting`

- [ ] **Step 3: Implement the accept service**

```python
# src/call_manager/proposal_assist/proposals/accepting.py
"""Accept a pending proposed_change by REPLAYING the existing mutation
function as the named approver, under the commit mutex (via save_section).
The caller owns the transaction."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import ProposedChange, Workspace
from call_manager.proposal_assist.editing.save import StaleSectionError, save_section


class ProposedChangeNotFound(Exception):
    pass


class ProposedChangeNotPending(Exception):
    def __init__(self, status: str) -> None:
        super().__init__(f"proposed_change is {status!r}, not pending")
        self.status = status


def _load_scoped(
    session: Session, proposed_change_id: uuid.UUID, workspace_id: uuid.UUID
) -> ProposedChange:
    """Fetch by BOTH id AND workspace_id, so a member of workspace A can never
    act on a proposed_change id belonging to workspace B (finding #2)."""
    pc = session.scalar(
        select(ProposedChange).where(
            ProposedChange.id == proposed_change_id,
            ProposedChange.workspace_id == workspace_id,
        )
    )
    if pc is None:
        raise ProposedChangeNotFound(str(proposed_change_id))
    return pc


def accept_proposed_change(
    *,
    session: Session,
    proposed_change_id: uuid.UUID,
    workspace_id: uuid.UUID,
    approver,
    workspaces_dir: Path | str,
) -> ProposedChange:
    pc = _load_scoped(session, proposed_change_id, workspace_id)
    if pc.status != "pending":
        raise ProposedChangeNotPending(pc.status)

    # SP2a exercises the draft + prose path only; spec/citation land in later plans.
    if pc.target_artifact != "draft":
        raise NotImplementedError("spec target is SP2b")
    if pc.kind != "prose_edit":
        raise NotImplementedError("citation_insert accept is SP2a-2")

    ws = session.get(Workspace, pc.workspace_id)
    try:
        result = save_section(
            session=session,
            workspace=ws,
            user_id=approver.id,
            section_index=pc.section_index,
            new_text=pc.payload["new_text"],
            base_commit_sha=pc.base_commit_sha,
            base_section_sha=pc.base_section_sha,
            workspaces_dir=workspaces_dir,
        )
    except StaleSectionError:
        pc.status = "stale"
        session.flush()
        return pc

    pc.status = "accepted"
    pc.resolved_by = approver.id
    pc.resolved_at = datetime.now(timezone.utc)
    pc.resulting_commit_sha = result.commit_sha
    session.flush()
    return pc
```

Confirm the import path of `StaleSectionError` (it is raised by `save_section` in `editing/save.py`; import it from wherever it is defined — `editing/save.py` or an `editing/errors.py`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/proposal_assist/proposals/test_accepting.py -v`
Expected: PASS (all four)

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/proposals/accepting.py tests/proposal_assist/proposals/test_accepting.py
git commit -m "feat(proposal-assist): accept_proposed_change replays save_section as approver"
```

---

### Task 5: Reject service

**Files:**
- Modify: `src/call_manager/proposal_assist/proposals/accepting.py` (add `reject_proposed_change`)
- Test: `tests/proposal_assist/proposals/test_accepting.py` (add a test)

- [ ] **Step 1: Write the failing test** (append to `test_accepting.py`)

```python
def test_reject_sets_status_and_approver(pg_session_factory, tmp_path):
    from call_manager.proposal_assist.proposals.accepting import reject_proposed_change

    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_prose_edit(
            session=s, workspace_id=ws.id, section_index=1,
            new_text="## Methods\nx.\n", base_commit_sha=base_commit,
            base_section_sha=base_section, origin_kind="interactive_turn",
        )
        s.flush()
        reject_proposed_change(
            session=s, proposed_change_id=pc.id, workspace_id=ws.id, approver=approver
        )
        s.flush()
        got = s.get(ProposedChange, pc.id)
        assert got.status == "rejected"
        assert got.resolved_by == approver.id
        assert got.resolved_at is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/proposal_assist/proposals/test_accepting.py::test_reject_sets_status_and_approver -v`
Expected: FAIL with `ImportError: cannot import name 'reject_proposed_change'`

- [ ] **Step 3: Implement** (append to `accepting.py`)

```python
def reject_proposed_change(
    *,
    session: Session,
    proposed_change_id: uuid.UUID,
    workspace_id: uuid.UUID,
    approver,
) -> ProposedChange:
    pc = _load_scoped(session, proposed_change_id, workspace_id)
    if pc.status != "pending":
        raise ProposedChangeNotPending(pc.status)
    pc.status = "rejected"
    pc.resolved_by = approver.id
    pc.resolved_at = datetime.now(timezone.utc)
    session.flush()
    return pc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/proposal_assist/proposals/test_accepting.py::test_reject_sets_status_and_approver -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(proposal-assist): reject_proposed_change service"
```

---

### Task 6: `refresh_staleness` — proactively flip out-of-date pending rows

**Files:**
- Create: `src/call_manager/proposal_assist/proposals/staleness.py`
- Test: `tests/proposal_assist/proposals/test_staleness.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/proposals/test_staleness.py
import subprocess
from pathlib import Path

from call_manager.db.models import ProposedChange
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.editing.save import save_section
from call_manager.proposal_assist.proposals.proposing import capture_base, propose_prose_edit
from call_manager.proposal_assist.proposals.staleness import refresh_staleness
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from tests.proposal_assist.conftest import make_workspace


def _init_repo(repo_dir: Path, content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def test_refresh_marks_moved_section_stale_only(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        content = "preamble\n\n## Methods\noriginal.\n\n## Impact\nx.\n"
        repo = Path(tmp_path) / str(ws.id) / "repo"
        _init_repo(repo, content)
        bc1, bs1 = capture_base(repo, 1)
        bc2, bs2 = capture_base(repo, 2)
        pc_methods = propose_prose_edit(
            session=s, workspace_id=ws.id, section_index=1,
            new_text="## Methods\np.\n", base_commit_sha=bc1, base_section_sha=bs1,
            origin_kind="interactive_turn",
        )
        pc_impact = propose_prose_edit(
            session=s, workspace_id=ws.id, section_index=2,
            new_text="## Impact\np.\n", base_commit_sha=bc2, base_section_sha=bs2,
            origin_kind="interactive_turn",
        )
        s.flush()
        # Methods moves; Impact does not.
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(tmp_path) / str(ws.id) / "repo",
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        save_section(
            session=s, workspace=ws, user_id=ws.owner_id, section_index=1,
            new_text="## Methods\nmoved.\n", base_commit_sha=head,
            base_section_sha=split_sections(content)[1].sha, workspaces_dir=str(tmp_path),
        )
        s.flush()

        refresh_staleness(session=s, workspace_id=ws.id, workspaces_dir=str(tmp_path))
        s.flush()

        assert s.get(ProposedChange, pc_methods.id).status == "stale"
        assert s.get(ProposedChange, pc_impact.id).status == "pending"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/proposal_assist/proposals/test_staleness.py -v`
Expected: FAIL with `ModuleNotFoundError: ... staleness`

- [ ] **Step 3: Implement**

```python
# src/call_manager/proposal_assist/proposals/staleness.py
"""Flip pending proposed_change rows to 'stale' when their targeted section's
base_section_sha no longer matches the live draft. Called by the list endpoint
(proactive UX) — accept-time StaleSectionError is the authoritative guard."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import ProposedChange
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME


def refresh_staleness(
    *, session: Session, workspace_id: uuid.UUID, workspaces_dir: Path | str
) -> int:
    repo_dir = workspace_repo_dir(workspaces_dir, workspace_id)
    sections = split_sections((repo_dir / PROPOSAL_FILENAME).read_text())
    live_sha = {i: sec.sha for i, sec in enumerate(sections)}

    rows = session.scalars(
        select(ProposedChange).where(
            ProposedChange.workspace_id == workspace_id,
            ProposedChange.status == "pending",
            ProposedChange.target_artifact == "draft",
        )
    ).all()
    n = 0
    for pc in rows:
        current = live_sha.get(pc.section_index)
        if current is None or current != pc.base_section_sha:
            pc.status = "stale"
            n += 1
    session.flush()
    return n
```

NOTE: remove the stray `// live` — that line is `sections = split_sections((repo_dir / PROPOSAL_FILENAME).read_text())`. (Typo guard: the value is the list of `Section` objects; `enumerate` gives `(index, section)`.)

`split_sections` returns a list of `Section` objects (each with a `.sha` property); `enumerate` gives `(index, section)`, keyed by section index to match `ProposedChange.section_index`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/proposal_assist/proposals/test_staleness.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/proposals/staleness.py tests/proposal_assist/proposals/test_staleness.py
git commit -m "feat(proposal-assist): refresh_staleness flips moved-section proposals to stale"
```

---

### Task 7: FastAPI endpoints — list / accept / reject (member-gated)

**Files:**
- Create: `src/call_manager/proposal_assist/web/proposals.py`
- Modify: wherever routers are registered (search for `include_router(` + `sections` in `src/call_manager/proposal_assist/web/` or `src/call_manager/main.py`) — register the new router the same way `sections.router` is registered.
- Test: `tests/proposal_assist/web/test_proposals_api.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/web/test_proposals_api.py
import subprocess
from pathlib import Path

from call_manager.proposal_assist.proposals.proposing import capture_base, propose_prose_edit
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from tests.proposal_assist.conftest import make_workspace


def _init_repo(repo_dir: Path, content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def test_accept_endpoint_applies_change(client, _app, current_user, pg_session_factory, tmp_path):
    # current_user owns the workspace; app.workspaces_dir points at tmp_path.
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        repo = Path(tmp_path) / str(ws.id) / "repo"
        _init_repo(repo, "preamble\n\n## Methods\norig.\n")
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_prose_edit(
            session=s, workspace_id=ws.id, section_index=1,
            new_text="## Methods\nnew.\n", base_commit_sha=base_commit,
            base_section_sha=base_section, origin_kind="interactive_turn",
        )
        s.commit()
        pc_id = str(pc.id)
        ws_id = str(ws.id)

    resp = client.post(f"/workspaces/{ws_id}/proposed-changes/{pc_id}/accept")
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
```

If the test harness has no `client` fixture, mirror the HTTP-client fixture used by an existing `tests/proposal_assist/web/` test module; otherwise call the service directly and assert on the DB (drop the HTTP layer from this test and keep the route thin).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/proposal_assist/web/test_proposals_api.py -v`
Expected: FAIL (route 404 — not registered yet)

- [ ] **Step 3: Implement the router**

```python
# src/call_manager/proposal_assist/web/proposals.py
"""Member-gated propose→accept endpoints for proposed_changes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import ProposedChange
from call_manager.proposal_assist.proposals.accepting import (
    ProposedChangeNotFound,
    ProposedChangeNotPending,
    accept_proposed_change,
    reject_proposed_change,
)
from call_manager.proposal_assist.drafting.locking import CommitMutexTimeout
from call_manager.proposal_assist.editing.save import SectionStructureError
from call_manager.proposal_assist.proposals.staleness import refresh_staleness
from call_manager.proposal_assist.web._perms import require_member
from call_manager.proposal_assist.web.workspaces import _session_dep

router = APIRouter(prefix="/workspaces", tags=["proposed-changes"])


def _serialize(pc: ProposedChange) -> dict:
    return {
        "id": str(pc.id),
        "status": pc.status,
        "target_artifact": pc.target_artifact,
        "section_index": pc.section_index,
        "kind": pc.kind,
        "origin_kind": pc.origin_kind,
        "resulting_commit_sha": pc.resulting_commit_sha,
    }


@router.get("/{workspace_id}/proposed-changes")
def list_proposed_changes(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),
    user=Depends(get_current_user),
) -> dict:
    require_member(workspace_id=workspace_id, user=user, session=session)
    workspaces_dir = request.app.state.settings.workspaces_dir
    refresh_staleness(session=session, workspace_id=workspace_id, workspaces_dir=workspaces_dir)
    rows = session.scalars(
        select(ProposedChange)
        .where(
            ProposedChange.workspace_id == workspace_id,
            ProposedChange.status == "pending",
        )
        .order_by(ProposedChange.created_at)
    ).all()
    session.commit()
    return {"items": [_serialize(pc) for pc in rows]}


@router.post("/{workspace_id}/proposed-changes/{pc_id}/accept")
def accept(
    workspace_id: uuid.UUID,
    pc_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),
    user=Depends(get_current_user),
) -> dict:
    require_member(workspace_id=workspace_id, user=user, session=session)
    workspaces_dir = request.app.state.settings.workspaces_dir
    try:
        pc = accept_proposed_change(
            session=session, proposed_change_id=pc_id, workspace_id=workspace_id,
            approver=user, workspaces_dir=workspaces_dir,
        )
    except ProposedChangeNotFound:
        raise HTTPException(status_code=404, detail="proposed change not found")
    except ProposedChangeNotPending as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except SectionStructureError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except CommitMutexTimeout:
        raise HTTPException(status_code=503, detail="workspace busy; retry")
    session.commit()
    return _serialize(pc)


@router.post("/{workspace_id}/proposed-changes/{pc_id}/reject")
def reject(
    workspace_id: uuid.UUID,
    pc_id: uuid.UUID,
    session: Session = Depends(_session_dep),
    user=Depends(get_current_user),
) -> dict:
    require_member(workspace_id=workspace_id, user=user, session=session)
    try:
        pc = reject_proposed_change(
            session=session, proposed_change_id=pc_id, workspace_id=workspace_id, approver=user
        )
    except ProposedChangeNotFound:
        raise HTTPException(status_code=404, detail="proposed change not found")
    except ProposedChangeNotPending as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    session.commit()
    return _serialize(pc)
```

Register the router where the other proposal-assist routers are registered (mirror `sections.router`):

```python
from call_manager.proposal_assist.web import proposals as _proposals
app.include_router(_proposals.router)
```

Confirm `request.app.state.settings.workspaces_dir` is the right accessor by checking how `sections.py`/`workspaces.py` reach `workspaces_dir`; use the identical accessor.

**Error mapping:** the accept route replays `save_section`, which can also raise `GitCommitError`, `IndexError`, and others. Mirror the FULL set of handlers from `web/sections.py:255` — import the same exception types and reuse the same status codes; the two shown above (`SectionStructureError`→422, `CommitMutexTimeout`→503) are the pattern, not the complete list.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/proposal_assist/web/test_proposals_api.py -v`
Expected: PASS

- [ ] **Step 5: Run the full proposal_assist suite**

Run: `uv run pytest tests/proposal_assist/ -v`
Expected: PASS (re-run any flagged failure in isolation — the full suite is known to be infra-flaky under load).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/web/proposals.py tests/proposal_assist/web/test_proposals_api.py
git add -A  # router registration
git commit -m "feat(proposal-assist): list/accept/reject proposed-change endpoints (member-gated)"
```

---

## Self-Review

**Spec coverage (against `2026-06-17-cockpit-workspace-design.md` §5–§6):**
- `proposed_change` table + per-section full-text + lifecycle + provenance columns → Task 1. ✓ (`superseded` status is in the CHECK but is set in SP2a-2 when the agent re-proposes — noted.)
- Accept = replay `save_section` as named approver under the mutex → Task 4. ✓
- `stale` = `base_section_sha` mismatch; accept-time `StaleSectionError` authoritative + proactive `refresh_staleness` → Tasks 4 & 6. ✓
- Delete workspace-wide turn-freeze → Task 2. ✓
- Member-gated surface → Task 7 (`require_member`). ✓
- **Deferred (correctly out of scope, noted in header):** agent `propose_*` tools + allowlist swap; citation-proposal path (`insert_citation_impl` replay); `origin_kind` values other than `interactive_turn`; the spec target. These are SP2a-2 / SP2b.

**Placeholder scan:** none — every step has runnable code/commands. Two explicit "confirm the accessor/path" notes (`workspace_repo_dir` signature, `StaleSectionError` import, `request.app.state.settings.workspaces_dir`, the `client` fixture) are verification prompts, not placeholders — each names the exact thing to check and the fallback.

**Type consistency:** `propose_prose_edit` / `accept_proposed_change` / `reject_proposed_change` / `refresh_staleness` signatures match their call sites in the endpoints and tests; `ProposedChange` column names match the model, migration, and `_serialize`. Payload shape `{"new_text": ...}` is written in Task 3 and read in Task 4.

**Scope check:** one coherent, independently-testable deliverable (the prose propose→accept substrate). The remaining SP2a plans (agent tools+citations, Lexical editor, select-to-cite, review-inbox UI) are listed in the header and gated where noted (editor work waits on the round-trip spike).

**Review hardening (Codex round 2, 2026-06-17):**
1. **Clobber race (HIGH):** propose takes the **read-snapshot base** (`base_commit_sha`/`base_section_sha` from `capture_base` at read time), never recomputed live — `test_accept_does_not_clobber_human_edit_race` guards it (a human edit between read and propose makes accept go `stale`, not overwrite).
2. **Cross-workspace auth (HIGH):** accept/reject fetch by **(id, workspace_id)** via `_load_scoped`; the endpoints pass the path `workspace_id` — `test_accept_rejects_mismatched_workspace` guards it.
3. **Turn-freeze scope:** Task 2 narrowed to the `save_section` hard guard; the two remaining soft pre-checks (`web/sections.py:186`, `analysis/insert.py:114`) are explicitly deferred to the porting plan with their own tests.
4. **Schema future-proofing:** `kind` CHECK widened to include `citation_remove`/`citation_update`; `origin_job_id` added — later plans need no migration.
5. **Accept error mapping:** the accept route maps `save_section`'s user-facing errors (`SectionStructureError`→422, `CommitMutexTimeout`→503, …) per `sections.py:255`.
6. **Test fix:** unique approver email (`approver@ki.se`) so it never collides with the workspace owner's default.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-17-cockpit-sp2a-propose-accept-substrate.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?** (And note: this is SP2a plan 1 of ~5 — the editor round-trip **spike** should kick off in parallel since it gates the editor plans.)
