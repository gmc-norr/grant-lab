from call_manager.db.models import CoherenceJob, ProposedChange
from tests.proposal_assist.conftest import make_workspace


def _coherence_row(s, ws_id, job_id):
    # target_artifact="spec" + NO proposal-spec.md on disk: refresh_staleness skips
    # rows whose artifact file is absent (staleness.py), so these stay 'pending' and
    # the ONLY filter exercised is the §3.3 visibility gate — not staleness.
    pc = ProposedChange(
        workspace_id=ws_id,
        target_artifact="spec",
        section_index=0,
        base_commit_sha="x",
        base_section_sha="y",
        kind="prose_edit",
        payload={"new_text": "z"},
        origin_kind="coherence_session",
        origin_job_id=job_id,
        status="pending",
    )
    s.add(pc)
    s.flush()
    return pc


def test_running_coherence_rows_hidden_completed_shown(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    # No repo files written: with proposal.md AND proposal-spec.md absent,
    # refresh_staleness is a no-op (absent artifact -> skip), isolating the gate.
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        running = CoherenceJob(workspace_id=ws.id, status="running")
        completed = CoherenceJob(workspace_id=ws.id, status="completed")
        failed = CoherenceJob(workspace_id=ws.id, status="failed")
        s.add_all([running, completed, failed])
        s.flush()
        _coherence_row(s, ws.id, running.id)  # hidden (job running)
        _coherence_row(s, ws.id, completed.id)  # shown (job completed)
        _coherence_row(s, ws.id, failed.id)  # hidden (job failed, not completed)
        s.commit()
        ws_id = str(ws.id)

    resp = client_authed_owner.get(f"/api/workspaces/{ws_id}/proposed-changes?target_artifact=spec")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1  # only the completed-job row (running AND failed are both hidden)


def test_interactive_rows_always_shown(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        # an interactive (non-coherence) spec row is never gated by job status
        s.add(
            ProposedChange(
                workspace_id=ws.id,
                target_artifact="spec",
                section_index=0,
                base_commit_sha="x",
                base_section_sha="y",
                kind="prose_edit",
                payload={"new_text": "z"},
                origin_kind="interactive_turn",
                status="pending",
            )
        )
        s.commit()
        ws_id = str(ws.id)
    resp = client_authed_owner.get(f"/api/workspaces/{ws_id}/proposed-changes?target_artifact=spec")
    assert len(resp.json()["items"]) == 1
