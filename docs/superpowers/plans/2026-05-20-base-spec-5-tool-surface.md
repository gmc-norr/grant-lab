# Base Spec B5 — Tool Surface for the Drafting Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the custom tool set the drafting agent invokes during turns. Eight tools across three groups: artifact editing (`read_proposal` / `edit_proposal` / `replace_proposal`), version history (`get_artifact_history` / `read_artifact_at` / `diff_artifact`), and context retrieval (`get_call_details` / `get_profile`). Each artifact-editing tool atomically commits to the workspace's git repo and writes an `artifact_versions` row. Tool registration is per-workspace via in-process MCP server. Reconciliation backfills `artifact_versions` rows for orphan git commits on workspace open.

**Architecture:** New package `src/call_monitor/proposal_assist/tools/` holds:
- `registry.py` — `ToolContext` dataclass + `build_proposal_assist_mcp_server(ctx)` builder that returns an `McpSdkServerConfig` ready for `ClaudeAgentOptions.mcp_servers`.
- `proposal.py`, `history.py`, `context.py` — tool implementations (closures over `ToolContext`).
- `reconcile.py` — `reconcile_workspace_git_drift` (read git log, backfill missing `artifact_versions` rows, write `workspace_events('git_drift_reconciled')`).

`ClaudeAgentSDKAdapter` gains a `tools_factory: Callable[[ToolContext], McpSdkServerConfig]` parameter (constructed by `build_workspace_controller`). At launch time, the adapter builds the per-workspace MCP server and passes it via `ClaudeAgentOptions(mcp_servers={...}, allowed_tools=[...])`. `WorkspaceController.launch()` runs reconciliation inside the same `work_session` block where the orphan sweep + resume_session_id fetch happen.

**Tech Stack:** Python 3.12, `claude-agent-sdk` (`@tool` decorator + `create_sdk_mcp_server`), `subprocess` for git operations (same pattern as Phase 0 worker), SQLAlchemy 2.x for `artifact_versions` writes, pytest + testcontainers, `tempfile` + `pygit2` not required — plain git CLI via subprocess matches the existing pattern.

**Spec:** [docs/superpowers/specs/2026-04-18-call-manager-proposal-assist-design.md](../specs/2026-04-18-call-manager-proposal-assist-design.md), §Tools exposed to the agent (lines 204–230), §Artifact versioning (lines 234–268).

**Overview:** [docs/superpowers/plans/2026-05-19-base-spec-overview.md](2026-05-19-base-spec-overview.md), §B5.

---

## Prerequisites and scope honesty

**Already on master (B1 + Slice A + B3 + B4):**
- `workspaces`, `chat_turns`, `turn_jobs`, `chat_summaries`, `artifact_versions`, `workspace_events` tables + models.
- Phase 0 creates `var/workspaces/<id>/repo/` as a git repo with an initial `proposal.md` commit.
- `WorkspaceController.launch()` runs orphan sweep + reads `last_sdk_session_id` inside a work_session.
- `ClaudeAgentSDKAdapter` constructs `ClaudeAgentOptions` and passes `resume`, `cwd`, `model`, `permission_mode`.
- `submit_user_turn` + drain tick + processor; tool events flow through to `chat_turns` rows already (the processor handles `ToolUseEvent` + `ToolResultEvent` from B4).

**What B5 does NOT do:**
- Branching tools (`create_branch`, `switch_branch`, `list_branches`) — base spec line 217–218, phased spec line 33 explicitly drops these.
- Past-grant tools (`list_past_grants`, `read_past_grant`) — base spec line 224, phased spec line 35 drops these.
- `search_pubmed` — base spec line 224, phased spec line 32 drops it.
- Figure / attachment tools (`describe_figure`, `look_at_figure`, `read_preliminary_data`) — **B7** owns these. B5's registry pattern is the surface B7 plugs into.
- POST `/workspaces/<id>/turns` endpoint, SSE bridge, chat UI — **B6**.
- Reconciliation UI (`/admin` "verify and reconcile workspace" button) — **B6 / B8**. B5 implements the reconciliation FUNCTION; the on-workspace-open trigger fires automatically via `controller.launch()`. The admin-driven invocation is wiring B6/B8 layer on top.

---

## Conventions reference

- Testcontainers + `pg_session_factory` as before.
- All new code under `src/call_monitor/proposal_assist/tools/` + one new module `proposal_assist/reconcile.py`.
- Don't sweep up pre-existing ruff debt outside touched files. The B4 review fixes confirmed this is master's policy.
- Tests under `tests/proposal_assist/test_tools_*.py` and `test_reconcile_*.py`.
- The `_settings_env` fixture supplies baseline env. `make_workspace`, `make_user`, etc. helpers are in `tests/proposal_assist/conftest.py`.
- Git operations: shell out via `subprocess.run(["git", ...], cwd=repo_dir, check=True)`. Match the existing Phase 0 pattern (`src/call_monitor/proposal_assist/phases/setup.py`).

---

## File map

**Created:**
- `src/call_monitor/proposal_assist/tools/__init__.py` — package surface re-exports.
- `src/call_monitor/proposal_assist/tools/registry.py` — `ToolContext` + `build_proposal_assist_mcp_server`.
- `src/call_monitor/proposal_assist/tools/proposal.py` — `read_proposal`, `edit_proposal`, `replace_proposal` + the shared "commit + write artifact_versions row" helper.
- `src/call_monitor/proposal_assist/tools/history.py` — `get_artifact_history`, `read_artifact_at`, `diff_artifact`.
- `src/call_monitor/proposal_assist/tools/context.py` — `get_call_details`, `get_profile`.
- `src/call_monitor/proposal_assist/reconcile.py` — `reconcile_workspace_git_drift`.
- Tests: `tests/proposal_assist/test_tools_proposal.py`, `test_tools_history.py`, `test_tools_context.py`, `test_tools_registry.py`, `test_reconcile.py`, `test_tools_claude_adapter_wiring.py`, `test_tools_end_to_end.py`.

**Modified:**
- `src/call_monitor/proposal_assist/drafting/claude_adapter.py` — constructor takes `tools_factory`; `launch()` constructs MCP server and passes via options; `allowed_tools` lists our tool names.
- `src/call_monitor/proposal_assist/drafting/factory.py` — `build_workspace_controller` constructs a `tools_factory` closure capturing `session_factory` + `settings` and passes it to the adapter.
- `src/call_monitor/proposal_assist/drafting/controller.py` — `launch()` invokes reconciliation inside the existing `work_session` block.
- `src/call_monitor/proposal_assist/drafting/__init__.py` — re-export `ToolContext` if it becomes part of the public surface (likely yes, B7 needs it).

---

## Revision history

- **rev1 (2026-05-20)** — initial plan.

---

## Task 1: tool registry skeleton

The contract. `ToolContext` carries everything a tool handler needs (workspace_id, repo_dir, session_factory, settings). `build_proposal_assist_mcp_server(ctx)` returns an `McpSdkServerConfig` ready to pass to `ClaudeAgentOptions(mcp_servers=...)`. Initially the server has zero tools — subsequent tasks register them.

**Files:**
- Create: `src/call_monitor/proposal_assist/tools/__init__.py`
- Create: `src/call_monitor/proposal_assist/tools/registry.py`
- Test: `tests/proposal_assist/test_tools_registry.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_tools_registry.py`:

```python
import uuid
from pathlib import Path

from claude_agent_sdk.types import McpSdkServerConfig

from call_monitor.proposal_assist.tools.registry import (
    ToolContext,
    build_proposal_assist_mcp_server,
)


def test_tool_context_is_a_dataclass():
    """Smoke: ToolContext has the four expected fields."""
    ctx = ToolContext(
        workspace_id=uuid.uuid4(),
        repo_dir=Path("/tmp/x"),
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )
    assert isinstance(ctx.workspace_id, uuid.UUID)
    assert isinstance(ctx.repo_dir, Path)


def test_build_mcp_server_returns_mcp_sdk_server_config():
    """The factory returns a config the SDK can consume."""
    ctx = ToolContext(
        workspace_id=uuid.uuid4(),
        repo_dir=Path("/tmp/x"),
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )
    server = build_proposal_assist_mcp_server(ctx)
    assert isinstance(server, McpSdkServerConfig)


def test_build_mcp_server_has_expected_name():
    """Server name is stable so allowed_tools entries can reference it."""
    ctx = ToolContext(
        workspace_id=uuid.uuid4(),
        repo_dir=Path("/tmp/x"),
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )
    server = build_proposal_assist_mcp_server(ctx)
    # McpSdkServerConfig's exact attribute name depends on the SDK; the
    # important invariant is that the server registers as "proposal_assist".
    # If the SDK stores the name under a different attribute, adjust this
    # assertion (likely server.name or server["name"]).
    name = getattr(server, "name", None) or server.get("name") if isinstance(server, dict) else getattr(server, "name", None)
    assert name == "proposal_assist"
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_tools_registry.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`src/call_monitor/proposal_assist/tools/__init__.py`:

```python
"""Per-workspace tool surface for the drafting agent.

The registry pattern (registry.py) is the contract: a ToolContext binds
workspace-specific resources (repo_dir, session_factory, settings) to a
fresh MCP server per workspace launch.

B7 will extend this surface with figure/attachment tools by adding more
@tool functions to build_proposal_assist_mcp_server.
"""

