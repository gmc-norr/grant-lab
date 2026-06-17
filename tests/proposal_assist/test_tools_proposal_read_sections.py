"""read_proposal_impl must populate ctx.agent_view_sections (section_index ->
section sha) at read time, HEAD-first, so propose_edit can take an anti-clobber
read-time base."""

import subprocess
import uuid
from pathlib import Path

import pytest

from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.tools.proposal import (
    PROPOSAL_FILENAME,
    read_proposal_impl,
)
from call_manager.proposal_assist.tools.registry import ToolContext


def _init_repo(repo_dir: Path, content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


@pytest.mark.asyncio
async def test_read_proposal_populates_agent_view_sections(tmp_path):
    content = "preamble\n\n## Methods\noriginal.\n\n## Results\nfindings.\n"
    repo = Path(tmp_path) / "repo"
    _init_repo(repo, content)
    ctx = ToolContext(
        workspace_id=uuid.uuid4(),
        repo_dir=repo,
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )

    result = await read_proposal_impl(ctx)

    assert result["content"] == content
    expected = {s.index: s.sha for s in split_sections(content)}
    assert ctx.agent_view_sections == expected
    # the full read-time proposal text is retained for propose_citation's
    # read-time anchor->section mapping.
    assert ctx.agent_view_proposal == content
    # sanity: head was captured too (existing behavior)
    assert ctx.agent_view_head is not None
