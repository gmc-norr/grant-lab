# Cockpit SP2b-2b — Agent↔Spec propose/accept + inline review UI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the drafting agent propose edits to the Spec artifact and let members review/accept/reject them from inside the cockpit — mirroring the SP2a draft propose/accept flow, with acceptance replaying through `save_section` so the SP2b-2a spec lock gates it.

**Architecture:** Parameterize the existing propose/accept/staleness services + agent tools over the artifact; light up the spec branch in `accept_proposed_change` (replay `save_section(artifact=SPEC)`); add dedicated `read_spec`/`propose_spec_edit` tools (registered AND allowlisted in `claude_adapter.py`); relocate the proposed-changes endpoints under `/api` so the SPA gets the error envelope, and expose `new_text`; build one shared, prose-only `<ProposalReview>` rendered in both artifact panels. **No DB migration** — `ProposedChange.target_artifact` already permits `'spec'`, and the lock columns exist from SP2b-2a.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Claude Agent SDK (MCP tools), pytest + testcontainers; React 18 + Vite + TanStack Query + TypeScript + Vitest/jsdom, pnpm.

**Spec:** `docs/superpowers/specs/2026-06-21-cockpit-sp2b-2b-spec-propose-accept-design.md`

**Worktree:** all work happens in `/home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2b` on branch `cockpit-sp2b-2b` (off master `d24ff87`). **The main checkout is on `fix/review-findings`, which LACKS this code** — never edit there. Every git command runs against this worktree; verify `git -C <worktree> branch --show-current` prints `cockpit-sp2b-2b` before any commit.

---

## Conventions every task must follow

- **TDD:** write the failing test, run it and SEE it fail for the expected reason, implement minimally, run it green, commit.
- **Test flakiness (known):** the full `uv run pytest` is testcontainer-flaky under load. Run the **specific touched test file(s)**. A connection-refused failure is infra — re-run that file in isolation.
- **Commit trailer:** every message ends with exactly: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- **Frontend:** no TS linter — the gate is `pnpm exec tsc --noEmit` (NOT `pnpm lint`). Schema regen: `EMBEDDING_MODEL=bge-m3 pnpm gen:api`. pnpm needs Node 22 via nvm (`. ~/.nvm/nvm.sh` if pnpm is missing). Run frontend commands from `frontend/`.
- **Status mapping:** the `/api` error envelope is `{error:{code,message}}`, produced by the global handler for `/api` paths only (`main.py:331`); a ≥500 is sanitized to `internal_error`. Non-`/api` routes return `{detail}`.

---

## File Structure

**Backend — modified:**
- `src/call_manager/proposal_assist/proposals/proposing.py` — `propose_prose_edit` gains `target_artifact` param.
- `src/call_manager/proposal_assist/proposals/staleness.py` — `refresh_staleness` sweeps both artifacts.
- `src/call_manager/proposal_assist/proposals/accepting.py` — spec accept path.
- `src/call_manager/proposal_assist/tools/proposal.py` — shared read/propose core + `read_spec_impl`/`propose_spec_edit_impl`.
- `src/call_manager/proposal_assist/tools/registry.py` — `ToolContext` spec fields + register the two tools.
- `src/call_manager/proposal_assist/drafting/claude_adapter.py` — `allowed_tools` + system-prompt append.
- `src/call_manager/proposal_assist/web/proposals.py` — `/api` prefix, `new_text` serialize, `?target_artifact`, accept mappings.

**Frontend — created/modified:**
- `frontend/src/api/proposals.ts` — hooks (new).
- `frontend/src/cockpit/ProposalReview.tsx` — review component + line-diff (new).
- `frontend/src/cockpit/SpecPanel.tsx`, `DraftPanel.tsx` — mount `<ProposalReview>`.
- `frontend/src/api/schema.gen.ts` — regenerated.

**Tests:** `tests/proposal_assist/proposals/test_{proposing,staleness,accepting}.py`, `tests/proposal_assist/tools/test_propose_spec_edit.py` (new), `tests/proposal_assist/test_tools_registry.py`, `tests/proposal_assist/test_web_proposed_changes.py` (+ `/api` path sweep), and `frontend/src/api/proposals.test.tsx` + `frontend/src/cockpit/ProposalReview.test.tsx` (new).

---

## Task 1: `propose_prose_edit` — `target_artifact` parameter

