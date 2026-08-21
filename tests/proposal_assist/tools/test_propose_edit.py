"""propose_edit_impl: writes a pending prose_edit proposed_change using the
agent's READ-TIME base (anti-clobber), never a live recompute. Rejects a
section the agent never read."""

import subprocess
import uuid
from pathlib import Path

import pytest

from call_manager.config import Settings
from call_manager.db.models import ProposedChange
from call_manager.proposal_assist.editing.save import save_section
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.port_impl import build_ports
from call_manager.proposal_assist.tools.proposal import (
    PROPOSAL_FILENAME,
    propose_edit_impl,
    read_proposal_impl,
)
from call_manager.proposal_assist.tools.registry import ToolContext, snapshot_invocation
from tests.proposal_assist.conftest import make_workspace


def _init_repo(repo_dir: Path, content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def _build_ctx(pg_session_factory, tmp_path, content):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    repo = Path(tmp_path) / str(wsid) / "repo"
    _init_repo(repo, content)
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=repo,
        session_factory=pg_session_factory,
        settings=Settings(),
        # make_workspace leaves lifecycle_generation at its server_default 0, so the
        # actor claims that generation and the port's live_at_generation check passes.
        lifecycle_generation=0,
    )
    return ctx, wsid, repo


@pytest.mark.asyncio
async def test_propose_edit_uses_read_time_base_not_live(
    pg_session_factory, _settings_env, tmp_path
):
    content = "preamble\n\n## Methods\noriginal.\n"
    ctx, wsid, repo = _build_ctx(pg_session_factory, tmp_path, content)

    # Agent reads — snapshots head + section shas at T0.
    await read_proposal_impl(ctx)
    read_time_section_sha = split_sections(content)[1].sha

    # A human edits the SAME section after the read (HEAD + section sha move).
    with pg_session_factory() as s:
        ws = s.get(__import__("call_manager.db.models", fromlist=["Workspace"]).Workspace, wsid)
        base_commit, base_section = (ctx.agent_view_head, read_time_section_sha)
        save_section(
            session=s,
            workspace=ws,
            user_id=ws.owner_id,
            section_index=1,
            new_text="## Methods\nhuman-edited.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            workspaces_dir=str(tmp_path),
        )
        s.commit()

    # Agent now proposes — must record the READ-TIME base (so accept goes stale),
    # NOT the live (human-edited) base.
    result = await propose_edit_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=snapshot_invocation(ctx),
        section_index=1,
        new_text="## Methods\nagent-version.\n",
    )
    assert "error" not in result, result
    assert result["status"] == "pending"

    with pg_session_factory() as s:
        pc = s.get(ProposedChange, uuid.UUID(result["proposed_change_id"]))
        assert pc.kind == "prose_edit"
        assert pc.section_index == 1
        # The recorded base is the READ-TIME sha, not the live human-edited one.
        assert pc.base_section_sha == read_time_section_sha
        assert pc.base_commit_sha == ctx.agent_view_head


@pytest.mark.asyncio
async def test_propose_edit_rejects_unread_section(pg_session_factory, _settings_env, tmp_path):
    content = "preamble\n\n## Methods\noriginal.\n"
    ctx, wsid, repo = _build_ctx(pg_session_factory, tmp_path, content)
    # No read_proposal call: agent_view_head is None and agent_view_sections {}.
    result = await propose_edit_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=snapshot_invocation(ctx),
        section_index=1,
        new_text="## Methods\nx.\n",
    )
    assert "error" in result
    assert "read_proposal" in result["error"]


@pytest.mark.asyncio
async def test_propose_edit_rejects_section_read_but_not_in_view(
    pg_session_factory, _settings_env, tmp_path
):
    content = "preamble\n\n## Methods\noriginal.\n"
    ctx, wsid, repo = _build_ctx(pg_session_factory, tmp_path, content)
    await read_proposal_impl(ctx)  # populates indices 0 and 1 only
    result = await propose_edit_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=snapshot_invocation(ctx),
        section_index=5,
        new_text="## Ghost\nx.\n",
    )
    assert "error" in result
    assert "read_proposal" in result["error"]


@pytest.mark.asyncio
async def test_propose_edit_records_selection_provenance(
    pg_session_factory, _settings_env, tmp_path
):
    import uuid as _uuid

    content = "preamble\n\n## Methods\noriginal.\n"
    ctx, wsid, repo = _build_ctx(pg_session_factory, tmp_path, content)
    await read_proposal_impl(ctx)

    ctx.turn_selection = {
        "anchor": "the bit", "from_offset": 5, "to_offset": 12, "section_index": 1
    }
    # Snapshot AFTER stamping the selection: the invocation freezes the live ctx, so
    # the port folds THIS turn's selection into the durable row.
    result = await propose_edit_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=snapshot_invocation(ctx),
        section_index=1,
        new_text="## Methods\nnew body\n",
    )
    assert "error" not in result, result
    pc_id = result["proposed_change_id"]

    with pg_session_factory() as s:
        pc = s.get(ProposedChange, _uuid.UUID(pc_id))
        assert pc.payload["selection_anchor"] == "the bit"
        assert pc.payload["selection_from"] == 5
        assert pc.payload["selection_to"] == 12


@pytest.mark.asyncio
async def test_propose_edit_plain_turn_omits_selection_keys(
    pg_session_factory, _settings_env, tmp_path
):
    """When ctx.turn_selection is None (a plain chat turn) the ProposedChange
    payload must NOT contain selection_anchor / selection_from / selection_to.
    Guards the `and ctx.turn_selection` short-circuit in propose_edit_impl."""
    import uuid as _uuid

    content = "preamble\n\n## Methods\noriginal.\n"
    ctx, wsid, repo = _build_ctx(pg_session_factory, tmp_path, content)
    await read_proposal_impl(ctx)

    # Explicitly None — plain chat turn with no text selection.
    ctx.turn_selection = None

    result = await propose_edit_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=snapshot_invocation(ctx),
        section_index=1,
        new_text="## Methods\nnew body\n",
    )
    assert "error" not in result, result

    with pg_session_factory() as s:
        pc = s.get(ProposedChange, _uuid.UUID(result["proposed_change_id"]))
        assert "selection_anchor" not in pc.payload
        assert "selection_from" not in pc.payload
        assert "selection_to" not in pc.payload
        # The normal new_text payload key is present (sanity check).
        assert "new_text" in pc.payload
