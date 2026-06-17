import subprocess
from pathlib import Path

from call_manager.db.models import TurnJob
from call_manager.proposal_assist.editing.save import save_section
from call_manager.proposal_assist.editing.sectionizer import split_sections
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
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
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
        assert result.commit_sha  # no TurnInFlightError raised