**Files:**
- Modify: `src/call_manager/proposal_assist/proposals/proposing.py:38-70`
- Test: `tests/proposal_assist/proposals/test_proposing.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/proposals/test_proposing.py` (reuse the file's existing imports of `propose_prose_edit`, `make_user`/`make_workspace`, `pg_session_factory`):

```python
def test_propose_prose_edit_targets_spec_when_requested(pg_session_factory):
    from call_manager.db.models import ProposedChange
    from call_manager.proposal_assist.proposals.proposing import propose_prose_edit
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Aims\nspec text.\n",
            base_commit_sha="0" * 40,
            base_section_sha="0" * 64,
            origin_kind="interactive_turn",
            target_artifact="spec",
        )
        s.flush()
        row = s.get(ProposedChange, pc.id)
        assert row.target_artifact == "spec"
        assert row.kind == "prose_edit"


def test_propose_prose_edit_defaults_to_draft(pg_session_factory):
    from call_manager.db.models import ProposedChange
    from call_manager.proposal_assist.proposals.proposing import propose_prose_edit
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\nx.\n",
            base_commit_sha="0" * 40,
            base_section_sha="0" * 64,
            origin_kind="interactive_turn",
        )
        s.flush()
        assert s.get(ProposedChange, pc.id).target_artifact == "draft"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/proposal_assist/proposals/test_proposing.py -k "targets_spec or defaults_to_draft" -v`
Expected: FAIL — `propose_prose_edit() got an unexpected keyword argument 'target_artifact'`.

- [ ] **Step 3: Add the parameter**

In `src/call_manager/proposal_assist/proposals/proposing.py`, change the `propose_prose_edit` signature to add `target_artifact: str = "draft"` (place it after `new_text`/`base_*`, before `origin_kind`) and use it in the row:

```python
def propose_prose_edit(
    *,
    session: Session,
    workspace_id: uuid.UUID,
    section_index: int,
    new_text: str,
    base_commit_sha: str,
    base_section_sha: str,
    target_artifact: str = "draft",
    origin_kind: str,
    origin_session_id: str | None = None,
    origin_job_id: uuid.UUID | None = None,
    triggering_turn_id: uuid.UUID | None = None,
    triggering_prompt: str | None = None,
    created_by: int | None = None,
) -> ProposedChange:
    pc = ProposedChange(
        workspace_id=workspace_id,
        target_artifact=target_artifact,
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

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/proposal_assist/proposals/test_proposing.py -v`
Expected: PASS (existing draft tests still green — the default preserves them).

- [ ] **Step 5: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2b
git branch --show-current   # cockpit-sp2b-2b
git add src/call_manager/proposal_assist/proposals/proposing.py tests/proposal_assist/proposals/test_proposing.py
git commit -m "feat(proposals): propose_prose_edit gains target_artifact param

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `refresh_staleness` — sweep both artifacts

`refresh_staleness` currently filters `target_artifact == "draft"` and reads only `proposal.md`. Generalize it to compare each pending row against its **own** artifact's live sections.

**Files:**
- Modify: `src/call_manager/proposal_assist/proposals/staleness.py`
- Test: `tests/proposal_assist/proposals/test_staleness.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/proposals/test_staleness.py` (mirror its existing repo-seeding helper; this one seeds a spec file too):

```python
def test_refresh_staleness_flips_stale_spec_row(pg_session_factory, tmp_path):
    import subprocess
    from pathlib import Path

    from call_manager.db.models import ProposedChange
    from call_manager.proposal_assist.proposals.proposing import propose_prose_edit
    from call_manager.proposal_assist.proposals.staleness import refresh_staleness
    from call_manager.proposal_assist.editing.sectionizer import split_sections
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        repo = Path(tmp_path) / str(ws.id) / "repo"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        (repo / "proposal.md").write_text("preamble\n")
        (repo / "proposal-spec.md").write_text("## Aims\nold.\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        # Propose against section 1 of the SPEC with a base sha that no longer matches.
        pc = propose_prose_edit(
            session=s, workspace_id=ws.id, section_index=1,
            new_text="## Aims\nproposed.\n", base_commit_sha="0" * 40,
            base_section_sha="deadbeef" * 8, origin_kind="interactive_turn",
            target_artifact="spec",
        )
        s.flush()
        n = refresh_staleness(session=s, workspace_id=ws.id, workspaces_dir=str(tmp_path))
        s.flush()
        assert n == 1
        assert s.get(ProposedChange, pc.id).status == "stale"
```

(Keep the existing draft staleness tests in the file — they must still pass.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/proposal_assist/proposals/test_staleness.py -k spec -v`
Expected: FAIL — the spec row is ignored (filter is `target_artifact == "draft"`), so `n == 0` and status stays `pending`.

- [ ] **Step 3: Generalize `refresh_staleness`**

Replace the body of `src/call_manager/proposal_assist/proposals/staleness.py` with a per-artifact sweep:

```python
"""Flip pending proposed_change rows to 'stale' when their targeted section's
base_section_sha no longer matches the live artifact. Called by the list endpoint
(proactive UX) — accept-time StaleSectionError is the authoritative guard."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import ProposedChange
from call_manager.proposal_assist.artifacts import Artifact, filename_for
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir


def refresh_staleness(
    *, session: Session, workspace_id: uuid.UUID, workspaces_dir: Path | str
) -> int:
    repo_dir = workspace_repo_dir(workspaces_dir, workspace_id)
    # Live section shas per artifact, read once. A missing artifact file (workspace
    # still in setup, or no spec yet) contributes no live sections, so its pending
    # rows simply aren't flipped here (accept-time staleness remains authoritative).
    live: dict[str, dict[int, str]] = {}
    for artifact in (Artifact.DRAFT, Artifact.SPEC):
        path = repo_dir / filename_for(artifact)
        if path.exists():
            live[str(artifact)] = {i: sec.sha for i, sec in enumerate(split_sections(path.read_text()))}

    # FOR UPDATE SKIP LOCKED: don't fight an in-flight accept/reject (its row lock
    # decides its fate); rows we lock stay pending until this sweep commits.
    rows = session.scalars(
        select(ProposedChange)
        .where(
            ProposedChange.workspace_id == workspace_id,
            ProposedChange.status == "pending",
        )
        .with_for_update(skip_locked=True)
    ).all()
    n = 0
    for pc in rows:
        sections = live.get(pc.target_artifact)
        if sections is None:
            continue  # artifact file absent -> leave pending
        current = sections.get(pc.section_index)
        if current is None or current != pc.base_section_sha:
            pc.status = "stale"
            n += 1
    session.flush()
    return n
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/proposal_assist/proposals/test_staleness.py -v`
Expected: PASS (the new spec test + all existing draft tests).

- [ ] **Step 5: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2b
git branch --show-current   # cockpit-sp2b-2b
git add src/call_manager/proposal_assist/proposals/staleness.py tests/proposal_assist/proposals/test_staleness.py
git commit -m "feat(proposals): refresh_staleness sweeps both artifacts

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `accept_proposed_change` — spec prose path

Light up the `target_artifact != "draft"` guard: a spec `prose_edit` replays `save_section(artifact=SPEC)`; `SpecLockedError` propagates (the endpoint maps it to 409); a non-prose spec kind raises `NotImplementedError`. The draft branches are unchanged.

**Files:**
- Modify: `src/call_manager/proposal_assist/proposals/accepting.py:14-119`
- Test: `tests/proposal_assist/proposals/test_accepting.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/proposals/test_accepting.py` (mirror its existing helpers for building a workspace + repo; this seeds a spec file and a spec proposal). Use `make_user`/`make_workspace` and the file's `_init_repo`-style helper — if the existing helper only writes `proposal.md`, add a sibling that also writes `proposal-spec.md`:

```python
def _spec_repo(tmp_path, ws_id, spec_text):
    import subprocess
    from pathlib import Path

    repo = Path(tmp_path) / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "proposal.md").write_text("preamble\n")
    (repo / "proposal-spec.md").write_text(spec_text)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_accept_spec_prose_commits_to_spec(pg_session_factory, tmp_path):
    from pathlib import Path

    from call_manager.proposal_assist.proposals.accepting import accept_proposed_change
    from call_manager.proposal_assist.proposals.proposing import capture_base, propose_prose_edit
    from call_manager.proposal_assist.artifacts import Artifact
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        s.flush()
        repo = _spec_repo(tmp_path, ws.id, "## Aims\norig.\n")
        base_commit, base_section = capture_base(repo, 1, artifact=Artifact.SPEC)
        pc = propose_prose_edit(
            session=s, workspace_id=ws.id, section_index=1,
            new_text="## Aims\nagent spec.\n", base_commit_sha=base_commit,
            base_section_sha=base_section, origin_kind="interactive_turn",
            target_artifact="spec",
        )
        s.commit()
        result = accept_proposed_change(
            session=s, proposed_change_id=pc.id, workspace_id=ws.id,
            approver=owner, workspaces_dir=str(tmp_path),
        )
        s.commit()
        assert result.status == "accepted"
        assert "agent spec." in (repo / "proposal-spec.md").read_text()


def test_accept_spec_prose_blocked_when_locked(pg_session_factory, tmp_path):
    from datetime import UTC, datetime

    from call_manager.proposal_assist.editing.save import SpecLockedError
    from call_manager.proposal_assist.proposals.accepting import accept_proposed_change
    from call_manager.proposal_assist.proposals.proposing import capture_base, propose_prose_edit
    from call_manager.proposal_assist.artifacts import Artifact
    from tests.proposal_assist.conftest import make_user, make_workspace
    import pytest

    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        ws.spec_locked_at = datetime.now(UTC)
        ws.spec_locked_by = owner.id
        s.flush()
        repo = _spec_repo(tmp_path, ws.id, "## Aims\norig.\n")
        base_commit, base_section = capture_base(repo, 1, artifact=Artifact.SPEC)
        pc = propose_prose_edit(
            session=s, workspace_id=ws.id, section_index=1,
            new_text="## Aims\nagent spec.\n", base_commit_sha=base_commit,
            base_section_sha=base_section, origin_kind="interactive_turn",
            target_artifact="spec",
        )
        s.commit()
        with pytest.raises(SpecLockedError):
            accept_proposed_change(
                session=s, proposed_change_id=pc.id, workspace_id=ws.id,
                approver=owner, workspaces_dir=str(tmp_path),
            )
```

> Note: this task also needs `capture_base` to accept an `artifact` kwarg — add it as the first sub-step below (it currently hardcodes `PROPOSAL_FILENAME`).

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/proposal_assist/proposals/test_accepting.py -k "spec_prose" -v`
Expected: FAIL — `capture_base()` has no `artifact` kwarg, and/or accept raises `NotImplementedError("spec target is SP2b")`.

- [ ] **Step 3: Parameterize `capture_base` over the artifact**

In `src/call_manager/proposal_assist/proposals/proposing.py`, change `capture_base` to take an artifact (default DRAFT, so existing callers are unchanged):

```python
from call_manager.proposal_assist.artifacts import Artifact, filename_for
# (keep the existing imports; the PROPOSAL_FILENAME import can stay or be dropped
#  if now unused — run ruff and remove it only if F401 fires)


def capture_base(repo_dir: Path, section_index: int, *, artifact: Artifact = Artifact.DRAFT) -> tuple[str, str]:
    """Snapshot (head_commit_sha, section_sha) of the artifact at READ time."""
    head = current_head(repo_dir)
    doc = (repo_dir / filename_for(artifact)).read_text()
    return head, split_sections(doc)[section_index].sha
```

- [ ] **Step 4: Light up the spec branch in `accept_proposed_change`**

In `src/call_manager/proposal_assist/proposals/accepting.py`, add the imports and replace the guard (lines 74-76) with a spec branch. Add to the imports at the top:

```python
from call_manager.proposal_assist.artifacts import Artifact
from call_manager.proposal_assist.editing.save import SpecLockedError, StaleSectionError, save_section
```

Then replace lines 74-76 (the `if pc.target_artifact != "draft": raise NotImplementedError(...)`) with:

```python
    if pc.target_artifact == "spec":
        if pc.kind != "prose_edit":
            # The spec is citation-free; only prose proposals target it. A
            # DB-permitted-but-unsupported kind is a clean NotImplementedError
            # (the endpoint maps it to 400), never a silent 500.
            raise NotImplementedError(f"spec accept of kind {pc.kind!r} is not supported")
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
                artifact=Artifact.SPEC,
            )
        except StaleSectionError:
            pc.status = "stale"
            pc.resolved_by = approver.id
            pc.resolved_at = datetime.now(UTC)
            session.flush()
            return pc
        # SpecLockedError is NOT caught here: it propagates to the endpoint, which
        # maps it to 409 and leaves the row pending (retryable after unlock).
        pc.status = "accepted"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        pc.resulting_commit_sha = result.commit_sha
        session.flush()
        return pc
```

(The existing `if pc.kind == "prose_edit":` draft branch and the `citation_insert` branch below it stay exactly as they are — they only run for `target_artifact == "draft"` now.)

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/proposal_assist/proposals/test_accepting.py -v`
Expected: PASS (the two new spec tests + all existing draft accept/reject tests).

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2b
git branch --show-current   # cockpit-sp2b-2b
git add src/call_manager/proposal_assist/proposals/accepting.py src/call_manager/proposal_assist/proposals/proposing.py tests/proposal_assist/proposals/test_accepting.py
git commit -m "feat(proposals): accept spec prose via save_section(SPEC); lock gates it

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Agent tools — `read_spec` / `propose_spec_edit` (impls + ToolContext)

Add the two spec impls backed by a shared core, plus the parallel `agent_view_spec_*` snapshot fields. The draft impls are refactored to call the same core (behavior preserved; the existing `tests/proposal_assist/tools/test_propose_edit.py` is the regression guard).

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/proposal.py`
- Modify: `src/call_manager/proposal_assist/tools/registry.py:42-99` (ToolContext)
- Test: `tests/proposal_assist/tools/test_propose_spec_edit.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/tools/test_propose_spec_edit.py`:

```python
"""read_spec snapshots the spec view; propose_spec_edit writes a pending
spec prose_edit using the read-time base; an unread section is rejected."""

import subprocess
import uuid
from pathlib import Path

import pytest

from call_manager.config import Settings
from call_manager.db.models import ProposedChange
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.tools.proposal import (
    propose_spec_edit_impl,
    read_spec_impl,
)
from call_manager.proposal_assist.tools.registry import ToolContext
from tests.proposal_assist.conftest import make_workspace


def _spec_repo(tmp_path, ws_id, spec_text):
    repo = Path(tmp_path) / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "proposal.md").write_text("preamble\n")
    (repo / "proposal-spec.md").write_text(spec_text)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


@pytest.mark.asyncio
async def test_read_spec_then_propose_spec_edit(pg_session_factory, _settings_env, tmp_path):
    spec = "## Aims\norig.\n"
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    repo = _spec_repo(tmp_path, wsid, spec)
    ctx = ToolContext(
        workspace_id=wsid, repo_dir=repo, session_factory=pg_session_factory, settings=Settings()
    )
    read = await read_spec_impl(ctx)
    assert read["content"] == spec
    assert ctx.agent_view_spec_head is not None
    assert ctx.agent_view_spec_sections[1] == split_sections(spec)[1].sha

    result = await propose_spec_edit_impl(ctx, section_index=1, new_text="## Aims\nagent.\n")
    assert result["status"] == "pending"
    with pg_session_factory() as s:
        pc = s.get(ProposedChange, uuid.UUID(result["proposed_change_id"]))
        assert pc.target_artifact == "spec"
        assert pc.kind == "prose_edit"
        assert pc.base_section_sha == split_sections(spec)[1].sha


@pytest.mark.asyncio
async def test_propose_spec_edit_rejects_unread(pg_session_factory, _settings_env, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    repo = _spec_repo(tmp_path, wsid, "## Aims\norig.\n")
    ctx = ToolContext(
        workspace_id=wsid, repo_dir=repo, session_factory=pg_session_factory, settings=Settings()
    )
    result = await propose_spec_edit_impl(ctx, section_index=1, new_text="## Aims\nx.\n")
    assert "error" in result and "read_spec" in result["error"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/proposal_assist/tools/test_propose_spec_edit.py -v`
Expected: FAIL — `cannot import name 'read_spec_impl'`.

- [ ] **Step 3: Add the ToolContext spec fields**

In `src/call_manager/proposal_assist/tools/registry.py`, in the `ToolContext` dataclass (after `agent_view_proposal`, ~line 99), add:

```python
    # SP2b-2b: the agent's read-time view of the SPEC artifact (parallel to the
    # draft fields above). Populated by read_spec_impl; propose_spec_edit_impl
    # reads its base from here (anti-clobber, read-time not live).
    agent_view_spec_head: str | None = field(default=None)
    agent_view_spec_sections: dict[int, str] = field(default_factory=dict)
    agent_view_spec_proposal: str | None = field(default=None)
```

- [ ] **Step 4: Add the shared core + spec impls (refactor draft to share)**

In `src/call_manager/proposal_assist/tools/proposal.py`, add `SPEC_FILENAME` to the artifacts import, and replace the two existing impls with a shared core + four thin wrappers. The full replacement for the body (keep the module docstring and `_NOT_READ_ERROR`):

```python
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME, SPEC_FILENAME

_NOT_READ_DRAFT = "you haven't read the proposal yet — call read_proposal first"
_NOT_READ_SPEC = "you haven't read the spec yet — call read_spec first"


async def _read_artifact_impl(
    ctx: ToolContext, *, filename: str, head_attr: str, sections_attr: str, proposal_attr: str
) -> dict[str, Any]:
    """Read an artifact and snapshot the agent's view into the given ctx attrs.
    ORDERING INVARIANT: resolve HEAD *before* reading content (fail-safe staleness)."""
    path = ctx.repo_dir / filename
    if not path.exists():
        return {"error": f"{filename} does not exist in this workspace"}
    try:
        setattr(ctx, head_attr, current_head(ctx.repo_dir))
    except GitCommitError:
        setattr(ctx, head_attr, None)
    content = path.read_text()
    setattr(ctx, sections_attr, {s.index: s.sha for s in split_sections(content)})
    setattr(ctx, proposal_attr, content)
    return {"content": content}


async def read_proposal_impl(ctx: ToolContext) -> dict[str, Any]:
    """Return the current proposal.md content (snapshots the agent's draft view)."""
    return await _read_artifact_impl(
        ctx, filename=PROPOSAL_FILENAME,
        head_attr="agent_view_head", sections_attr="agent_view_sections",
        proposal_attr="agent_view_proposal",
    )


async def read_spec_impl(ctx: ToolContext) -> dict[str, Any]:
    """Return the current proposal-spec.md content (snapshots the agent's spec view)."""
    return await _read_artifact_impl(
        ctx, filename=SPEC_FILENAME,
        head_attr="agent_view_spec_head", sections_attr="agent_view_spec_sections",
        proposal_attr="agent_view_spec_proposal",
    )


async def _propose_artifact_edit_impl(
    ctx: ToolContext, *, target_artifact: str, head: str | None,
    sections: dict[int, str], section_index: int, new_text: str, not_read_err: str,
) -> dict[str, Any]:
    from call_manager.proposal_assist.proposals.proposing import propose_prose_edit

    if head is None:
        return {"error": not_read_err}
    base_section_sha = sections.get(section_index)
    if base_section_sha is None:
        return {"error": not_read_err}
    with ctx.session_factory() as session:
        pc = propose_prose_edit(
            session=session, workspace_id=ctx.workspace_id, section_index=section_index,
            new_text=new_text, base_commit_sha=head, base_section_sha=base_section_sha,
            target_artifact=target_artifact, origin_kind="interactive_turn",
            origin_session_id=ctx.origin_session_id, triggering_turn_id=ctx.triggering_turn_id,
            triggering_prompt=ctx.triggering_prompt, created_by=ctx.created_by,
        )
        pc_id = str(pc.id)
        session.commit()
    return {"proposed_change_id": pc_id, "status": "pending"}


async def propose_edit_impl(ctx: ToolContext, *, section_index: int, new_text: str) -> dict[str, Any]:
    return await _propose_artifact_edit_impl(
        ctx, target_artifact="draft", head=ctx.agent_view_head,
        sections=ctx.agent_view_sections, section_index=section_index,
        new_text=new_text, not_read_err=_NOT_READ_DRAFT,
    )


async def propose_spec_edit_impl(ctx: ToolContext, *, section_index: int, new_text: str) -> dict[str, Any]:
    return await _propose_artifact_edit_impl(
        ctx, target_artifact="spec", head=ctx.agent_view_spec_head,
        sections=ctx.agent_view_spec_sections, section_index=section_index,
        new_text=new_text, not_read_err=_NOT_READ_SPEC,
    )
```

> Keep the existing imports at the top of the file (`current_head`, `GitCommitError`, `split_sections`, `ToolContext`, `logging`). The old `_NOT_READ_ERROR` constant is replaced by `_NOT_READ_DRAFT`/`_NOT_READ_SPEC`; if any other module imports `_NOT_READ_ERROR`, grep first (`grep -rn _NOT_READ_ERROR src tests`) and update or keep an alias.

- [ ] **Step 5: Run to verify it passes (and the draft regression)**

Run: `uv run pytest tests/proposal_assist/tools/test_propose_spec_edit.py tests/proposal_assist/tools/test_propose_edit.py tests/proposal_assist/test_tools_proposal_read_sections.py -v`
Expected: PASS — new spec tests green, draft tool tests still green.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2b
git branch --show-current   # cockpit-sp2b-2b
git add src/call_manager/proposal_assist/tools/proposal.py src/call_manager/proposal_assist/tools/registry.py tests/proposal_assist/tools/test_propose_spec_edit.py
git commit -m "feat(tools): read_spec/propose_spec_edit impls + ToolContext spec view

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Register the tools + allowlist + system prompt

Registering in `registry.py` makes the tools *exist*; the runtime allowlist in `claude_adapter.py` makes them *callable*; the system-prompt append tells the agent to *use* them.

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/registry.py:183-226` (imports + two `@tool` blocks)
- Modify: `src/call_manager/proposal_assist/drafting/claude_adapter.py:225-260`
- Test: `tests/proposal_assist/test_tools_registry.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_tools_registry.py` (mirror its existing way of building a `ToolContext` and listing tool names):

```python
def test_spec_tools_registered(pg_session_factory, tmp_path):
    from pathlib import Path

    from call_manager.config import Settings
    from call_manager.proposal_assist.tools.registry import ToolContext, _collect_tools

    ctx = ToolContext(
        workspace_id=__import__("uuid").uuid4(),
        repo_dir=Path(tmp_path),
        session_factory=pg_session_factory,
        settings=Settings(),
    )
    names = {t.name for t in _collect_tools(ctx)}
    assert "read_spec" in names
    assert "propose_spec_edit" in names
```

The allowlist itself is already guarded by an **existing** test:
`tests/proposal_assist/test_tools_claude_adapter_wiring.py::test_every_collected_tool_is_allowlisted`
(line ~157) fails if a collected tool is missing from `allowed_tools`. That file
also hard-asserts `len(allowed) == 19` (line ~193) and lists the 19 tools
explicitly (line ~97). **Both of those existing assertions must be updated in
this task** (→ 21, and add `read_spec` + `propose_spec_edit` to the listed set) —
do it in Step 4 below, in the same commit, or the wiring test goes red.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_tools_registry.py -k spec_tools -v`
Expected: FAIL — `read_spec` not in `_collect_tools` names.

- [ ] **Step 3: Register the tools in `registry.py`**

In `_collect_tools`, extend the `tools.proposal` import (line 183-186) to include the new impls:

```python
    from call_manager.proposal_assist.tools.proposal import (
        propose_edit_impl,
        propose_spec_edit_impl,
        read_proposal_impl,
        read_spec_impl,
    )
```

Then add two `@tool` blocks next to `read_proposal`/`propose_edit` (mirror their shape exactly):

```python
    @tool(
        "read_spec",
        "Return the current proposal-spec.md (the Spec artifact) content. Call this "
        "before propose_spec_edit so the system knows which version you based your "
        "edit on.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def read_spec(args):
        return await _safe_tool("read_spec", ctx, read_spec_impl, ctx)

    @tool(
        "propose_spec_edit",
        "Propose a full-text replacement of one section of the SPEC (proposal-spec.md) "
        "as a PENDING change for a human to review and accept — this does NOT edit or "
        "commit anything. `section_index` is the 0-based section (0 is the preamble); "
        "call read_spec first. `new_text` is the WHOLE new section including its "
        "`## heading` line. Returns a proposed_change_id and status 'pending'.",
        {
            "type": "object",
            "properties": {
                "section_index": {"type": "integer"},
                "new_text": {"type": "string"},
            },
            "required": ["section_index", "new_text"],
        },
    )
    async def propose_spec_edit(args):
        try:
            section_index = int(args["section_index"])
        except (TypeError, ValueError, KeyError):
            return _mcp_error("section_index must be an integer")
        return await _safe_tool(
            "propose_spec_edit", ctx, propose_spec_edit_impl, ctx,
            section_index=section_index, new_text=args["new_text"],
        )
```

**Add both to the returned list.** `_collect_tools` ends with an explicit
`return [ read_proposal, propose_edit, … ]` (registry.py ~line 616). Add
`read_spec` and `propose_spec_edit` to that list (next to `read_proposal` /
`propose_edit`) — a `@tool` block that isn't in the returned list is never
registered.

- [ ] **Step 4: Add to the runtime allowlist + system prompt**

In `src/call_manager/proposal_assist/drafting/claude_adapter.py`, add the two names to the `allowed_tools` list (after `"propose_edit"`, ~line 240):

```python
                "read_proposal",
                # SP2a-2 — propose-only mutation (no direct repo writes)
                "propose_edit",
                # SP2b-2b — spec artifact propose-only
                "read_spec",
                "propose_spec_edit",
                "propose_citation",
```

And teach the agent to use them. Near the other instruction constants, add:

```python
_SPEC_TOOLS_INSTRUCTION = (
    "There are two artifacts: the draft proposal (read_proposal / propose_edit) and "
    "the Spec (read_spec / propose_spec_edit), a short citation-free statement of aims, "
    "approach, novelty, impact, and key claims. When the user's request is about the "
    "Spec, use the spec tools; otherwise use the draft tools. Both only PROPOSE changes "
    "for a human to accept."
)
```

Then append it in the `system_prompt` preset (line 225-229):

```python
        options_kwargs["system_prompt"] = {
            "type": "preset",
            "preset": "claude_code",
            "append": _REFERENCE_SAFETY_INSTRUCTION
            + "\n\n"
            + _CITATION_INSTRUCTION
            + "\n\n"
            + _SPEC_TOOLS_INSTRUCTION,
        }
```

Then update the two existing hard assertions in
`tests/proposal_assist/test_tools_claude_adapter_wiring.py`: bump
`assert len(allowed) == 19` → `== 21`, and add `"read_spec"` and
`"propose_spec_edit"` to the explicitly-listed allowlist set it checks (~line 97).

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/proposal_assist/test_tools_registry.py tests/proposal_assist/test_tools_claude_adapter_wiring.py -v`
Expected: PASS — `test_spec_tools_registered`, `test_every_collected_tool_is_allowlisted` (the real guard: every collected tool is allowlisted), and the updated `len == 21` assertion all green.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2b
git branch --show-current   # cockpit-sp2b-2b
git add src/call_manager/proposal_assist/tools/registry.py src/call_manager/proposal_assist/drafting/claude_adapter.py tests/proposal_assist/test_tools_registry.py tests/proposal_assist/test_tools_claude_adapter_wiring.py
git commit -m "feat(agent): register + allowlist read_spec/propose_spec_edit + prompt

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Endpoints — relocate under `/api`, serialize `new_text`, `?target_artifact`, accept mappings

Relocating the router under `/api` changes every existing path `/workspaces/{id}/proposed-changes` → `/api/workspaces/{id}/proposed-changes`. **This breaks all current tests in `test_web_proposed_changes.py` (14 call sites) — they MUST be swept in this same commit** (no green intermediate, like the SP2b-2a `current_phase` sweep). Success responses are plain dicts (unchanged); only error *shape* changes (now the `/api` envelope), but the existing tests assert status codes, not error bodies, so the sweep is purely path-string.

**Files:**
- Modify: `src/call_manager/proposal_assist/web/proposals.py`
- Test: `tests/proposal_assist/test_web_proposed_changes.py` (path sweep + new tests)

- [ ] **Step 1: Write the failing tests (new behavior)**

Append to `tests/proposal_assist/test_web_proposed_changes.py` a spec helper + new tests (these already use the post-relocation `/api/...` paths):

```python
def _seed_spec_pending(s, ws, tmp_path, locked=False):
    from datetime import UTC, datetime
    from pathlib import Path

    from call_manager.proposal_assist.artifacts import Artifact
    from call_manager.proposal_assist.proposals.proposing import capture_base, propose_prose_edit

    repo = Path(tmp_path) / str(ws.id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "proposal.md").write_text("preamble\n")
    (repo / "proposal-spec.md").write_text("## Aims\norig.\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    if locked:
        ws.spec_locked_at = datetime.now(UTC)
        ws.spec_locked_by = ws.owner_id
        s.flush()
    base_commit, base_section = capture_base(repo, 1, artifact=Artifact.SPEC)
    return propose_prose_edit(
        session=s, workspace_id=ws.id, section_index=1, new_text="## Aims\nagent.\n",
        base_commit_sha=base_commit, base_section_sha=base_section,
        origin_kind="interactive_turn", target_artifact="spec",
    )


def test_accept_spec_endpoint_commits(client_authed_owner, _app, current_user, pg_session_factory, tmp_path):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        pc = _seed_spec_pending(s, ws, tmp_path)
        s.commit()
        pc_id, ws_id = str(pc.id), str(ws.id)
    resp = client_authed_owner.post(f"/api/workspaces/{ws_id}/proposed-changes/{pc_id}/accept")
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    assert "agent." in (Path(tmp_path) / ws_id / "repo" / "proposal-spec.md").read_text()


def test_accept_spec_locked_returns_409(client_authed_owner, _app, current_user, pg_session_factory, tmp_path):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        pc = _seed_spec_pending(s, ws, tmp_path, locked=True)
        s.commit()
        pc_id, ws_id = str(pc.id), str(ws.id)
    resp = client_authed_owner.post(f"/api/workspaces/{ws_id}/proposed-changes/{pc_id}/accept")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"
    with pg_session_factory() as s:
        from call_manager.db.models import ProposedChange
        assert s.get(ProposedChange, uuid.UUID(pc_id)).status == "pending"


def test_list_filters_by_target_artifact(client_authed_owner, _app, current_user, pg_session_factory, tmp_path):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        _seed_pending(s, ws, tmp_path)        # draft (writes proposal.md repo)
        _seed_spec_pending(s, ws, tmp_path)    # spec (adds proposal-spec.md)
        s.commit()
        ws_id = str(ws.id)
    spec_only = client_authed_owner.get(
        f"/api/workspaces/{ws_id}/proposed-changes?target_artifact=spec"
    ).json()["items"]
    assert [i["target_artifact"] for i in spec_only] == ["spec"]
    assert spec_only[0]["new_text"] == "## Aims\nagent.\n"
    all_items = client_authed_owner.get(f"/api/workspaces/{ws_id}/proposed-changes").json()["items"]
    assert {i["target_artifact"] for i in all_items} == {"draft", "spec"}
```

Add `import uuid` at the top of the test file if not already present.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_web_proposed_changes.py -k "spec or target_artifact" -v`
Expected: FAIL — 404 (the `/api/...` routes don't exist yet).

- [ ] **Step 3: Relocate the router + serialize `new_text` + filter + mappings**

In `src/call_manager/proposal_assist/web/proposals.py`:

(a) Change the router prefix and add the `SpecLockedError` import:

```python
from call_manager.proposal_assist.editing.save import SectionStructureError, SpecLockedError

router = APIRouter(prefix="/api/workspaces", tags=["proposed-changes"])
```

(b) Add `new_text` to `_serialize`:

```python
def _serialize(pc: ProposedChange) -> dict:
    return {
        "id": str(pc.id),
        "status": pc.status,
        "target_artifact": pc.target_artifact,
        "section_index": pc.section_index,
        "kind": pc.kind,
        "origin_kind": pc.origin_kind,
        "new_text": pc.payload.get("new_text") if pc.kind == "prose_edit" else None,
        "resulting_commit_sha": pc.resulting_commit_sha,
        "created_at": pc.created_at.isoformat() if pc.created_at else None,
        "resolved_by": pc.resolved_by,
        "resolved_at": pc.resolved_at.isoformat() if pc.resolved_at else None,
    }
```

(c) Add the `?target_artifact` filter to `list_proposed_changes` (the function signature gains `target_artifact: str | None = None` as a query param, and the query gains a conditional `.where`):

```python
@router.get("/{workspace_id}/proposed-changes")
def list_proposed_changes(
    workspace_id: uuid.UUID,
    request: Request,
    target_artifact: str | None = None,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    require_member(workspace_id=workspace_id, user=user, session=session)
    workspaces_dir = request.app.state.settings.workspaces_dir
    refresh_staleness(session=session, workspace_id=workspace_id, workspaces_dir=workspaces_dir)
    stmt = (
        select(ProposedChange)
        .where(
            ProposedChange.workspace_id == workspace_id,
            ProposedChange.status == "pending",
        )
        .order_by(ProposedChange.created_at)
    )
    if target_artifact is not None:
        stmt = stmt.where(ProposedChange.target_artifact == target_artifact)
    rows = session.scalars(stmt).all()
    session.commit()
    return {"items": [_serialize(pc) for pc in rows]}
```

(d) In the `accept` handler, add a `SpecLockedError → 409` clause and a `NotImplementedError → 400` clause (before the generic handlers), and change `CommitMutexTimeout` from 503 to **429 + Retry-After**:

```python
    except SpecLockedError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="spec is locked — unlock to accept") from exc
    except NotImplementedError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail="unsupported proposal kind") from exc
    except CommitMutexTimeout as exc:
        session.rollback()
        raise HTTPException(
            status_code=429, detail="workspace busy; retry", headers={"Retry-After": "2"}
        ) from exc
```

(Place the `SpecLockedError`/`NotImplementedError` clauses among the existing `except` blocks; replace the existing `CommitMutexTimeout` 503 block with the 429 version above.)

- [ ] **Step 4: Sweep the existing test paths to `/api/...`**

In `tests/proposal_assist/test_web_proposed_changes.py`, replace every occurrence of `f"/workspaces/{` with `f"/api/workspaces/{` (the existing 14 list/accept/reject call sites). Use Edit `replace_all` on the substring `/workspaces/{ws` and `/workspaces/{ws_b` — verify by grep afterward that no bare `"/workspaces/` proposed-changes path remains:

Run: `grep -n '"/workspaces/\|f"/workspaces/' tests/proposal_assist/test_web_proposed_changes.py` → expect only `/api/...` forms (no bare `/workspaces/...proposed-changes`).

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/proposal_assist/test_web_proposed_changes.py -v`
Expected: PASS — the swept existing tests + the new spec/filter tests.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2b
git branch --show-current   # cockpit-sp2b-2b
git add src/call_manager/proposal_assist/web/proposals.py tests/proposal_assist/test_web_proposed_changes.py
git commit -m "feat(api): proposed-changes under /api; new_text + ?target_artifact; spec 409/400/429

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Frontend hooks + schema regen

**Files:**
- Modify: `frontend/src/api/schema.gen.ts` (regenerated)
- Create: `frontend/src/api/proposals.ts`
- Test: `frontend/src/api/proposals.test.tsx`

- [ ] **Step 1: Regenerate the schema**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2b/frontend
EMBEDDING_MODEL=bge-m3 pnpm gen:api
```

Expected: `src/api/schema.gen.ts` now has `/api/workspaces/{workspace_id}/proposed-changes` (+ `…/{pc_id}/accept` `…/reject`) with the `target_artifact` query param. (Responses are untyped dicts — the hook below declares a local row type.)

- [ ] **Step 2: Write the failing hook tests**

Create `frontend/src/api/proposals.test.tsx` (mirror `editor.test.tsx`'s `wrapper(qc)` + `vi.spyOn(clientMod.api, …)` pattern):

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { expect, it, vi } from "vitest";

import * as clientMod from "./client";
import { useAcceptProposal, useProposedChanges, useRejectProposal } from "./proposals";

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

it("useProposedChanges GETs with the target_artifact query", async () => {
  const get = vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: { items: [] }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useProposedChanges("ws-1", "spec"), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith("/api/workspaces/{workspace_id}/proposed-changes", {
    params: { path: { workspace_id: "ws-1" }, query: { target_artifact: "spec" } },
  });
});

it("useAcceptProposal POSTs and invalidates spec + proposals", async () => {
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { status: "accepted" },
    error: undefined,
  } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useAcceptProposal("ws-1", "spec"), { wrapper: wrapper(qc) });
  await result.current.mutateAsync("pc-1");
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["proposed-changes", "ws-1", "spec"] });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["spec", "ws-1"] });
});

it("useRejectProposal POSTs and invalidates the list", async () => {
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { status: "rejected" },
    error: undefined,
  } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useRejectProposal("ws-1", "spec"), { wrapper: wrapper(qc) });
  await result.current.mutateAsync("pc-1");
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["proposed-changes", "ws-1", "spec"] });
});
```

- [ ] **Step 3: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2b/frontend
pnpm test -- src/api/proposals.test.tsx
```
Expected: FAIL — module `./proposals` not found.

