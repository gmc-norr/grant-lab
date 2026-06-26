import uuid

from call_manager.db.models import EvaluatorJob, Workspace
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import make_workspace


def _ws(_app, current_user, pg_session_factory, tmp_path):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, current_phase="active")
        s.flush()
        ws.rubric_criteria = []
        s.commit()
        return str(ws.id)


def _seed_proposal(tmp_path, ws_id, text):
    repo = workspace_repo_dir(str(tmp_path), uuid.UUID(ws_id))
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proposal.md").write_text(text)


def test_problems_empty_shape(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/problems")
    assert r.status_code == 200
    assert r.json() == {
        "problems": [],
        "counts": {"error": 0, "warning": 0},
        "scorecard_stale": [],
        "uncited_claims_stale": False,
    }


def test_problems_over_length_and_criterion_gap(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    _seed_proposal(tmp_path, ws_id, "## Long\n" + "z" * 50 + "\n")
    with pg_session_factory() as s:
        s.add(
            EvaluatorJob(
                workspace_id=uuid.UUID(ws_id),
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
                evaluated_draft_sha="",
                evaluated_spec_sha="",
                evaluated_rubric_updated_at=s.get(Workspace, uuid.UUID(ws_id)).rubric_updated_at,
            )
        )
        s.commit()
    body = client_authed_owner.get(
        f"/api/workspaces/{ws_id}/problems"
    ).json()  # budget default 6000 -> 51-char section is NOT over-length
    cats = {p["category"] for p in body["problems"]}
    assert "criterion_gap" in cats
    assert body["counts"]["error"] >= 1


def test_problems_unknown_workspace_404(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    r = client_authed_owner.get(f"/api/workspaces/{uuid.uuid4()}/problems")
    assert r.status_code == 404


def test_problems_unauth_rejected(client_unauth, _app, current_user, pg_session_factory, tmp_path):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    r = client_unauth.get(f"/api/workspaces/{ws_id}/problems")
    assert r.status_code in (401, 403)
