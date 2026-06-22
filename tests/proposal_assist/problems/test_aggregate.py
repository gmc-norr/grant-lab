from call_manager.db.models import CoherenceJob, EvaluatorJob, ProposedChange
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.problems.aggregate import (
    assemble_problems,
    count_pending_coherence_proposals,
)
from tests.proposal_assist.conftest import make_workspace


def _seed_repo(workspaces_dir, ws_id, *, proposal="", citations=None):
    repo = workspace_repo_dir(workspaces_dir, ws_id)
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proposal.md").write_text(proposal)
    if citations is not None:
        (repo / "citations.json").write_text(citations)
    return repo


def test_assemble_empty_when_no_draft_no_scorecard(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wid = ws.id
        rubric_ts = ws.rubric_updated_at
        _seed_repo(str(tmp_path), wid)  # empty proposal.md, no ledger
        out = assemble_problems(
            session=s,
            workspace_id=wid,
            repo_dir=workspace_repo_dir(str(tmp_path), wid),
            ws_rubric_updated_at=rubric_ts,
            budget=6000,
        )
    assert out == {"problems": [], "counts": {"error": 0, "warning": 0}, "scorecard_stale": []}


def test_assemble_no_ledger_but_markers_surface_orphan_errors(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wid = ws.id
        # Canonical marker form: ev:ev-<hex 6+> (find_markers ignores anything else).
        _seed_repo(str(tmp_path), wid, proposal="## M\nSee [@x]<!-- ev:ev-aaaaaa --> here.\n")
        out = assemble_problems(
            session=s,
            workspace_id=wid,
            repo_dir=workspace_repo_dir(str(tmp_path), wid),
            ws_rubric_updated_at=ws.rubric_updated_at,
            budget=6000,
        )
    cats = [p["category"] for p in out["problems"]]
    assert "broken_citation" in cats  # orphan_marker, NOT hidden by the absent ledger
    assert out["counts"]["error"] >= 1


def test_assemble_corrupt_ledger_is_one_error_row(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wid = ws.id
        _seed_repo(str(tmp_path), wid, proposal="## M\nbody\n", citations="{not json")
        out = assemble_problems(
            session=s,
            workspace_id=wid,
            repo_dir=workspace_repo_dir(str(tmp_path), wid),
            ws_rubric_updated_at=ws.rubric_updated_at,
            budget=6000,
        )
    broken = [p for p in out["problems"] if p["category"] == "broken_citation"]
    assert len(broken) == 1
    assert broken[0]["title"] == "Citations ledger unreadable"


def test_assemble_combines_scorecard_overlength_and_coherence(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        wid = ws.id
        # over-length: a section beyond budget=10
        _seed_repo(str(tmp_path), wid, proposal="## Long\n" + "z" * 50 + "\n")
        # scorecard with a gap criterion; markers chosen so stale is False (draft sha
        # differs because the seeded proposal != "" -> "draft" appears in stale).
        s.add(
            EvaluatorJob(
                workspace_id=wid,
                status="completed",
                scorecard={
                    "criteria": [
                        {
                            "index": 0,
                            "name": "Impact",
                            "band": "gap",
                            "note": "weak",
                            "deep_link": None,
                            "weight": 1.0,
                            "effective_weight": 1.0,
                        }
                    ],
                    "overall_band": "gap",
                    "summary": "",
                },
                evaluated_draft_sha="",  # current draft sha != "" -> stale includes "draft"
                evaluated_spec_sha="",
                evaluated_rubric_updated_at=ws.rubric_updated_at,
            )
        )
        # one pending coherence proposal whose job is completed -> ONE drift roll-up
        job = CoherenceJob(workspace_id=wid, status="completed")
        s.add(job)
        s.flush()
        s.add(
            ProposedChange(
                workspace_id=wid,
                target_artifact="spec",
                section_index=0,
                base_commit_sha="x",
                base_section_sha="y",
                kind="prose_edit",
                payload={"new_text": "z"},
                origin_kind="coherence_session",
                origin_job_id=job.id,
                status="pending",
            )
        )
        s.commit()
        out = assemble_problems(
            session=s,
            workspace_id=wid,
            repo_dir=workspace_repo_dir(str(tmp_path), wid),
            ws_rubric_updated_at=ws.rubric_updated_at,
            budget=10,
        )
    cats = sorted({p["category"] for p in out["problems"]})
    assert cats == ["coherence_drift", "criterion_gap", "over_length"]
    # exactly ONE coherence roll-up, not one-per-proposal
    assert sum(1 for p in out["problems"] if p["category"] == "coherence_drift") == 1
    assert "draft" in out["scorecard_stale"]


def test_count_pending_coherence_excludes_incomplete_jobs(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        running = CoherenceJob(workspace_id=ws.id, status="running")
        s.add(running)
        s.flush()
        s.add(
            ProposedChange(
                workspace_id=ws.id,
                target_artifact="spec",
                section_index=0,
                base_commit_sha="x",
                base_section_sha="y",
                kind="prose_edit",
                payload={"new_text": "z"},
                origin_kind="coherence_session",
                origin_job_id=running.id,
                status="pending",
            )
        )
        s.commit()
        # the proposal's job is still running -> not yet visible -> count 0
        assert count_pending_coherence_proposals(s, ws.id) == 0