- [ ] **Step 4: Write the hooks**

Create `frontend/src/api/proposals.ts`:

```ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";

export type Artifact = "draft" | "spec";

export type ProposalRow = {
  id: string;
  status: "pending" | "accepted" | "rejected" | "superseded" | "stale";
  target_artifact: Artifact;
  section_index: number;
  kind: string;
  origin_kind: string;
  new_text: string | null;
  resulting_commit_sha: string | null;
  created_at: string | null;
  resolved_by: number | null;
  resolved_at: string | null;
};

/** GET /api/workspaces/{id}/proposed-changes?target_artifact=… — pending rows. */
export function useProposedChanges(id: string, artifact: Artifact) {
  return useQuery({
    queryKey: ["proposed-changes", id, artifact],
    queryFn: async () => {
      const data = unwrap(
        await api.GET("/api/workspaces/{workspace_id}/proposed-changes", {
          params: { path: { workspace_id: id }, query: { target_artifact: artifact } },
        }),
      ) as { items: ProposalRow[] };
      return data.items;
    },
  });
}

/** POST accept. Invalidates the proposals list AND the artifact content query. */
export function useAcceptProposal(id: string, artifact: Artifact) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (pcId: string) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/proposed-changes/{pc_id}/accept", {
          params: { path: { workspace_id: id, pc_id: pcId } },
        }),
      ) as ProposalRow,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["proposed-changes", id, artifact] });
      qc.invalidateQueries({ queryKey: [artifact, id] });
    },
  });
}

/** POST reject. Invalidates the proposals list. */
export function useRejectProposal(id: string, artifact: Artifact) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (pcId: string) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/proposed-changes/{pc_id}/reject", {
          params: { path: { workspace_id: id, pc_id: pcId } },
        }),
      ) as ProposalRow,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["proposed-changes", id, artifact] }),
  });
}
```

