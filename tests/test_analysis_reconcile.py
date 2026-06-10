"""Self-healing coverage reconcile (Task 13). Spec 2026-06-10 §3 + §6.

Every live HEAD section of every non-deleted workspace must end up with a
claim_analysis_jobs row even when the save-time enqueue was lost to the
post-commit DB-failure window. Covered = a job in (queued, running, done);
FAILED is deliberately NOT covered — dead-lettered content gets another
chance via this tick. Uses the production <workspaces_dir>/<id>/repo layout
(per test_analysis_invalidation).
"""

import hashlib
import subprocess
from datetime import UTC, datetime

from call_manager.db.models import ClaimAnalysisJob, User, Workspace
from call_manager.proposal_assist.analysis.reconcile import reconcile_analysis_coverage
from call_manager.proposal_assist.citations.gitio import _git_env, current_head
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir

# Empty preamble (section 0 is "") + two non-empty ## sections.
DOC = "## Alpha\nAlpha makes a factual claim.\n## Beta\nBeta makes another claim.\n"
EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def _init_repo(tmp_path, ws_id, doc=DOC):
    repo = workspace_repo_dir(tmp_path, ws_id)
    repo.mkdir(parents=True)
    (repo / "proposal.md").write_text(doc)
    env = _git_env()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, env=env)
    return repo


def _ws(session, *, email="owner@example.org"):
    user = User(email=email, is_admin=False)
    session.add(user)
    session.flush()
    ws = Workspace(owner_id=user.id, title="t")
    session.add(ws)
    session.flush()
    return ws


def _live_shas(doc=DOC):
    return [s.sha for s in split_sections(doc) if s.text.strip()]


def _seed_job(session, ws_id, *, section_sha, status, commit_sha):
    session.add(
        ClaimAnalysisJob(
            workspace_id=ws_id,
            section_index=1,
            section_heading="Alpha",
            section_sha=section_sha,
            commit_sha=commit_sha,
            status=status,
        )
    )
    session.flush()


def _queued_shas(session, ws_id):
    rows = (
        session.query(ClaimAnalysisJob.section_sha)
        .filter_by(workspace_id=ws_id, status="queued")
        .all()
    )
    return {sha for (sha,) in rows}


def test_reconcile_enqueues_uncovered_live_sections_once(pg_session_factory, tmp_path):
    """Two non-empty sections, no jobs → 2 inserts at the repo's HEAD sha; the
    empty preamble is skipped; a second run finds full coverage → 0."""
    with pg_session_factory() as s:
        ws = _ws(s)
        repo = _init_repo(tmp_path, ws.id)
        s.commit()
        ws_id = ws.id
    head = current_head(repo)

    n = reconcile_analysis_coverage(session_factory=pg_session_factory, workspaces_dir=tmp_path)
    assert n == 2

    with pg_session_factory() as s:
        jobs = s.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id).all()
        assert {j.section_sha for j in jobs} == set(_live_shas())
        assert EMPTY_SHA not in {j.section_sha for j in jobs}
        assert all(j.status == "queued" for j in jobs)
        assert all(j.commit_sha == head for j in jobs)

    # Coverage reached — idempotent.
    assert (
        reconcile_analysis_coverage(session_factory=pg_session_factory, workspaces_dir=tmp_path)
        == 0
    )


def test_done_is_covered_but_failed_is_reenqueued(pg_session_factory, tmp_path):
    """DELIBERATE SEMANTIC (spec §3): covered = queued/running/done only. A
    DONE job keeps its section out of the tick; a FAILED (dead-lettered) job
    does NOT — its content gets a fresh row with a fresh attempt budget."""
    sha_done, sha_failed = _live_shas()
    with pg_session_factory() as s:
        ws = _ws(s)
        repo = _init_repo(tmp_path, ws.id)
        head = current_head(repo)
        _seed_job(s, ws.id, section_sha=sha_done, status="done", commit_sha=head)
        _seed_job(s, ws.id, section_sha=sha_failed, status="failed", commit_sha=head)
        s.commit()
        ws_id = ws.id

    n = reconcile_analysis_coverage(session_factory=pg_session_factory, workspaces_dir=tmp_path)
    assert n == 1

    with pg_session_factory() as s:
        assert _queued_shas(s, ws_id) == {sha_failed}


def test_soft_deleted_and_missing_repo_are_skipped(pg_session_factory, tmp_path):
    """A soft-deleted workspace gets no jobs even though its repo has live
    sections; a workspace with no repo at all doesn't raise."""
    with pg_session_factory() as s:
        deleted = _ws(s, email="deleted@example.org")
        _init_repo(tmp_path, deleted.id)
        deleted.soft_deleted_at = datetime.now(UTC)
        repoless = _ws(s, email="repoless@example.org")  # NOTE: no _init_repo
        s.commit()
        deleted_id, repoless_id = deleted.id, repoless.id

    n = reconcile_analysis_coverage(session_factory=pg_session_factory, workspaces_dir=tmp_path)
    assert n == 0

    with pg_session_factory() as s:
        assert (
            s.query(ClaimAnalysisJob)
            .filter(ClaimAnalysisJob.workspace_id.in_([deleted_id, repoless_id]))
            .count()
            == 0
        )