from call_monitor.proposal_assist.tools.registry import (
    ToolContext,
    build_proposal_assist_mcp_server,
)

__all__ = ["ToolContext", "build_proposal_assist_mcp_server"]
```

`src/call_monitor/proposal_assist/tools/registry.py`:

```python
"""ToolContext + MCP server builder for the per-workspace tool surface.

Tools are async functions decorated with claude_agent_sdk.tool(...) and
registered into an SDK MCP server via create_sdk_mcp_server. The server
runs in-process; the SDK calls handler functions over the MCP protocol
during a turn.

Each WorkspaceController.launch builds a fresh ToolContext and a fresh
MCP server bound to that workspace's repo_dir + session_factory. The
handlers are closures over ctx — they don't read from globals.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from claude_agent_sdk import create_sdk_mcp_server

if TYPE_CHECKING:
    from claude_agent_sdk.types import McpSdkServerConfig
    from sqlalchemy.orm import sessionmaker

    from call_monitor.config import Settings


@dataclass
class ToolContext:
    """Per-workspace binding passed to every tool handler."""

    workspace_id: uuid.UUID
    repo_dir: Path
    session_factory: "sessionmaker"
    settings: "Settings"


def build_proposal_assist_mcp_server(ctx: ToolContext) -> "McpSdkServerConfig":
    """Build the per-workspace MCP server. Each tool handler is a closure
    over ctx. The list grows as subsequent tasks (T2, T3, T4) add tools."""
    tools = _collect_tools(ctx)
    return create_sdk_mcp_server(name="proposal_assist", tools=tools)


def _collect_tools(ctx: ToolContext) -> list:
    """Gather every tool's @tool-decorated handler. Subsequent tasks
    append to this list."""
    return []
```

- [ ] **Step 4: Run + lint + commit**

```bash
uv run pytest tests/proposal_assist/test_tools_registry.py -v
# expect 3 passing
uv run ruff format src/call_monitor/proposal_assist/tools/ tests/proposal_assist/test_tools_registry.py
uv run ruff check src/call_monitor/proposal_assist/tools/ tests/proposal_assist/test_tools_registry.py
git add src/call_monitor/proposal_assist/tools/__init__.py src/call_monitor/proposal_assist/tools/registry.py tests/proposal_assist/test_tools_registry.py
git commit -m "feat(b5): tool registry skeleton — ToolContext + MCP server builder"
```

---

## Task 2: artifact tools (read / edit / replace)

The three highest-value tools. `read_proposal()` returns the current `proposal.md` content. `edit_proposal(edits)` applies a list of `{old_string, new_string}` surgical replacements + commits + writes an `artifact_versions` row. `replace_proposal(content)` overwrites + commits + writes a row.

**Write order (spec line 248–253):** git commit FIRST (gets sha), then DB tx (insert `artifact_versions` row with sha, mark triggering turn). If DB fails: drift case → next workspace open's reconciliation backfills the orphan commit. Reconciliation is Task 5.

**Files:**
- Create: `src/call_monitor/proposal_assist/tools/proposal.py`
- Test: `tests/proposal_assist/test_tools_proposal.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_tools_proposal.py`:

```python
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from call_monitor.db.models import ArtifactVersion
from call_monitor.proposal_assist.tools.proposal import (
    edit_proposal_impl,
    read_proposal_impl,
    replace_proposal_impl,
)
from call_monitor.proposal_assist.tools.registry import ToolContext
from tests.proposal_assist.conftest import make_workspace


def _init_repo(repo_dir: Path, initial_content: str = "# Proposal\n\nInitial.\n"):
    """Helper: create a git repo with an initial proposal.md commit."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo_dir, check=True)
    (repo_dir / "proposal.md").write_text(initial_content)
    subprocess.run(["git", "add", "proposal.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo_dir, check=True)


def _build_ctx(pg_session_factory, tmp_path) -> tuple[ToolContext, uuid.UUID]:
    """Build a ToolContext backed by a real testcontainer DB + tmp repo."""
    from call_monitor.config import Settings
    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    return ToolContext(
        workspace_id=wsid,
        repo_dir=repo_dir,
        session_factory=pg_session_factory,
        settings=settings,
    ), wsid


@pytest.mark.asyncio
async def test_read_proposal_returns_current_content(
    pg_session_factory, _settings_env, tmp_path,
):
    ctx, _ = _build_ctx(pg_session_factory, tmp_path)
    result = await read_proposal_impl(ctx)
    assert result["content"] == "# Proposal\n\nInitial.\n"


@pytest.mark.asyncio
async def test_edit_proposal_applies_string_replace_and_commits(
    pg_session_factory, _settings_env, tmp_path,
):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path)
    result = await edit_proposal_impl(
        ctx,
        edits=[{"old_string": "Initial.", "new_string": "Revised content."}],
        triggering_turn_id=None,
    )
    # File contents updated.
    assert (ctx.repo_dir / "proposal.md").read_text() == "# Proposal\n\nRevised content.\n"
    # Commit landed.
    sha = result["commit_sha"]
    assert sha and len(sha) >= 7
    # artifact_versions row written.
    with pg_session_factory() as s:
        rows = list(s.scalars(
            select(ArtifactVersion).where(ArtifactVersion.workspace_id == wsid)
        ))
    assert len(rows) == 1
    assert rows[0].commit_sha == sha
    assert rows[0].created_by == "agent"


@pytest.mark.asyncio
async def test_edit_proposal_rejects_when_old_string_not_found(
    pg_session_factory, _settings_env, tmp_path,
):
    """If any edit's old_string doesn't match, the whole batch is rejected;
    no file write, no commit, no row."""
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path)
    result = await edit_proposal_impl(
        ctx,
        edits=[{"old_string": "NOPE", "new_string": "X"}],
        triggering_turn_id=None,
    )
    assert result.get("error")
    assert (ctx.repo_dir / "proposal.md").read_text() == "# Proposal\n\nInitial.\n"
    with pg_session_factory() as s:
        rows = list(s.scalars(
            select(ArtifactVersion).where(ArtifactVersion.workspace_id == wsid)
        ))
    assert rows == []


@pytest.mark.asyncio
async def test_edit_proposal_rejects_when_old_string_appears_multiple_times(
    pg_session_factory, _settings_env, tmp_path,
):
    """If an old_string is ambiguous (matches more than once), reject —
    the agent must pick a unique anchor."""
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, )
    (ctx.repo_dir / "proposal.md").write_text("foo bar foo")
    import subprocess
    subprocess.run(["git", "add", "proposal.md"], cwd=ctx.repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "setup"], cwd=ctx.repo_dir, check=True)

    result = await edit_proposal_impl(
        ctx,
        edits=[{"old_string": "foo", "new_string": "baz"}],
        triggering_turn_id=None,
    )
    assert "ambiguous" in (result.get("error") or "").lower()


@pytest.mark.asyncio
async def test_replace_proposal_overwrites_and_commits(
    pg_session_factory, _settings_env, tmp_path,
):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path)
    new_content = "# Brand new\n\nFresh start.\n"
    result = await replace_proposal_impl(
        ctx, content=new_content, triggering_turn_id=None,
    )
    assert (ctx.repo_dir / "proposal.md").read_text() == new_content
    sha = result["commit_sha"]
    assert sha
    with pg_session_factory() as s:
        rows = list(s.scalars(
            select(ArtifactVersion).where(ArtifactVersion.workspace_id == wsid)
        ))
    assert len(rows) == 1
    assert rows[0].commit_sha == sha