- [ ] **Step 5: Run the tests + type gate**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2b/frontend
pnpm test -- src/api/proposals.test.tsx
pnpm exec tsc --noEmit
```
Expected: tests PASS; `tsc` clean. (If `tsc` flags the `query.target_artifact` param shape, confirm the regenerated schema lists it; the `as` casts cover the untyped dict responses.)

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2b
git branch --show-current   # cockpit-sp2b-2b
git add frontend/src/api/proposals.ts frontend/src/api/proposals.test.tsx frontend/src/api/schema.gen.ts
git commit -m "feat(spa): proposed-changes hooks (list/accept/reject) + regen schema

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: `<ProposalReview>` component + mount in both panels

A shared, **prose-only** review strip with a minimal line diff, rendered above the editor in `SpecPanel` and `DraftPanel`.

**Files:**
- Create: `frontend/src/cockpit/ProposalReview.tsx`
- Modify: `frontend/src/cockpit/SpecPanel.tsx`, `frontend/src/cockpit/DraftPanel.tsx`
- Modify: `frontend/src/styles/cockpit.css`
- Test: `frontend/src/cockpit/ProposalReview.test.tsx`

- [ ] **Step 1: Write the failing component test**

Create `frontend/src/cockpit/ProposalReview.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { ProposalReview } from "./ProposalReview";

