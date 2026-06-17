import subprocess
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
