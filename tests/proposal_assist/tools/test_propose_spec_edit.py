"""read_spec snapshots the spec view; propose_spec_edit writes a pending
spec prose_edit using the read-time base; an unread section is rejected."""

import subprocess
import uuid
from pathlib import Path

import pytest

from call_manager.config import Settings
from call_manager.db.models import ProposedChange
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.port_impl import build_ports
from call_manager.proposal_assist.tools.proposal import (
    propose_spec_edit_impl,
    read_spec_impl,
)
from call_manager.proposal_assist.tools.registry import ToolContext, snapshot_invocation
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
    spec = "preamble\n\n## Aims\norig.\n"
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    repo = _spec_repo(tmp_path, wsid, spec)
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=repo,
        session_factory=pg_session_factory,
        settings=Settings(),
        lifecycle_generation=0,  # make_workspace leaves the server_default 0
    )
    read = await read_spec_impl(ctx)
    assert read["content"] == spec
    assert ctx.agent_view_spec_head is not None
    assert ctx.agent_view_spec_sections[1] == split_sections(spec)[1].sha

    result = await propose_spec_edit_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=snapshot_invocation(ctx),
        section_index=1,
        new_text="## Aims\nagent.\n",
    )
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
    repo = _spec_repo(tmp_path, wsid, "preamble\n\n## Aims\norig.\n")
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=repo,
        session_factory=pg_session_factory,
        settings=Settings(),
        lifecycle_generation=0,  # make_workspace leaves the server_default 0
    )
    result = await propose_spec_edit_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=snapshot_invocation(ctx),
        section_index=1,
        new_text="## Aims\nx.\n",
    )
    assert "error" in result and "read_spec" in result["error"]
