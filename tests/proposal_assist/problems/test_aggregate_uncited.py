from pathlib import Path

from call_manager.db.models import UncitedClaimsJob, WorkspaceEvent
from call_manager.proposal_assist.problems.aggregate import assemble_problems
from tests.proposal_assist.conftest import make_workspace


def _repo(tmp_path, ws_id, body="## Aims\n\nWe claim 30% relapse occurs.\n"):
    repo = Path(tmp_path) / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proposal.md").write_text(body)
    return repo


def test_fifth_signal_composes_and_stale_is_independent(tmp_path, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        # completed pass whose evaluated_draft_sha will NOT match the current draft -> stale
        s.add(UncitedClaimsJob(
            workspace_id=ws.id, status="completed", evaluated_draft_sha="stale-sha",
            claims=[{"claim_text": "30% relapse", "section_index": 1, "reason": "stat",
                     "claim_hash": "h1"}],
        ))
        s.commit()
        ws_id, rubric_ts = ws.id, ws.rubric_updated_at
    repo = _repo(tmp_path, ws_id)
    with pg_session_factory() as s:
        out = assemble_problems(
            session=s, workspace_id=ws_id, repo_dir=repo,
            ws_rubric_updated_at=rubric_ts, budget=0,
        )
    uncited = [r for r in out["problems"] if r["category"] == "uncited_claim"]
    assert len(uncited) == 1 and uncited[0]["claim_hash"] == "h1"
    assert out["uncited_claims_stale"] is True
    assert out["scorecard_stale"] == []  # independent of the uncited flag


def test_dismissed_claim_not_shown(tmp_path, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(UncitedClaimsJob(
            workspace_id=ws.id, status="completed", evaluated_draft_sha="x",
            claims=[{"claim_text": "30% relapse", "section_index": 1, "reason": "r",
                     "claim_hash": "h1"}],
        ))
        s.add(WorkspaceEvent(workspace_id=ws.id, kind="uncited_claim_dismissed",
                             payload={"claim_hash": "h1"}))
        s.commit()
        ws_id, rubric_ts = ws.id, ws.rubric_updated_at
    repo = _repo(tmp_path, ws_id)
    with pg_session_factory() as s:
        out = assemble_problems(
            session=s, workspace_id=ws_id, repo_dir=repo,
            ws_rubric_updated_at=rubric_ts, budget=0,
        )
    assert [r for r in out["problems"] if r["category"] == "uncited_claim"] == []
