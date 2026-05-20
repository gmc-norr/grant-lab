import subprocess
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
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
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
            s.add(
                ArtifactVersion(
                    workspace_id=ws.id,
                    commit_sha=sha,
                    triggering_turn_id=None,
                    created_by="agent",
                    message="present",
                )
            )
        s.commit()
        wsid = ws.id

    backfilled = reconcile_workspace_git_drift(pg_session_factory, wsid, repo_dir)
    assert backfilled == 0
    with pg_session_factory() as s:
        events = list(
            s.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wsid,
                    WorkspaceEvent.kind == "git_drift_reconciled",
                )
            )
        )
    assert events == []


def test_reconcile_backfills_orphan_commits(pg_session_factory, tmp_path):
    """Git has 3 commits, DB has only the first 2 → backfill 1."""
    repo_dir = tmp_path / "repo"
    shas = _init_repo_with_n_commits(repo_dir, 3)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        for sha in shas[:2]:
            s.add(
                ArtifactVersion(
                    workspace_id=ws.id,
                    commit_sha=sha,
                    triggering_turn_id=None,
                    created_by="agent",
                    message="present",
                )
            )
        s.commit()
        wsid = ws.id

    backfilled = reconcile_workspace_git_drift(pg_session_factory, wsid, repo_dir)
    assert backfilled == 1

    with pg_session_factory() as s:
        rows = list(s.scalars(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wsid)))
        events = list(
            s.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wsid,
                    WorkspaceEvent.kind == "git_drift_reconciled",
                )
            )
        )
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


def test_reconcile_preserves_commit_timestamp(pg_session_factory, tmp_path):
    """Codex C3 regression: backfilled artifact_versions.created_at must
    equal the git commit's committer-timestamp, not reconciliation time.

    We forge a commit with GIT_COMMITTER_DATE set to a known past time,
    then verify the backfilled row's created_at matches (to the second).
    """
    import os
    from datetime import UTC, datetime, timedelta

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "a@b"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo_dir, check=True)

    past = datetime.now(UTC) - timedelta(days=2)
    iso = past.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = iso
    env["GIT_COMMITTER_DATE"] = iso

    (repo_dir / "proposal.md").write_text("v0")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "-m", "agent: backdated"],
        cwd=repo_dir,
        check=True,
        env=env,
    )

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    backfilled = reconcile_workspace_git_drift(pg_session_factory, wsid, repo_dir)
    assert backfilled == 1

    with pg_session_factory() as s:
        row = s.scalar(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wsid))
    delta_seconds = abs((row.created_at - past).total_seconds())
    assert delta_seconds < 2, (
        f"backfilled created_at {row.created_at} should match commit "
        f"timestamp {past}; delta {delta_seconds}s"
    )
