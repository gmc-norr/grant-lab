import subprocess
from pathlib import Path

from grant_lab.db.models import ProposedChange
from grant_lab.proposal_assist.editing.save import save_section
from grant_lab.proposal_assist.editing.sectionizer import split_sections
from grant_lab.proposal_assist.ports.stamp import stamp_for_workspace
from grant_lab.proposal_assist.proposals.proposing import capture_base, propose_prose_edit
from grant_lab.proposal_assist.proposals.staleness import refresh_staleness
from grant_lab.proposal_assist.tools.proposal import PROPOSAL_FILENAME
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
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\np.\n",
            base_commit_sha=bc1,
            base_section_sha=bs1,
            origin_kind="interactive_turn",
        )
        pc_impact = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=2,
            new_text="## Impact\np.\n",
            base_commit_sha=bc2,
            base_section_sha=bs2,
            origin_kind="interactive_turn",
        )
        s.flush()
        # Methods moves; Impact does not.
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(tmp_path) / str(ws.id) / "repo",
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        save_section(
            session=s,
            workspace=ws,
            stamp=stamp_for_workspace(ws),
            user_id=ws.owner_id,
            section_index=1,
            new_text="## Methods\nmoved.\n",
            base_commit_sha=head,
            base_section_sha=split_sections(content)[1].sha,
            workspaces_dir=str(tmp_path),
            base_branch=None,
        )
        s.flush()

        refresh_staleness(session=s, workspace_id=ws.id, workspaces_dir=str(tmp_path))
        s.flush()

        assert s.get(ProposedChange, pc_methods.id).status == "stale"
        assert s.get(ProposedChange, pc_impact.id).status == "pending"


def test_refresh_marks_proposal_for_deleted_section_stale(pg_session_factory, tmp_path):
    # A proposal whose section_index no longer exists in the live draft (the doc
    # shrank) is flipped stale via the `current is None` branch.
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        content = "preamble\n\n## A\na.\n\n## B\nb.\n"
        repo = Path(tmp_path) / str(ws.id) / "repo"
        _init_repo(repo, content)
        bc2, bs2 = capture_base(repo, 2)  # section 2 == "## B"
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=2,
            new_text="## B\np.\n",
            base_commit_sha=bc2,
            base_section_sha=bs2,
            origin_kind="interactive_turn",
        )
        s.flush()
        # The live draft shrinks: section 2 ("## B") is removed.
        (repo / PROPOSAL_FILENAME).write_text("preamble\n\n## A\na.\n")

        refresh_staleness(session=s, workspace_id=ws.id, workspaces_dir=str(tmp_path))
        s.flush()

        assert s.get(ProposedChange, pc.id).status == "stale"


def test_refresh_returns_zero_when_proposal_missing(pg_session_factory, tmp_path):
    # Workspace still in setup: no repo / proposal.md on disk yet. refresh is a
    # best-effort no-op (returns 0) rather than raising FileNotFoundError, so the
    # list endpoint can't 500 on an un-set-up workspace.
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\np.\n",
            base_commit_sha="0" * 40,
            base_section_sha="deadbeef",
            origin_kind="interactive_turn",
        )
        s.flush()

        n = refresh_staleness(session=s, workspace_id=ws.id, workspaces_dir=str(tmp_path))
        s.flush()

        assert n == 0
        assert s.get(ProposedChange, pc.id).status == "pending"


def test_refresh_staleness_flips_stale_spec_row(pg_session_factory, tmp_path):
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
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Aims\nproposed.\n",
            base_commit_sha="0" * 40,
            base_section_sha="deadbeef" * 8,
            origin_kind="interactive_turn",
            target_artifact="spec",
        )
        s.flush()
        n = refresh_staleness(session=s, workspace_id=ws.id, workspaces_dir=str(tmp_path))
        s.flush()
        assert n == 1
        assert s.get(ProposedChange, pc.id).status == "stale"