@pytest.mark.asyncio
async def test_edit_proposal_no_op_when_edits_empty(
    pg_session_factory, _settings_env, tmp_path,
):
    """Empty edits list → no commit, no row, helpful error."""
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path)
    result = await edit_proposal_impl(
        ctx, edits=[], triggering_turn_id=None,
    )
    assert result.get("error")
    with pg_session_factory() as s:
        rows = list(s.scalars(
            select(ArtifactVersion).where(ArtifactVersion.workspace_id == wsid)
        ))
    assert rows == []


@pytest.mark.asyncio
async def test_edit_proposal_links_triggering_turn(
    pg_session_factory, _settings_env, tmp_path,
):
    """When triggering_turn_id is provided, the artifact_versions row's
    triggering_turn_id is populated."""
    from call_monitor.db.models import ChatTurn
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        turn = ChatTurn(
            workspace_id=wsid, role="user",
            content={"type": "text", "text": "make edits"},
        )
        s.add(turn)
        s.commit()
        turn_id = turn.id
    await edit_proposal_impl(
        ctx,
        edits=[{"old_string": "Initial.", "new_string": "Revised."}],
        triggering_turn_id=turn_id,
    )
    with pg_session_factory() as s:
        row = s.scalar(
            select(ArtifactVersion).where(ArtifactVersion.workspace_id == wsid)
        )
    assert row.triggering_turn_id == turn_id
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_tools_proposal.py -v
```

Expected: `ModuleNotFoundError` on `call_monitor.proposal_assist.tools.proposal`.

- [ ] **Step 3: Implement**

`src/call_monitor/proposal_assist/tools/proposal.py`:

```python
"""Artifact-editing tools: read_proposal, edit_proposal, replace_proposal.

Each editing tool atomically:
  1. Validates inputs (no-op edits, non-unique anchors, missing anchors → reject).
  2. Writes the new content to repo_dir/proposal.md.
  3. Commits via `git commit` (subprocess).
  4. Inserts an artifact_versions row in the DB with the new sha.

Write order is git-first, DB-second per spec line 248. If the DB write
fails after the commit, the next workspace open's reconciliation pass
backfills the orphan commit (see reconcile.py).

The handlers are async (the SDK calls them via the MCP protocol). They
return a JSON-able dict (the SDK serializes it back to the model).
Errors are returned as {"error": "..."} dicts rather than raising,
which gives the model a chance to react.
"""
from __future__ import annotations

import logging
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from call_monitor.db.models import ArtifactVersion
from call_monitor.proposal_assist.tools.registry import ToolContext

logger = logging.getLogger(__name__)

PROPOSAL_FILENAME = "proposal.md"


async def read_proposal_impl(ctx: ToolContext) -> dict[str, Any]:
    """Return the current proposal.md content."""
    path = ctx.repo_dir / PROPOSAL_FILENAME
    if not path.exists():
        return {"error": f"{PROPOSAL_FILENAME} does not exist in this workspace"}
    return {"content": path.read_text()}