afterEach(() => vi.restoreAllMocks());

function renderReview(props: { locked?: boolean } = {}) {
  const qc = new QueryClient();
  render(
    <QueryClientProvider client={qc}>
      <ProposalReview
        workspaceId="ws-1"
        artifact="spec"
        currentSections={[{ index: 1, text: "## Aims\norig.\n" }]}
        locked={props.locked ?? false}
      />
    </QueryClientProvider>,
  );
}

const ROW = {
  id: "pc-1",
  status: "pending",
  target_artifact: "spec",
  section_index: 1,
  kind: "prose_edit",
  origin_kind: "interactive_turn",
  new_text: "## Aims\nbetter.\n",
  resulting_commit_sha: null,
  created_at: "2026-06-21T10:00:00Z",
  resolved_by: null,
  resolved_at: null,
};

it("renders nothing when there are no pending proposals", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: { items: [] }, error: undefined } as never);
  renderReview();
  await waitFor(() =>
    expect(screen.queryByText(/pending proposal/i)).not.toBeInTheDocument(),
  );
});

it("shows a prose proposal with accept/reject and hides citation rows", async () => {
  const citation = { ...ROW, id: "pc-2", kind: "citation_insert", new_text: null };
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { items: [ROW, citation] },
    error: undefined,
  } as never);
  renderReview();
  await screen.findByText(/pending proposals \(1\)/i); // count reflects prose only
  expect(screen.getByRole("button", { name: /accept/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /reject/i })).toBeInTheDocument();
});

