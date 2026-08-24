import subprocess
from pathlib import Path

from call_manager.db.models import ArtifactVersion, ClaimAnalysisJob, WorkspaceEvent
from call_manager.proposal_assist.artifacts import Artifact
from call_manager.proposal_assist.citations.gitio import _git_env
from call_manager.proposal_assist.editing.save import save_section
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.ports.stamp import stamp_for_workspace
from tests.proposal_assist.conftest import make_workspace

SPEC_DOC = "## Aims\nbe great.\n\n## Approach\ndo science.\n"


def _init_spec_repo(repo_dir: Path, content: str) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    env = _git_env()
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True, env=env)
    (repo_dir / "proposal-spec.md").write_text(content)
    subprocess.run(["git", "add", "proposal-spec.md"], cwd=repo_dir, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init spec"], cwd=repo_dir, check=True, env=env)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return out.stdout.strip()


def test_spec_save_commits_spec_file_and_skips_tier1_tier2(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        # The draft path would enqueue a ClaimAnalysisJob (Tier-2); the spec path must enqueue none.
        repo = Path(tmp_path) / str(ws.id) / "repo"
        head = _init_spec_repo(repo, SPEC_DOC)
        sec_sha = split_sections(SPEC_DOC)[1].sha  # the "## Aims" section

        result = save_section(
            session=s,
            workspace=ws,
            stamp=stamp_for_workspace(ws),
            user_id=ws.owner_id,
            section_index=1,
            new_text="## Aims\nbe truly great.\n",
            base_commit_sha=head,
            base_section_sha=sec_sha,
            workspaces_dir=str(tmp_path),
            artifact=Artifact.SPEC,
        )
        s.flush()

        # proposal-spec.md was rewritten; proposal.md was NOT created.
        assert (repo / "proposal-spec.md").read_text().startswith("## Aims\nbe truly great.")
        assert not (repo / "proposal.md").exists()
        # Spec path skips Tier-1 (no issues) and Tier-2 (no enqueue).
        assert result.issues == []
        assert result.job_enqueued is False
        assert result.changed is True
        assert s.query(ClaimAnalysisJob).filter(ClaimAnalysisJob.workspace_id == ws.id).count() == 0
        # Provenance is tagged spec.
        av = s.query(ArtifactVersion).filter(ArtifactVersion.workspace_id == ws.id).one()
        assert av.message == "user spec edit: Aims"
        ev = s.query(WorkspaceEvent).filter(WorkspaceEvent.workspace_id == ws.id).one()
        assert ev.payload["artifact"] == "spec"


def test_spec_save_still_enforces_structure_guard(pg_session_factory, tmp_path):
    import pytest

    from call_manager.proposal_assist.editing.save import SectionStructureError

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        repo = Path(tmp_path) / str(ws.id) / "repo"
        head = _init_spec_repo(repo, SPEC_DOC)
        sec_sha = split_sections(SPEC_DOC)[1].sha
        with pytest.raises(SectionStructureError):
            save_section(
                session=s,
                workspace=ws,
                stamp=stamp_for_workspace(ws),
                user_id=ws.owner_id,
                section_index=1,
                new_text="be great.\n",  # dropped the "## Aims" heading
                base_commit_sha=head,
                base_section_sha=sec_sha,
                workspaces_dir=str(tmp_path),
                artifact=Artifact.SPEC,
            )


def test_raw_draft_string_still_runs_draft_pipeline(pg_session_factory, tmp_path):
    # Normalization: a stringly-typed "draft" must hit Tier-1 (issues computed),
    # not silently degrade to the spec path.
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        doc = "preamble\n\n## Methods\nSee [@ghost].\n"  # bare marker -> a Tier-1 issue
        repo = Path(tmp_path) / str(ws.id) / "repo"
        env = _git_env()
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
        (repo / "proposal.md").write_text(doc)
        subprocess.run(["git", "add", "proposal.md"], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=repo, check=True, env=env)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout.strip()
        sec_sha = split_sections(doc)[1].sha

        result = save_section(
            session=s,
            workspace=ws,
            stamp=stamp_for_workspace(ws),
            user_id=ws.owner_id,
            section_index=1,
            new_text="## Methods\nSee [@ghost] again.\n",
            base_commit_sha=head,
            base_section_sha=sec_sha,
            workspaces_dir=str(tmp_path),
            artifact="draft",  # raw string, not the enum member
        )
        assert any(i["type"] == "bare_marker" for i in result.issues)


def test_spec_save_survives_cross_artifact_head_move(pg_session_factory, tmp_path):
    # Spec §7/§10: a DRAFT-only commit advances repo HEAD but does NOT change the
    # spec section's bytes, so a spec save with the pre-draft base_commit_sha + the
    # still-current base_section_sha succeeds (shared predicate: head != base_commit
    # AND live.sha != base_section -> stale; here the section sha is unchanged).
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        repo = Path(tmp_path) / str(ws.id) / "repo"
        head0 = _init_spec_repo(repo, SPEC_DOC)
        sec_sha = split_sections(SPEC_DOC)[1].sha
        # Draft-only git commit -> HEAD moves, spec section bytes unchanged.
        env = _git_env()
        (repo / "proposal.md").write_text("draft body\n")
        subprocess.run(["git", "add", "proposal.md"], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "draft move"], cwd=repo, check=True, env=env)

        result = save_section(
            session=s,
            workspace=ws,
            stamp=stamp_for_workspace(ws),
            user_id=ws.owner_id,
            section_index=1,
            new_text="## Aims\nrevised under a moved HEAD.\n",
            base_commit_sha=head0,  # PRE-draft HEAD (now != current HEAD)
            base_section_sha=sec_sha,  # spec section UNCHANGED -> not stale
            workspaces_dir=str(tmp_path),
            artifact=Artifact.SPEC,
        )
        assert result.changed is True


def test_spec_save_stale_section_raises(pg_session_factory, tmp_path):
    import pytest

    from call_manager.proposal_assist.editing.save import StaleSectionError

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        repo = Path(tmp_path) / str(ws.id) / "repo"
        head0 = _init_spec_repo(repo, SPEC_DOC)
        sec_sha = split_sections(SPEC_DOC)[1].sha
        # First spec save changes the section bytes (HEAD moves, section sha changes).
        save_section(
            session=s,
            workspace=ws,
            stamp=stamp_for_workspace(ws),
            user_id=ws.owner_id,
            section_index=1,
            new_text="## Aims\nfirst change.\n",
            base_commit_sha=head0,
            base_section_sha=sec_sha,
            workspaces_dir=str(tmp_path),
            artifact=Artifact.SPEC,
        )
        s.flush()
        # A second save with the ORIGINAL (now stale) base hits
        # head != base_commit AND live.sha != base_section -> StaleSectionError.
        with pytest.raises(StaleSectionError):
            save_section(
                session=s,
                workspace=ws,
                stamp=stamp_for_workspace(ws),
                user_id=ws.owner_id,
                section_index=1,
                new_text="## Aims\nconflicting change.\n",
                base_commit_sha=head0,
                base_section_sha=sec_sha,
                workspaces_dir=str(tmp_path),
                artifact=Artifact.SPEC,
            )
