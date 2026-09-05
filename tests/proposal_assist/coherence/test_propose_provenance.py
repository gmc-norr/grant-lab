import uuid
from pathlib import Path

import pytest

from grant_lab.config import Settings
from grant_lab.proposal_assist.artifacts import PROPOSAL_FILENAME
from grant_lab.proposal_assist.port_impl import build_ports
from grant_lab.proposal_assist.tools.proposal import propose_edit_impl, read_proposal_impl
from grant_lab.proposal_assist.tools.registry import ToolContext, snapshot_invocation


def _ctx(repo_dir, session_factory, **overrides):
    return ToolContext(
        workspace_id=overrides.pop("workspace_id"),
        repo_dir=repo_dir,
        session_factory=session_factory,
        settings=Settings(),
        # make_workspace leaves lifecycle_generation at its server_default 0.
        lifecycle_generation=overrides.pop("lifecycle_generation", 0),
        **overrides,
    )


def _init_repo(repo_dir: Path) -> None:
    import subprocess

    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    # split_sections: index 0 is the PREAMBLE (text before the first ##), index 1
    # is "## Aims". Editing the preamble with heading text trips save_section's
    # structure guard, so the tests below edit section_index=1.
    (repo_dir / PROPOSAL_FILENAME).write_text("Intro preamble.\n\n## Aims\n\nOriginal.\n")
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


@pytest.mark.asyncio
async def test_propose_defaults_to_interactive(tmp_path, pg_session_factory, _settings_env):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id
    repo = tmp_path / str(ws_id) / "repo"
    _init_repo(repo)
    ctx = _ctx(repo, pg_session_factory, workspace_id=ws_id)
    await read_proposal_impl(ctx)
    await propose_edit_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=snapshot_invocation(ctx),
        section_index=1,
        new_text="## Aims\n\nReworded.\n",
    )

    from grant_lab.db.models import ProposedChange

    with pg_session_factory() as s:
        pc = s.query(ProposedChange).filter_by(workspace_id=ws_id).one()
        assert pc.origin_kind == "interactive_turn"
        assert pc.origin_job_id is None


@pytest.mark.asyncio
async def test_propose_stamps_coherence_provenance(tmp_path, pg_session_factory, _settings_env):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id
    repo = tmp_path / str(ws_id) / "repo"
    _init_repo(repo)
    job_id = uuid.uuid4()
    ctx = _ctx(
        repo,
        pg_session_factory,
        workspace_id=ws_id,
        origin_kind="coherence_session",
        origin_job_id=job_id,
    )
    await read_proposal_impl(ctx)
    await propose_edit_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=snapshot_invocation(ctx),
        section_index=1,
        new_text="## Aims\n\nReworded.\n",
    )

    from grant_lab.db.models import ProposedChange

    with pg_session_factory() as s:
        pc = s.query(ProposedChange).filter_by(workspace_id=ws_id).one()
        assert pc.origin_kind == "coherence_session"
        assert pc.origin_job_id == job_id