it("disables Accept with a hint when the spec is locked", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: { items: [ROW] }, error: undefined } as never);
  renderReview({ locked: true });
  const accept = await screen.findByRole("button", { name: /accept/i });
  expect(accept).toBeDisabled();
});
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2b/frontend
pnpm test -- src/cockpit/ProposalReview.test.tsx
```
Expected: FAIL — module `./ProposalReview` not found.

- [ ] **Step 3: Implement the component (with a dependency-free line diff)**

Create `frontend/src/cockpit/ProposalReview.tsx`:

```tsx
import { useState } from "react";

import { ApiError } from "../api/client";
import {
  type Artifact,
  type ProposalRow,
  useAcceptProposal,
  useProposedChanges,
  useRejectProposal,
} from "../api/proposals";

type DiffRow = { tag: "same" | "add" | "del"; text: string };

// Minimal LCS line diff (dependency-free). Good enough for short prose sections;
// richer word-diff is deferred.
function lineDiff(current: string, proposed: string): DiffRow[] {
  const a = current.split("\n");
  const b = proposed.split("\n");
  const n = a.length;
  const m = b.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const out: DiffRow[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ tag: "same", text: a[i] });
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      out.push({ tag: "del", text: a[i++] });
    } else {
      out.push({ tag: "add", text: b[j++] });
    }
  }
  while (i < n) out.push({ tag: "del", text: a[i++] });
  while (j < m) out.push({ tag: "add", text: b[j++] });
  return out;
}

