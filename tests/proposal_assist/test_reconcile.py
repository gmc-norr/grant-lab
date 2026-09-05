import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from grant_lab.db.models import ArtifactVersion, WorkspaceEvent
from grant_lab.proposal_assist.drafting.locking import commit_mutex_key
from grant_lab.proposal_assist.reconcile import reconcile_workspace_git_drift
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


@pytest.mark.asyncio
async def test_controller_launch_runs_reconciliation(pg_session_factory, tmp_path):
    """controller.launch backfills orphan commits before the subprocess
    is launched."""
    from grant_lab.proposal_assist.drafting.controller import WorkspaceController
    from grant_lab.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
    from grant_lab.proposal_assist.drafting.registry import SubprocessRegistry

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
        rows = list(s.scalars(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wsid)))
    assert len(rows) == 2
    assert {r.commit_sha for r in rows} == set(shas)
    assert all(r.created_by == "reconciled" for r in rows)


def test_reconcile_git_drift_writes_nothing_to_a_soft_deleted_workspace(
    pg_session_factory, tmp_path
):
    """The workspace was soft-deleted before reconcile ran.

    The commits are real and orphaned, so unfenced reconcile backfilled all of
    them. The fenced version must insert ZERO rows and ZERO audit events — the
    INSERT ... SELECT ... WHERE live_at_generation finds no live workspace.
    """
    repo_dir = tmp_path / "repo"
    _init_repo_with_n_commits(repo_dir, 2)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        wsid = ws.id
        ws.soft_deleted_at = datetime.now(UTC)
        s.commit()

    backfilled = reconcile_workspace_git_drift(pg_session_factory, wsid, repo_dir)

    assert backfilled == 0
    with pg_session_factory() as s:
        rows = list(
            s.scalars(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wsid))
        )
        assert rows == []
        events = list(
            s.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wsid,
                    WorkspaceEvent.kind == "git_drift_reconciled",
                )
            )
        )
        assert events == []


def test_reconcile_git_drift_waits_for_the_commit_mutex_key(pg_session_factory, tmp_path):
    """The shared lease is what stops a delete committing underneath the INSERT.

    A delete holds the workspace's commit-mutex key EXCLUSIVELY for its whole
    transaction. Simulate that with a holder session, then run reconcile in a
    thread and observe WHILE THE HOLDER IS STILL IN FORCE. Asserting after
    releasing proves nothing: reconcile would then complete either way.

    Without acquire_shared_lease, reconcile ignores the key and inserts
    immediately — which is exactly the race fencing.py:14-24 documents for an
    INSERT, whose lack of a target row means there is no READ COMMITTED re-check
    to mask a missing fence.
    """
    repo_dir = tmp_path / "repo"
    _init_repo_with_n_commits(repo_dir, 2)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        wsid = ws.id
        s.commit()

    done = threading.Event()

    def _reconcile():
        try:
            reconcile_workspace_git_drift(pg_session_factory, wsid, repo_dir)
        finally:
            done.set()

    holder = pg_session_factory()
    observed: list[int] = []
    t = threading.Thread(target=_reconcile)
    try:
        # Transaction-scoped exclusive hold on the same key a delete would take.
        holder.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": commit_mutex_key(wsid)}
        )
        t.start()
        time.sleep(1.5)  # ample time for an UNLEASED reconcile to finish inserting
        with pg_session_factory() as probe:
            observed.append(
                probe.scalar(
                    select(func.count())
                    .select_from(ArtifactVersion)
                    .where(ArtifactVersion.workspace_id == wsid)
                )
            )
    finally:
        holder.rollback()   # releases the xact lock
        holder.close()
        assert done.wait(timeout=30), "reconcile never finished"
        t.join(timeout=5)

    assert observed == [0], (
        f"while the commit-mutex key was held exclusively, reconcile had already "
        f"inserted {observed!r} row(s) — it did not take the shared lease"
    )
    # And it must still complete once the key is free.
    with pg_session_factory() as s:
        assert s.scalar(
            select(func.count())
            .select_from(ArtifactVersion)
            .where(ArtifactVersion.workspace_id == wsid)
        ) == 2


def test_reconcile_git_drift_still_backfills_a_live_workspace(pg_session_factory, tmp_path):
    """The legitimate crash-recovery case must keep working.

    save.py:292 documents reconciliation as the repair path for a DB failure
    after the git commit. A guard that rejects this causes silent history loss.
    """
    repo_dir = tmp_path / "repo"
    shas = _init_repo_with_n_commits(repo_dir, 2)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        wsid = ws.id
        s.commit()

    backfilled = reconcile_workspace_git_drift(pg_session_factory, wsid, repo_dir)

    assert backfilled == 2
    with pg_session_factory() as s:
        got = set(
            s.scalars(
                select(ArtifactVersion.commit_sha).where(
                    ArtifactVersion.workspace_id == wsid
                )
            )
        )
        assert got == set(shas)


def test_artifact_versions_rejects_a_duplicate_sha_for_one_workspace(pg_session_factory):
    """The DB, not a caller's read-then-insert, must prevent a duplicate version
    row appearing in the user's history."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        wsid = ws.id
        s.commit()
    with pg_session_factory() as s:
        s.add(ArtifactVersion(
            workspace_id=wsid, commit_sha="deadbeef", triggering_turn_id=None,
            created_by="agent", message="first",
        ))
        s.commit()
    with pg_session_factory() as s:
        s.add(ArtifactVersion(
            workspace_id=wsid, commit_sha="deadbeef", triggering_turn_id=None,
            created_by="reconciled", message="duplicate",
        ))
        with pytest.raises(IntegrityError):
            s.commit()