async def edit_proposal_impl(
    ctx: ToolContext,
    *,
    edits: list[dict[str, str]],
    triggering_turn_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Apply surgical {old_string → new_string} edits. All-or-nothing:
    if any anchor is missing or ambiguous, the batch is rejected and
    nothing is written or committed.

    Returns {"commit_sha": "..."} on success; {"error": "..."} on
    validation failure.
    """
    if not edits:
        return {"error": "edits list cannot be empty"}

    path = ctx.repo_dir / PROPOSAL_FILENAME
    if not path.exists():
        return {"error": f"{PROPOSAL_FILENAME} does not exist in this workspace"}

    content = path.read_text()
    for idx, edit in enumerate(edits):
        old = edit.get("old_string", "")
        new = edit.get("new_string", "")
        if not old:
            return {"error": f"edit #{idx}: old_string is required"}
        count = content.count(old)
        if count == 0:
            return {"error": f"edit #{idx}: old_string not found in proposal"}
        if count > 1:
            return {"error": (
                f"edit #{idx}: old_string is ambiguous "
                f"(matches {count} times); pick a unique anchor"
            )}
        content = content.replace(old, new, 1)

    return await _write_and_commit(
        ctx,
        content=content,
        commit_message=_commit_message(triggering_turn_id, edits=edits),
        triggering_turn_id=triggering_turn_id,
    )


async def replace_proposal_impl(
    ctx: ToolContext,
    *,
    content: str,
    triggering_turn_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Full overwrite of proposal.md. For large structural rewrites
    where edit_proposal would be unwieldy."""
    return await _write_and_commit(
        ctx,
        content=content,
        commit_message=_commit_message(triggering_turn_id, edits=None),
        triggering_turn_id=triggering_turn_id,
    )


def _commit_message(triggering_turn_id: uuid.UUID | None, *, edits: list | None) -> str:
    kind = "edit_proposal" if edits else "replace_proposal"
    if triggering_turn_id is not None:
        return f"agent: {kind} [turn={triggering_turn_id}]"
    return f"agent: {kind}"


async def _write_and_commit(
    ctx: ToolContext,
    *,
    content: str,
    commit_message: str,
    triggering_turn_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Write file → git commit → artifact_versions insert. Returns sha
    on success."""
    path = ctx.repo_dir / PROPOSAL_FILENAME
    path.write_text(content)

    try:
        subprocess.run(
            ["git", "add", PROPOSAL_FILENAME],
            cwd=ctx.repo_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", commit_message],
            cwd=ctx.repo_dir,
            check=True,
            capture_output=True,
        )
        sha_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ctx.repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        sha = sha_proc.stdout.strip()
    except subprocess.CalledProcessError as e:
        # Git committed nothing (e.g., "nothing to commit" when content
        # is identical). Surface as error; the model can retry with a
        # real diff.
        return {"error": f"git commit failed: {e.stderr.decode('utf-8', errors='replace') if isinstance(e.stderr, bytes) else e.stderr}"}

    # DB write: artifact_versions row.
    try:
        with ctx.session_factory() as s:
            s.add(ArtifactVersion(
                workspace_id=ctx.workspace_id,
                commit_sha=sha,
                triggering_turn_id=triggering_turn_id,
                created_by="agent",
                message=commit_message,
            ))
            s.commit()
    except Exception:
        # Spec-mandated drift case: git has commit, DB doesn't.
        # Reconciliation backfills on next workspace open.
        logger.exception(
            "edit/replace_proposal: artifact_versions insert failed for "
            "workspace %s sha %s — reconciliation will backfill",
            ctx.workspace_id, sha,
        )
        # Still return the sha so the model knows the commit landed.

    return {"commit_sha": sha}
```

- [ ] **Step 4: Run + ruff + commit**

```bash
uv run pytest tests/proposal_assist/test_tools_proposal.py -v
# expect 7 passing
uv run ruff format src/call_monitor/proposal_assist/tools/proposal.py tests/proposal_assist/test_tools_proposal.py
uv run ruff check src/call_monitor/proposal_assist/tools/proposal.py tests/proposal_assist/test_tools_proposal.py
git add src/call_monitor/proposal_assist/tools/proposal.py tests/proposal_assist/test_tools_proposal.py
git commit -m "feat(b5): artifact tools — read/edit/replace_proposal with auto-commit"
```

---

## Task 3: history tools (get_artifact_history, read_artifact_at, diff_artifact)

Three read-only tools for the agent to inspect the version history of `proposal.md`. All shell out to `git log` / `git show` / `git diff`.

**Files:**
- Create: `src/call_monitor/proposal_assist/tools/history.py`
- Test: `tests/proposal_assist/test_tools_history.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_tools_history.py`:

```python
import subprocess
from pathlib import Path

import pytest

from call_monitor.proposal_assist.tools.history import (
    diff_artifact_impl,
    get_artifact_history_impl,
    read_artifact_at_impl,
)
from call_monitor.proposal_assist.tools.registry import ToolContext


def _init_repo_with_history(repo_dir: Path) -> list[str]:
    """Create a repo with three commits; return shas in chronological order."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo_dir, check=True)

    shas: list[str] = []
    for i, line in enumerate(["initial", "second", "third"]):
        (repo_dir / "proposal.md").write_text(f"# Proposal v{i}\n{line}\n")
        subprocess.run(["git", "add", "proposal.md"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"commit {i}"], cwd=repo_dir, check=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        shas.append(sha)
    return shas


def _build_ctx(repo_dir, pg_session_factory):
    import uuid as _uuid
    from call_monitor.config import Settings
    return ToolContext(
        workspace_id=_uuid.uuid4(),
        repo_dir=repo_dir,
        session_factory=pg_session_factory,
        settings=Settings.model_construct(),
    )


@pytest.mark.asyncio
async def test_get_artifact_history_returns_recent_commits(
    pg_session_factory, _settings_env, tmp_path,
):
    repo_dir = tmp_path / "repo"
    shas = _init_repo_with_history(repo_dir)
    ctx = _build_ctx(repo_dir, pg_session_factory)
    result = await get_artifact_history_impl(ctx, limit=10)
    commits = result["commits"]
    assert len(commits) == 3
    # Returned newest-first.
    assert commits[0]["sha"] == shas[-1]
    assert commits[-1]["sha"] == shas[0]
    assert commits[0]["message"] == "commit 2"


@pytest.mark.asyncio
async def test_get_artifact_history_respects_limit(
    pg_session_factory, _settings_env, tmp_path,
):
    repo_dir = tmp_path / "repo"
    _init_repo_with_history(repo_dir)
    ctx = _build_ctx(repo_dir, pg_session_factory)
    result = await get_artifact_history_impl(ctx, limit=2)
    assert len(result["commits"]) == 2


@pytest.mark.asyncio
async def test_read_artifact_at_returns_old_content(
    pg_session_factory, _settings_env, tmp_path,
):
    repo_dir = tmp_path / "repo"
    shas = _init_repo_with_history(repo_dir)
    ctx = _build_ctx(repo_dir, pg_session_factory)
    result = await read_artifact_at_impl(ctx, commit_sha=shas[0])
    assert "v0" in result["content"]


@pytest.mark.asyncio
async def test_read_artifact_at_rejects_invalid_sha(
    pg_session_factory, _settings_env, tmp_path,
):
    repo_dir = tmp_path / "repo"
    _init_repo_with_history(repo_dir)
    ctx = _build_ctx(repo_dir, pg_session_factory)
    result = await read_artifact_at_impl(ctx, commit_sha="deadbeef")
    assert result.get("error")


@pytest.mark.asyncio
async def test_diff_artifact_returns_unified_diff(
    pg_session_factory, _settings_env, tmp_path,
):
    repo_dir = tmp_path / "repo"
    shas = _init_repo_with_history(repo_dir)
    ctx = _build_ctx(repo_dir, pg_session_factory)
    result = await diff_artifact_impl(ctx, commit_a=shas[0], commit_b=shas[-1])
    diff = result["diff"]
    assert "v0" in diff and "v2" in diff
    assert "+" in diff and "-" in diff


@pytest.mark.asyncio
async def test_diff_artifact_rejects_invalid_sha(
    pg_session_factory, _settings_env, tmp_path,
):
    repo_dir = tmp_path / "repo"
    shas = _init_repo_with_history(repo_dir)
    ctx = _build_ctx(repo_dir, pg_session_factory)
    result = await diff_artifact_impl(ctx, commit_a="bad", commit_b=shas[-1])
    assert result.get("error")
```

- [ ] **Step 2: Implement**

`src/call_monitor/proposal_assist/tools/history.py`:

```python
"""Git-history tools: get_artifact_history, read_artifact_at, diff_artifact.

Read-only — never mutate the repo. All shell out to git; errors are
returned as {"error": "..."} dicts so the model sees them rather than
the SDK crashing.
"""
from __future__ import annotations

import subprocess
from typing import Any

from call_monitor.proposal_assist.tools.registry import ToolContext

PROPOSAL_FILENAME = "proposal.md"


async def get_artifact_history_impl(
    ctx: ToolContext, *, limit: int = 20,
) -> dict[str, Any]:
    """Recent commits on the current branch, newest-first, with sha,
    message, and timestamp."""
    limit = max(1, min(limit, 100))  # clamp
    try:
        proc = subprocess.run(
            ["git", "log", f"-n{limit}", "--pretty=format:%H%x00%ct%x00%s"],
            cwd=ctx.repo_dir, check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        return {"error": f"git log failed: {e.stderr}"}
    commits = []
    for line in proc.stdout.strip().split("\n"):
        if not line:
            continue
        sha, ts, message = line.split("\x00", 2)
        commits.append({"sha": sha, "timestamp": int(ts), "message": message})
    return {"commits": commits}


async def read_artifact_at_impl(
    ctx: ToolContext, *, commit_sha: str,
) -> dict[str, Any]:
    """Return proposal.md content at a specific commit."""
    try:
        proc = subprocess.run(
            ["git", "show", f"{commit_sha}:{PROPOSAL_FILENAME}"],
            cwd=ctx.repo_dir, check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        return {"error": f"git show failed for {commit_sha}: {e.stderr.strip()}"}
    return {"content": proc.stdout}


async def diff_artifact_impl(
    ctx: ToolContext, *, commit_a: str, commit_b: str,
) -> dict[str, Any]:
    """Unified diff between two commits on proposal.md."""
    try:
        proc = subprocess.run(
            ["git", "diff", commit_a, commit_b, "--", PROPOSAL_FILENAME],
            cwd=ctx.repo_dir, check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        return {"error": f"git diff failed: {e.stderr.strip()}"}
    return {"diff": proc.stdout}
```

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_tools_history.py -v
# expect 6 passing
uv run ruff format src/call_monitor/proposal_assist/tools/history.py tests/proposal_assist/test_tools_history.py
uv run ruff check src/call_monitor/proposal_assist/tools/history.py tests/proposal_assist/test_tools_history.py
git add src/call_monitor/proposal_assist/tools/history.py tests/proposal_assist/test_tools_history.py
git commit -m "feat(b5): history tools — get_artifact_history, read_artifact_at, diff_artifact"
```

---

## Task 4: context tools (get_call_details, get_profile)

Two read-only context lookups. `get_call_details` returns the linked call's full body + metadata; empty if `workspaces.call_id` is null. `get_profile` returns the linked profile's description + structured fields; empty if `workspaces.profile_id` is null.

**Files:**
- Create: `src/call_monitor/proposal_assist/tools/context.py`
- Test: `tests/proposal_assist/test_tools_context.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_tools_context.py`:

```python
from pathlib import Path

import pytest

from call_monitor.proposal_assist.tools.context import (
    get_call_details_impl,
    get_profile_impl,
)
from call_monitor.proposal_assist.tools.registry import ToolContext
from tests.proposal_assist.conftest import make_workspace


def _build_ctx(pg_session_factory, wsid):
    from call_monitor.config import Settings
    return ToolContext(
        workspace_id=wsid,
        repo_dir=Path("/tmp/unused"),
        session_factory=pg_session_factory,
        settings=Settings.model_construct(),
    )


@pytest.mark.asyncio
async def test_get_call_details_returns_empty_when_no_linked_call(
    pg_session_factory, _settings_env,
):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    ctx = _build_ctx(pg_session_factory, wsid)
    result = await get_call_details_impl(ctx)
    assert result == {"linked": False}


@pytest.mark.asyncio
async def test_get_call_details_returns_call_body_when_linked(
    pg_session_factory, _settings_env,
):
    """Workspace linked to a call returns title, body, programme,
    deadline, link."""
    from call_monitor.db.models import Call, Source
    with pg_session_factory() as s:
        src = Source(slug="test-src", name="Test Source", base_url="http://x")
        s.add(src)
        s.flush()
        call = Call(
            source_id=src.id,
            external_id="call-1",
            title="Test Call",
            description="Full call description here.",
            url="https://example.com/call-1",
            programme="HORIZON",
            fetched_at=None,
        )
        s.add(call)
        s.flush()
        ws = make_workspace(s)
        ws.call_id = call.id
        s.commit()
        wsid = ws.id
    ctx = _build_ctx(pg_session_factory, wsid)
    result = await get_call_details_impl(ctx)
    assert result["linked"] is True
    assert result["title"] == "Test Call"
    assert "Full call description" in result["description"]
    assert result["url"] == "https://example.com/call-1"


@pytest.mark.asyncio
async def test_get_profile_returns_empty_when_no_linked_profile(
    pg_session_factory, _settings_env,
):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    ctx = _build_ctx(pg_session_factory, wsid)
    result = await get_profile_impl(ctx)
    assert result == {"linked": False}


@pytest.mark.asyncio
async def test_get_profile_returns_profile_when_linked(
    pg_session_factory, _settings_env,
):
    from call_monitor.db.models import Profile
    from tests.proposal_assist.conftest import make_user
    with pg_session_factory() as s:
        user = make_user(s)
        s.flush()
        prof = Profile(
            name="Test Profile",
            description="Research on X",
            kind="group",
            created_by=user.id,
        )
        s.add(prof)
        s.flush()
        ws = make_workspace(s, owner=user)
        ws.profile_id = prof.id
        s.commit()
        wsid = ws.id
    ctx = _build_ctx(pg_session_factory, wsid)
    result = await get_profile_impl(ctx)
    assert result["linked"] is True
    assert result["name"] == "Test Profile"
    assert "Research on X" in result["description"]
```

- [ ] **Step 2: Implement**

`src/call_monitor/proposal_assist/tools/context.py`:

```python
"""Context-retrieval tools: get_call_details, get_profile.

Both are read-only DB lookups against the existing calls / profiles
tables. Returns `{"linked": False}` when the workspace has no linked
call / profile (the workspace's call_id / profile_id is NULL).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from call_monitor.db.models import Call, Profile, Workspace
from call_monitor.proposal_assist.tools.registry import ToolContext


async def get_call_details_impl(ctx: ToolContext) -> dict[str, Any]:
    """Return the full call body + metadata for the workspace's linked
    call. Returns {"linked": False} when no call is linked."""
    with ctx.session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.id == ctx.workspace_id))
        if ws is None or ws.call_id is None:
            return {"linked": False}
        call = s.scalar(select(Call).where(Call.id == ws.call_id))
        if call is None:
            return {"linked": False}
        return {
            "linked": True,
            "title": call.title,
            "description": call.description,
            "url": call.url,
            "programme": call.programme,
        }


async def get_profile_impl(ctx: ToolContext) -> dict[str, Any]:
    """Return the linked profile's description + structured fields.
    Returns {"linked": False} when no profile is linked."""
    with ctx.session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.id == ctx.workspace_id))
        if ws is None or ws.profile_id is None:
            return {"linked": False}
        prof = s.scalar(select(Profile).where(Profile.id == ws.profile_id))
        if prof is None:
            return {"linked": False}
        return {
            "linked": True,
            "name": prof.name,
            "description": prof.description,
            "kind": prof.kind,
        }
```

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_tools_context.py -v
# expect 4 passing
uv run ruff format src/call_monitor/proposal_assist/tools/context.py tests/proposal_assist/test_tools_context.py
uv run ruff check src/call_monitor/proposal_assist/tools/context.py tests/proposal_assist/test_tools_context.py
git add src/call_monitor/proposal_assist/tools/context.py tests/proposal_assist/test_tools_context.py
git commit -m "feat(b5): context tools — get_call_details + get_profile"
```

**Read the existing models** before writing the test (especially Call and Profile constructor signatures — Call requires a source_id, Profile requires created_by). The test snippets above match B1's models but verify against the current state.

---

## Task 5: reconciliation

`reconcile_workspace_git_drift(session, workspace_id, repo_dir)` walks `git log` and inserts `artifact_versions` rows for any commits that don't yet have one. Writes a `workspace_events(kind='git_drift_reconciled')` row when backfill actually happened. Idempotent — safe to run on every workspace open.

**Files:**
- Create: `src/call_monitor/proposal_assist/reconcile.py`
- Test: `tests/proposal_assist/test_reconcile.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_reconcile.py`:

```python
import subprocess
import uuid
from pathlib import Path

from sqlalchemy import select

from call_monitor.db.models import ArtifactVersion, WorkspaceEvent
from call_monitor.proposal_assist.reconcile import reconcile_workspace_git_drift
from tests.proposal_assist.conftest import make_workspace


def _init_repo_with_n_commits(repo_dir: Path, n: int) -> list[str]:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo_dir, check=True)
    shas = []
    for i in range(n):
        (repo_dir / "proposal.md").write_text(f"v{i}")
        subprocess.run(["git", "add", "proposal.md"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"agent: edit v{i}"], cwd=repo_dir, check=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        shas.append(sha)
    return shas


def test_reconcile_noop_when_all_commits_have_rows(pg_session_factory, tmp_path):
    """No drift → no backfill rows, no audit event."""
    repo_dir = tmp_path / "repo"
    shas = _init_repo_with_n_commits(repo_dir, 2)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        for sha in shas:
            s.add(ArtifactVersion(
                workspace_id=ws.id, commit_sha=sha,
                triggering_turn_id=None, created_by="agent",
                message="present",
            ))
        s.commit()
        wsid = ws.id

    backfilled = reconcile_workspace_git_drift(pg_session_factory, wsid, repo_dir)
    assert backfilled == 0
    with pg_session_factory() as s:
        events = list(s.scalars(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == wsid,
                WorkspaceEvent.kind == "git_drift_reconciled",
            )
        ))
    assert events == []


def test_reconcile_backfills_orphan_commits(pg_session_factory, tmp_path):
    """Git has 3 commits, DB has only the first 2 → backfill 1."""
    repo_dir = tmp_path / "repo"
    shas = _init_repo_with_n_commits(repo_dir, 3)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        for sha in shas[:2]:
            s.add(ArtifactVersion(
                workspace_id=ws.id, commit_sha=sha,
                triggering_turn_id=None, created_by="agent",
                message="present",
            ))
        s.commit()
        wsid = ws.id

    backfilled = reconcile_workspace_git_drift(pg_session_factory, wsid, repo_dir)
    assert backfilled == 1

    with pg_session_factory() as s:
        rows = list(s.scalars(
            select(ArtifactVersion).where(ArtifactVersion.workspace_id == wsid)
        ))
        events = list(s.scalars(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == wsid,
                WorkspaceEvent.kind == "git_drift_reconciled",
            )
        ))
    assert len(rows) == 3
    backfilled_row = next(r for r in rows if r.commit_sha == shas[-1])
    assert backfilled_row.created_by == "reconciled"
    assert backfilled_row.triggering_turn_id is None
    assert "edit v2" in backfilled_row.message
    assert len(events) == 1
    assert events[0].payload["backfilled_count"] == 1


def test_reconcile_handles_empty_repo(pg_session_factory, tmp_path):
    """A freshly-initialized repo with no commits is a noop, not an error."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    backfilled = reconcile_workspace_git_drift(pg_session_factory, wsid, repo_dir)
    assert backfilled == 0
```

- [ ] **Step 2: Implement**

`src/call_monitor/proposal_assist/reconcile.py`:

```python
"""Workspace git ↔ DB reconciliation.

Spec line 248–268. The filesystem (git) and Postgres are not in the
same transaction, so commits can land without their matching
artifact_versions row. Reconciliation backfills.

Triggers:
  - on workspace open (controller.launch invokes this).
  - on-demand from /admin (B6/B8 will wire that).

Forward-only and never destructive: git commits are never undone.
Orphan-commit case → backfill an artifact_versions row.
Orphan-row case (sha in DB, no git commit) → impossible under the
write order (git first); if it appears, mark created_by='unrecoverable'
and alert. Out of scope here.
"""
from __future__ import annotations

import logging
import subprocess
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from call_monitor.db.models import ArtifactVersion, WorkspaceEvent

logger = logging.getLogger(__name__)


def reconcile_workspace_git_drift(
    session_factory: sessionmaker,
    workspace_id: uuid.UUID,
    repo_dir: Path,
) -> int:
    """Backfill artifact_versions rows for git commits that don't have
    one. Returns the count of rows inserted."""
    if not (repo_dir / ".git").is_dir():
        # No git repo yet — nothing to reconcile.
        return 0
    try:
        proc = subprocess.run(
            ["git", "log", "--pretty=format:%H%x00%s"],
            cwd=repo_dir, check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        logger.warning(
            "reconcile_workspace_git_drift: git log failed for %s: %s",
            workspace_id, e.stderr,
        )
        return 0

    git_lines = [
        line for line in proc.stdout.strip().split("\n") if line
    ]
    if not git_lines:
        return 0
    git_commits = [line.split("\x00", 1) for line in git_lines]

    with session_factory() as s:
        existing_shas = set(s.scalars(
            select(ArtifactVersion.commit_sha)
            .where(ArtifactVersion.workspace_id == workspace_id)
        ).all())
        backfilled = 0
        for sha, message in git_commits:
            if sha in existing_shas:
                continue
            s.add(ArtifactVersion(
                workspace_id=workspace_id,
                commit_sha=sha,
                triggering_turn_id=None,
                created_by="reconciled",
                message=message,
            ))
            backfilled += 1
        if backfilled > 0:
            s.add(WorkspaceEvent(
                workspace_id=workspace_id,
                kind="git_drift_reconciled",
                payload={"backfilled_count": backfilled},
            ))
        s.commit()
    return backfilled
```

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_reconcile.py -v
# expect 3 passing
uv run ruff format src/call_monitor/proposal_assist/reconcile.py tests/proposal_assist/test_reconcile.py
uv run ruff check src/call_monitor/proposal_assist/reconcile.py tests/proposal_assist/test_reconcile.py
git add src/call_monitor/proposal_assist/reconcile.py tests/proposal_assist/test_reconcile.py
git commit -m "feat(b5): reconcile_workspace_git_drift — backfill orphan commits"
```

---

## Task 6: register all tools in the MCP server builder

Now that all eight tool implementations exist, wire them into `build_proposal_assist_mcp_server`. Use the `@tool` decorator with the SDK's input schema syntax.

**Files:**
- Modify: `src/call_monitor/proposal_assist/tools/registry.py`
- Test: extend `tests/proposal_assist/test_tools_registry.py`

- [ ] **Step 1: Write failing test (add to test_tools_registry.py)**

```python
def test_build_mcp_server_registers_all_eight_tools():
    """All eight tools are registered with their expected names."""
    import uuid
    from pathlib import Path
    from call_monitor.proposal_assist.tools.registry import (
        ToolContext, build_proposal_assist_mcp_server,
    )

    ctx = ToolContext(
        workspace_id=uuid.uuid4(),
        repo_dir=Path("/tmp/x"),
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )
    server = build_proposal_assist_mcp_server(ctx)
    # SDK exposes the tools list via .tools attribute on McpSdkServerConfig.
    # If the attribute name differs, adjust — but the count should be 8.
    tools = getattr(server, "tools", None)
    if tools is None and isinstance(server, dict):
        tools = server.get("tools", [])
    assert tools is not None
    names = {getattr(t, "name", None) or t.get("name") if isinstance(t, dict) else getattr(t, "name", None) for t in tools}
    assert names == {
        "read_proposal", "edit_proposal", "replace_proposal",
        "get_artifact_history", "read_artifact_at", "diff_artifact",
        "get_call_details", "get_profile",
    }
```

- [ ] **Step 2: Implement — update `_collect_tools`**

Replace the empty `_collect_tools(ctx)` body. Each handler is a `@tool`-decorated async closure over `ctx` that calls the existing `_impl` function.

```python
from claude_agent_sdk import tool


def _collect_tools(ctx: ToolContext) -> list:
    from call_monitor.proposal_assist.tools.context import (
        get_call_details_impl, get_profile_impl,
    )
    from call_monitor.proposal_assist.tools.history import (
        diff_artifact_impl, get_artifact_history_impl, read_artifact_at_impl,
    )
    from call_monitor.proposal_assist.tools.proposal import (
        edit_proposal_impl, read_proposal_impl, replace_proposal_impl,
    )

    @tool("read_proposal", "Return the current proposal.md content.", {})
    async def read_proposal(args):
        return await read_proposal_impl(ctx)

    @tool(
        "edit_proposal",
        "Apply a list of {old_string, new_string} surgical edits to "
        "proposal.md. All-or-nothing: if any anchor is missing or "
        "ambiguous, no edits are applied. Auto-commits on success.",
        {"edits": list, "triggering_turn_id": str | None},
    )
    async def edit_proposal(args):
        ttid = args.get("triggering_turn_id")
        from uuid import UUID
        return await edit_proposal_impl(
            ctx,
            edits=args["edits"],
            triggering_turn_id=UUID(ttid) if ttid else None,
        )

    @tool(
        "replace_proposal",
        "Overwrite proposal.md with new content. For large structural "
        "rewrites. Auto-commits.",
        {"content": str, "triggering_turn_id": str | None},
    )
    async def replace_proposal(args):
        ttid = args.get("triggering_turn_id")
        from uuid import UUID
        return await replace_proposal_impl(
            ctx,
            content=args["content"],
            triggering_turn_id=UUID(ttid) if ttid else None,
        )

    @tool(
        "get_artifact_history",
        "Return up to `limit` recent commits on proposal.md, newest first.",
        {"limit": int},
    )
    async def get_artifact_history(args):
        return await get_artifact_history_impl(ctx, limit=int(args.get("limit", 20)))

    @tool(
        "read_artifact_at",
        "Return proposal.md content at a specific commit sha.",
        {"commit_sha": str},
    )
    async def read_artifact_at(args):
        return await read_artifact_at_impl(ctx, commit_sha=args["commit_sha"])

    @tool(
        "diff_artifact",
        "Unified diff of proposal.md between two commit shas.",
        {"commit_a": str, "commit_b": str},
    )
    async def diff_artifact(args):
        return await diff_artifact_impl(
            ctx, commit_a=args["commit_a"], commit_b=args["commit_b"],
        )

    @tool(
        "get_call_details",
        "Return the linked call's full body + metadata, or "
        "{linked: false} if the workspace has no linked call.",
        {},
    )
    async def get_call_details(args):
        return await get_call_details_impl(ctx)

    @tool(
        "get_profile",
        "Return the linked profile's description + structured fields, "
        "or {linked: false} if the workspace has no linked profile.",
        {},
    )
    async def get_profile(args):
        return await get_profile_impl(ctx)

    return [
        read_proposal, edit_proposal, replace_proposal,
        get_artifact_history, read_artifact_at, diff_artifact,
        get_call_details, get_profile,
    ]
```

**Implementer note:** The exact attribute name on `McpSdkServerConfig` (`.tools` vs `["tools"]`) depends on the SDK's internal representation — verify with a quick `dir(server)` after construction. The test in Step 1 probes both shapes; adapt the assertion if the SDK uses something else (e.g., `server.config["tools"]`).

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_tools_registry.py -v
# expect 4 passing (3 from T1 + 1 new)
uv run ruff format src/call_monitor/proposal_assist/tools/registry.py tests/proposal_assist/test_tools_registry.py
uv run ruff check src/call_monitor/proposal_assist/tools/registry.py tests/proposal_assist/test_tools_registry.py
git add src/call_monitor/proposal_assist/tools/registry.py tests/proposal_assist/test_tools_registry.py
git commit -m "feat(b5): register all 8 tools in the proposal_assist MCP server"
```

---

## Task 7: wire the adapter + factory to pass the MCP server to the SDK

`ClaudeAgentSDKAdapter` gains an optional `tools_factory: Callable[[ToolContext], McpSdkServerConfig] | None` constructor arg. At launch time, if `tools_factory` is set, the adapter builds a `ToolContext` and an MCP server, then passes both `mcp_servers={"proposal_assist": server}` and `allowed_tools=[...]` via `ClaudeAgentOptions`.

`build_workspace_controller` constructs the `tools_factory` closure (capturing `session_factory` + `settings`) and passes it to `ClaudeAgentSDKAdapter`.

The adapter NEEDS `session_factory` + `settings` inside `tools_factory` — but those don't live on `LaunchContext`. Solution: the factory wraps `build_proposal_assist_mcp_server` into a closure that captures `session_factory` + `settings`, and the adapter just builds `ToolContext(workspace_id, repo_dir)` from what it knows.

**Files:**
- Modify: `src/call_monitor/proposal_assist/drafting/claude_adapter.py`
- Modify: `src/call_monitor/proposal_assist/drafting/factory.py`
- Modify: `src/call_monitor/proposal_assist/drafting/__init__.py` (re-export `ToolContext`)
- Test: `tests/proposal_assist/test_tools_claude_adapter_wiring.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_tools_claude_adapter_wiring.py`:

```python
import asyncio
import uuid
from pathlib import Path

import pytest


def test_claude_adapter_constructor_accepts_tools_factory():
    """The adapter accepts a tools_factory; if None, it works as before."""
    from call_monitor.proposal_assist.drafting.claude_adapter import (
        ClaudeAgentSDKAdapter,
    )
    # No factory: should construct fine.
    adapter = ClaudeAgentSDKAdapter(model="x", workspaces_dir="/tmp")
    assert adapter is not None
    # With factory: also fine.
    adapter2 = ClaudeAgentSDKAdapter(
        model="x", workspaces_dir="/tmp",
        tools_factory=lambda repo_dir, workspace_id: None,  # type: ignore[arg-type]
    )
    assert adapter2 is not None


def test_factory_wires_tools_into_adapter(monkeypatch, _settings_env):
    """build_workspace_controller passes a tools_factory closure to the
    claude adapter; the closure is what builds the per-workspace MCP
    server."""
    from call_monitor.config import Settings
    from call_monitor.proposal_assist.drafting.factory import (
        build_workspace_controller,
    )

    monkeypatch.delenv("DRAFTING_ADAPTER", raising=False)  # default = claude
    monkeypatch.setenv("WORKSPACES_DIR", "/tmp/test_workspaces")
    settings = Settings()

    controller = build_workspace_controller(
        settings=settings, session_factory=None,  # type: ignore[arg-type]
    )
    # Reach through controller → registry → adapter to inspect:
    adapter = controller._registry._adapter
    assert getattr(adapter, "_tools_factory", None) is not None


def test_adapter_launch_passes_mcp_servers_and_allowed_tools(monkeypatch):
    """When tools_factory is set, adapter.launch passes
    ClaudeAgentOptions(mcp_servers=..., allowed_tools=...)."""
    from call_monitor.proposal_assist.drafting import claude_adapter as ca
    from call_monitor.proposal_assist.drafting.contract import LaunchContext

    captured: dict = {}
    real_options = ca.ClaudeAgentOptions

    def spy_options(**kw):
        captured.update(kw)
        return real_options(**kw)

    monkeypatch.setattr(ca, "ClaudeAgentOptions", spy_options)

    class _StubClient:
        def __init__(self, options): self.options = options
        async def connect(self): pass

    monkeypatch.setattr(ca, "ClaudeSDKClient", _StubClient)

    sentinel_server = object()

    def fake_factory(repo_dir, workspace_id):
        return sentinel_server

    adapter = ca.ClaudeAgentSDKAdapter(
        model="x", workspaces_dir="/tmp",
        tools_factory=fake_factory,  # type: ignore[arg-type]
    )
    ctx = LaunchContext(workspace_id=uuid.uuid4())
    asyncio.run(adapter.launch(ctx))

    mcp = captured.get("mcp_servers")
    assert mcp is not None
    assert "proposal_assist" in mcp
    assert mcp["proposal_assist"] is sentinel_server

    allowed = captured.get("allowed_tools")
    assert allowed is not None
    # MCP tool naming convention: mcp__<server>__<tool>.
    assert any("read_proposal" in t for t in allowed)
    assert any("edit_proposal" in t for t in allowed)
```

- [ ] **Step 2: Modify `claude_adapter.py`**

Add a constructor parameter:

```python
def __init__(
    self,
    *,
    model: str,
    workspaces_dir: str,
    permission_mode: str = "bypassPermissions",
    tools_factory: "Callable[[Path, uuid.UUID], McpSdkServerConfig] | None" = None,
) -> None:
    ...
    self._tools_factory = tools_factory
```

In `launch()`, after computing `repo_dir`:

```python
options_kwargs: dict[str, Any] = dict(
    cwd=str(repo_dir),
    model=self._model,
    permission_mode=self._permission_mode,
)
if context.resume_session_id is not None:
    options_kwargs["resume"] = context.resume_session_id
if self._tools_factory is not None:
    mcp_server = self._tools_factory(repo_dir, workspace_id)
    options_kwargs["mcp_servers"] = {"proposal_assist": mcp_server}
    # MCP tools are referenced as mcp__<server>__<tool>.
    options_kwargs["allowed_tools"] = [
        f"mcp__proposal_assist__{name}"
        for name in (
            "read_proposal", "edit_proposal", "replace_proposal",
            "get_artifact_history", "read_artifact_at", "diff_artifact",
            "get_call_details", "get_profile",
        )
    ]
options = ClaudeAgentOptions(**options_kwargs)
```

- [ ] **Step 3: Modify `factory.py`**

```python
def build_workspace_controller(
    *,
    settings: Settings,
    session_factory: sessionmaker,
) -> WorkspaceController:
    adapter = _build_adapter(settings, session_factory)
    registry = SubprocessRegistry(
        adapter=adapter,
        idle_seconds=settings.workspace_idle_timeout_seconds,
    )
    return WorkspaceController(
        registry=registry, session_factory=session_factory,
    )


def _build_adapter(settings: Settings, session_factory: sessionmaker) -> DraftingAgent:
    choice = settings.drafting_adapter.lower()
    if choice == "fake":
        return FakeAgentAdapter()
    if choice == "claude":
        from call_monitor.proposal_assist.drafting.claude_adapter import (
            ClaudeAgentSDKAdapter,
        )
        from call_monitor.proposal_assist.tools.registry import (
            ToolContext,
            build_proposal_assist_mcp_server,
        )

        def tools_factory(repo_dir, workspace_id):
            ctx = ToolContext(
                workspace_id=workspace_id,
                repo_dir=repo_dir,
                session_factory=session_factory,
                settings=settings,
            )
            return build_proposal_assist_mcp_server(ctx)

        return ClaudeAgentSDKAdapter(
            model=settings.proposal_assist_llm_model,
            workspaces_dir=settings.workspaces_dir,
            tools_factory=tools_factory,
        )
    raise ValueError(...)
```

- [ ] **Step 4: Update `drafting/__init__.py`**

Add `ToolContext` to the re-export list so B7 can import it.

Actually `ToolContext` lives in `tools/`, not `drafting/`. Re-export from `proposal_assist/tools/__init__.py` (already done in Task 1) is sufficient. Skip drafting/__init__.py change.

- [ ] **Step 5: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_tools_claude_adapter_wiring.py tests/proposal_assist/test_drafting_claude_adapter.py tests/proposal_assist/test_drafting_factory.py -v
# expect all passing — existing adapter/factory tests must still pass
uv run ruff format src/call_monitor/proposal_assist/drafting/claude_adapter.py src/call_monitor/proposal_assist/drafting/factory.py tests/proposal_assist/test_tools_claude_adapter_wiring.py
uv run ruff check src/call_monitor/proposal_assist/drafting/claude_adapter.py src/call_monitor/proposal_assist/drafting/factory.py tests/proposal_assist/test_tools_claude_adapter_wiring.py
git add src/call_monitor/proposal_assist/drafting/claude_adapter.py src/call_monitor/proposal_assist/drafting/factory.py tests/proposal_assist/test_tools_claude_adapter_wiring.py
git commit -m "feat(b5): wire tools_factory through adapter + factory into ClaudeAgentOptions"
```

---

## Task 8: controller hooks reconciliation into launch

`WorkspaceController.launch()` already has a `work_session` block (orphan sweep + last_sdk_session_id fetch). Add reconciliation:

```python
with self._session_factory() as work_session:
    sweep_orphaned_turn_jobs(work_session, workspace_id)
    ws_row = work_session.scalar(
        select(Workspace).where(Workspace.id == workspace_id)
    )
    resume_session_id = ws_row.last_sdk_session_id if ws_row else None
    work_session.commit()
# Reconciliation is its own session (it does its own writes).
repo_dir = self._workspaces_dir / str(workspace_id) / "repo"
reconcile_workspace_git_drift(self._session_factory, workspace_id, repo_dir)
```

Wait — `WorkspaceController` doesn't have a `_workspaces_dir`. The controller is decoupled from filesystem details (the adapter owns those). The reconciliation needs `repo_dir`. Options:
- Add `workspaces_dir` to the controller (matches adapter's design).
- Have the factory construct a controller-bound reconciler closure: `controller._reconciler(workspace_id)` does the lookup.
- Inject the reconciler as a Callable.

Cleanest: add `workspaces_dir: str` (or `Path`) to `WorkspaceController.__init__`. The factory passes `settings.workspaces_dir`.

**Files:**
- Modify: `src/call_monitor/proposal_assist/drafting/controller.py`
- Modify: `src/call_monitor/proposal_assist/drafting/factory.py`
- Test: extend `tests/proposal_assist/test_reconcile.py` with an integration test through the controller.

- [ ] **Step 1: Write failing test (add to test_reconcile.py)**

```python
import asyncio

import pytest

from call_monitor.proposal_assist.drafting.controller import WorkspaceController
from call_monitor.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_monitor.proposal_assist.drafting.registry import SubprocessRegistry


@pytest.mark.asyncio
async def test_controller_launch_runs_reconciliation(pg_session_factory, tmp_path):
    """controller.launch backfills orphan commits before the subprocess
    is launched."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    workspaces_dir = tmp_path / "workspaces"
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
        workspaces_dir=workspaces_dir,
    )

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    # Seed the workspace's repo with TWO commits but no artifact_versions rows.
    repo_dir = workspaces_dir / str(wsid) / "repo"
    shas = _init_repo_with_n_commits(repo_dir, 2)

    await controller.launch(wsid)

    with pg_session_factory() as s:
        rows = list(s.scalars(
            select(ArtifactVersion).where(ArtifactVersion.workspace_id == wsid)
        ))
    assert len(rows) == 2
    assert {r.commit_sha for r in rows} == set(shas)
    assert all(r.created_by == "reconciled" for r in rows)
```

- [ ] **Step 2: Modify `WorkspaceController.__init__`**

Add `workspaces_dir: Path | str` keyword param. Inside `launch()`, after the work_session block, call `reconcile_workspace_git_drift(self._session_factory, workspace_id, repo_dir)`. Wrap in try/except — reconciliation failures should NOT block launch:

```python
try:
    reconcile_workspace_git_drift(
        self._session_factory, workspace_id,
        Path(self._workspaces_dir) / str(workspace_id) / "repo",
    )
except Exception:
    logger.exception(
        "controller.launch: reconciliation failed for %s; continuing",
        workspace_id,
    )
```

- [ ] **Step 3: Modify `factory.py`**

Pass `workspaces_dir=settings.workspaces_dir` to `WorkspaceController(...)`.

- [ ] **Step 4: Update existing controller tests**

The existing `WorkspaceController(registry=..., session_factory=...)` constructor calls in test files now need `workspaces_dir=`. Either:
- Add a sensible default (e.g., `workspaces_dir: Path | str = "var/workspaces"`) to make the change non-breaking.
- Or update every existing constructor call.

Prefer the default — fewer test churn. Document on the field that production passes `settings.workspaces_dir` explicitly.

- [ ] **Step 5: Run + commit**

```bash
uv run pytest tests/proposal_assist/ -q
# expect everything green, including the new integration test
uv run ruff format src/call_monitor/proposal_assist/drafting/controller.py src/call_monitor/proposal_assist/drafting/factory.py tests/proposal_assist/test_reconcile.py
uv run ruff check src/call_monitor/proposal_assist/drafting/ tests/proposal_assist/test_reconcile.py
git add src/call_monitor/proposal_assist/drafting/controller.py src/call_monitor/proposal_assist/drafting/factory.py tests/proposal_assist/test_reconcile.py
git commit -m "feat(b5): controller runs reconciliation on launch"
```

---

## Task 9: end-to-end tool-call test

Verify the FULL tool-call path through B3/B4/B5 against the FakeAgentAdapter: the agent's `ToolUseEvent` produces a `chat_turns` row of role='assistant' with `type='tool_use'`, and the matching `ToolResultEvent` produces a row of role='tool'. This is mostly covered by existing B4 processor tests, but B5 adds the wiring confirmation.

Specifically: confirm that the TOOL HANDLER actually executes when the SDK calls it. Since FakeAdapter doesn't actually run the SDK loop, we can't easily test handler execution end-to-end without an API key.

**Pragmatic substitute:** test that each `*_impl` function works against a real ToolContext + the controller's repo dir. This is what Tasks 2/3/4 already do — Task 9 is the integration glue.

Optional smoke: a "with-API-key" test that's skipped without `ANTHROPIC_API_KEY` and asks the real SDK to invoke `read_proposal`. Out of scope for B4-style auto-run; document.

**Files:**
- Test only: `tests/proposal_assist/test_tools_end_to_end.py`

- [ ] **Step 1: Write integration test**

```python
"""End-to-end: build the MCP server, hand-invoke one tool's handler
through it, verify the closure properly bound ctx."""
import asyncio
import subprocess
import uuid
from pathlib import Path

import pytest

from call_monitor.proposal_assist.tools.proposal import (
    edit_proposal_impl, read_proposal_impl,
)
from call_monitor.proposal_assist.tools.registry import (
    ToolContext, build_proposal_assist_mcp_server,
)
from tests.proposal_assist.conftest import make_workspace


def _init_repo(repo_dir):
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "a@b"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    (repo_dir / "proposal.md").write_text("# Initial\n\nText.\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def test_mcp_server_contains_tools_bound_to_context(
    pg_session_factory, _settings_env, tmp_path,
):
    from call_monitor.config import Settings
    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    ctx = ToolContext(
        workspace_id=wsid, repo_dir=repo_dir,
        session_factory=pg_session_factory, settings=settings,
    )
    server = build_proposal_assist_mcp_server(ctx)
    # Smoke: server has tools, can be passed to ClaudeAgentOptions later.
    assert server is not None
```

- [ ] **Step 2: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_tools_end_to_end.py -v
uv run ruff format tests/proposal_assist/test_tools_end_to_end.py
git add tests/proposal_assist/test_tools_end_to_end.py
git commit -m "test(b5): MCP server smoke test bound to a real ToolContext"
```

---

## Task 10: milestone

Full suite + ruff + surface verification.

- [ ] **Step 1: Run + lint**

```bash
uv run pytest -q
# expect ~770+ passing
uv run ruff check src tests
```

- [ ] **Step 2: Surface verification**

```bash
grep -rn 'from call_monitor.proposal_assist.tools' src tests
```

Expected: imports only in `factory.py`, the tools' own tests, and the tool modules themselves.

```bash
grep -rn 'create_sdk_mcp_server\|@tool' src
```

Expected: only `tools/registry.py` calls `create_sdk_mcp_server`. The `@tool` decorator is used only inside `_collect_tools`.

```bash
grep -rn 'claude_agent_sdk' src
```

Expected: only `claude_adapter.py` and `tools/registry.py` import from `claude_agent_sdk`. Two files total — the SDK firewall holds.

- [ ] **Step 3: Surface for the user**

> B5 complete. The drafting agent now has 8 custom tools: read/edit/replace_proposal, get_artifact_history, read_artifact_at, diff_artifact, get_call_details, get_profile. Tool registration is per-workspace via in-process MCP server. Reconciliation backfills orphan git commits on every workspace open.
>
> **Out of scope (next slices):**
> - POST /workspaces/<id>/turns + SSE bridge + chat UI → **B6**.
> - Disclosure modal, per-turn banner, kill switch, cap-rejection UX → **B6/B8**.
> - Figure/attachment tools (`describe_figure`, `look_at_figure`, `read_preliminary_data`) → **B7** (plugs into B5's registry).
> - Admin "verify and reconcile workspace" button → **B6/B8**.

---

## Self-review notes (for the engineer running this plan)

1. **Tool error returns vs exceptions.** Every tool returns `{"error": "..."}` on validation failure rather than raising. This matches the SDK convention — the model SEES the error string and can choose to retry with corrected inputs. Raising would crash the SDK's tool-invocation loop.

2. **edit_proposal is all-or-nothing.** A single failed anchor aborts the whole batch; no partial edits land. The Claude Code edit tool works the same way; the agent will be familiar.

3. **Ambiguity check on edit_proposal.** Multi-match anchors are rejected because the agent's intent is unambiguous — if it wanted to replace "foo" everywhere, it'd use `replace_proposal` or be more specific. Saves a footgun.

4. **Tool descriptions matter.** The `@tool` decorator's `description` is what the model sees when deciding which tool to call. Keep them precise and user-action-oriented ("Apply edits", not "Edit method").

5. **Reconciliation is forward-only.** Git commits are never undone. The reconciliation only backfills DB rows for git commits, never the reverse. Per spec.

6. **Per-workspace MCP server lifetime.** Built fresh at every launch (when the registry doesn't have a cached handle). When the idle sweep terminates the subprocess, the server is discarded with it. No leaks.

7. **`mcp__proposal_assist__<tool>` naming.** The SDK exposes MCP tools under the `mcp__<server>__<tool>` name format in `allowed_tools`. Verify with `dir(server)` if the SDK has changed.

8. **`allowed_tools` blocks built-ins.** By passing `allowed_tools` explicitly, we forbid the SDK's built-in Bash/Read/Edit tools at the same time. This is the proper fix for B3's "soft-replay re-fires built-in tools" FIXME — once tools are wired, the agent only has our 8 + (anything the SDK considers always-available).

9. **No commit signing.** Spec doesn't require GPG-signed commits. The git config in tests uses simple email/name; production uses the workspace process's git config.

10. **Reconciliation runs on EVERY launch.** Including warm-launches where no new commits arrived. The query is cheap (selects existing shas from `artifact_versions`, then scans `git log`). Idempotent. If perf matters, B6 could add a "skip if last commit sha matches latest artifact_versions row" optimization.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-20-base-spec-5-tool-surface.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, two-stage review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