export function ProposalReview({
  workspaceId,
  artifact,
  currentSections,
  locked = false,
}: {
  workspaceId: string;
  artifact: Artifact;
  currentSections: { index: number; text: string }[];
  locked?: boolean;
}) {
  const list = useProposedChanges(workspaceId, artifact);
  const accept = useAcceptProposal(workspaceId, artifact);
  const reject = useRejectProposal(workspaceId, artifact);
  const [notice, setNotice] = useState<string | null>(null);

  const prose = (list.data ?? []).filter((p: ProposalRow) => p.kind === "prose_edit");
  if (prose.length === 0) return null;

  const currentFor = (idx: number) => currentSections.find((s) => s.index === idx)?.text ?? "";

  async function onAccept(p: ProposalRow) {
    setNotice(null);
    try {
      const result = await accept.mutateAsync(p.id);
      if (result.status === "stale") {
        setNotice("That proposal went stale (the section moved) — reloading.");
      }
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message.replace(/^[a-z_]+: /, "") : "Accept failed.");
    }
  }

  return (
    <section className="proposal-review" aria-label="Pending proposals">
      <h2 className="proposal-review-title">Pending proposals ({prose.length})</h2>
      {notice && (
        <p role="status" className="proposal-review-notice">
          {notice}
        </p>
      )}
      <ul>
        {prose.map((p) => (
          <li key={p.id} className="proposal-item">
            <p className="proposal-meta">
              proposed by the assistant{p.created_at ? ` · ${new Date(p.created_at).toLocaleString()}` : ""}
            </p>
            <pre className="proposal-diff">
              {lineDiff(currentFor(p.section_index), p.new_text ?? "").map((r, k) => (
                <div key={k} className={`diff-${r.tag}`}>
                  {(r.tag === "add" ? "+ " : r.tag === "del" ? "- " : "  ") + r.text}
                </div>
              ))}
            </pre>
            <div className="proposal-actions">
              <button
                type="button"
                disabled={locked || accept.isPending}
                title={locked ? "Unlock the spec to accept" : undefined}
                onClick={() => onAccept(p)}
              >
                Accept
              </button>
              <button type="button" disabled={reject.isPending} onClick={() => reject.mutate(p.id)}>
                Reject
              </button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
```

- [ ] **Step 4: Mount in both panels**

In `frontend/src/cockpit/SpecPanel.tsx`, inside the non-empty `spec-shell` return (from SP2b-2a), add the review strip below the lock control and above the editor, passing the lock state and the current sections:

```tsx
      {/* SP2b-2b: pending spec proposals */}
      <ProposalReview
        workspaceId={id}
        artifact="spec"
        currentSections={sections.map((s) => ({ index: s.index, text: s.text }))}
        locked={locked}
      />
```

Add the import `import { ProposalReview } from "./ProposalReview";` at the top.

In `frontend/src/cockpit/DraftPanel.tsx`, wrap the editor so the review strip can sit above it:

```tsx
import { useDraft, useSaveSection } from "../api/editor";
import { ArtifactSectionEditor } from "../editor/ArtifactSectionEditor";
import { ProposalReview } from "./ProposalReview";

export function DraftPanel({ id }: { id: string }) {
  const draft = useDraft(id);
  const save = useSaveSection(id);
  if (draft.isLoading) return <main aria-busy="true">Loading cockpit…</main>;
  if (draft.isError || !draft.data) {
    return (
      <main role="alert">
        <h1>Could not load this workspace</h1>
      </main>
    );
  }
  const { head, can_edit, sections } = draft.data;
  return (
    <div className="draft-shell">
      <ProposalReview
        workspaceId={id}
        artifact="draft"
        currentSections={sections.map((s) => ({ index: s.index, text: s.text }))}
      />
      <ArtifactSectionEditor
        workspaceId={id}
        queryKey={["draft", id]}
        head={head}
        canEdit={can_edit}
        sections={sections}
        save={save}
        refetch={draft.refetch}
        citations
      />
    </div>
  );
}
```

- [ ] **Step 5: Add CSS**

Append to `frontend/src/styles/cockpit.css`:

```css
/* SP2b-2b: inline pending-proposal review strip. */
.proposal-review {
  border: 1px solid var(--cm-border);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
  background: var(--cm-surface-sunken);
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.proposal-review-title { margin: 0; font-size: 0.95rem; }
.proposal-item { list-style: none; border-top: 1px solid var(--cm-border); padding-top: 0.5rem; }
.proposal-meta { margin: 0 0 0.25rem; color: var(--cm-ink-muted, #667); font-size: 0.8rem; }
.proposal-diff { margin: 0; white-space: pre-wrap; font-size: 0.85rem; }
.proposal-diff .diff-add { background: rgba(46, 160, 67, 0.15); }
.proposal-diff .diff-del { background: rgba(248, 81, 73, 0.15); }
.proposal-actions { display: flex; gap: 0.5rem; margin-top: 0.4rem; }
.draft-shell { display: flex; flex-direction: column; gap: 0.5rem; }
```

- [ ] **Step 6: Run tests + type gate + the panel regression**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2b/frontend
pnpm test -- src/cockpit/ProposalReview.test.tsx src/cockpit/SpecPanel.test.tsx src/routes/Cockpit.test.tsx
pnpm exec tsc --noEmit
```
Expected: PASS; `tsc` clean. (`SpecPanel.test.tsx`/`Cockpit.test.tsx` mock `api.GET`; ensure the added `useProposedChanges` GET is tolerated — if a panel test asserted an exact single GET call, relax it to allow the proposals fetch, or have the mock return `{items: []}` for the proposed-changes path.)

- [ ] **Step 7: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2b
git branch --show-current   # cockpit-sp2b-2b
git add frontend/src/cockpit/ProposalReview.tsx frontend/src/cockpit/ProposalReview.test.tsx frontend/src/cockpit/SpecPanel.tsx frontend/src/cockpit/DraftPanel.tsx frontend/src/styles/cockpit.css
git commit -m "feat(spa): inline prose-only ProposalReview in Spec + Draft panels

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (after Task 8)

- [ ] Backend (touched files in isolation, dodging the flaky whole-suite run):
  `uv run pytest tests/proposal_assist/proposals/test_proposing.py tests/proposal_assist/proposals/test_staleness.py tests/proposal_assist/proposals/test_accepting.py tests/proposal_assist/tools/test_propose_spec_edit.py tests/proposal_assist/tools/test_propose_edit.py tests/proposal_assist/test_tools_registry.py tests/proposal_assist/test_web_proposed_changes.py -q`
- [ ] Frontend: `cd frontend && pnpm test && pnpm exec tsc --noEmit`
- [ ] Schema drift: `git -C /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2b diff --exit-code -- frontend/src/api/schema.gen.ts` (clean — regen committed in Task 7).
- [ ] Dispatch one final whole-feature adversarial review; address findings.
- [ ] STOP before merge. Hand back to the human (finishing-a-development-branch).

---

## Self-review notes (plan author)

- **Spec coverage:** §1 tools+services → Tasks 1/3/4/5; §2 endpoints → Task 6 (relocate `/api`, `new_text`, `?target_artifact`, `SpecLockedError→409`/`NotImplementedError→400`/busy→429); §3 frontend → Tasks 7/8 (prose-only, stale-on-accept notice, locked-disable); §4 tests → every task.
- **No DB migration** — `ProposedChange.target_artifact` already permits `spec`; lock columns exist from SP2b-2a.
- **Two no-green-intermediate touches, both contained in their task:** Task 6 relocates the router AND sweeps the 14 existing endpoint-test paths in one commit (a bare `/workspaces/...proposed-changes` after the prefix change would 404). Task 4 refactors the draft impls into a shared core in one commit, guarded by the existing draft tool tests.
- **Confirm-before-relying notes for implementers:** (a) the exact helper/fixture names in `tests/proposal_assist/proposals/test_{proposing,staleness,accepting}.py` (reuse them; adapt if names differ — do not invent); (b) whether `test_drafting_claude_adapter.py` asserts an exact allowlist (update its expectation if so); (c) whether any `SpecPanel.test.tsx`/`Cockpit.test.tsx` mock asserts a single `api.GET` (relax to tolerate the proposals fetch); (d) `_settings_env` is the existing async-tool fixture used by `test_propose_edit.py` — reuse it.
- **Type consistency:** `propose_prose_edit(target_artifact=…)` (T1) is consumed by the shared core (T4) and the agent tools; `ProposalRow.new_text` (T7) matches `_serialize`'s `new_text` (T6); `useAcceptProposal`/`useRejectProposal`/`useProposedChanges` names match `ProposalReview` (T8) imports; query keys `["proposed-changes", id, artifact]` and `[artifact, id]` are consistent across T7/T8.
