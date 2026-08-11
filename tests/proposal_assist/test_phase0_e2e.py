"""End-to-end happy path for Phase 0:

  workspace created (B1 create_workspace seeds rubric defaults)
  → setup job queued
  → Phase 0 worker run_one() drains the job through all 6 steps
  → job yields at waiting_for_rubric_review (workspace.current_phase='setup')
  → owner POSTs rubric review
  → workspace advances to current_phase='active', job=done

LLM is mocked (AnthropicClient.chat returns canned section + rubric envelopes).
Git operations run for real against a tmp_path repo root.
"""

import json
from unittest.mock import Mock

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import (
    CallRubric,
    CallSectionExtract,
    Workspace,
    WorkspaceEvent,
    WorkspaceSetupJob,
)
from call_manager.main import create_app
from call_manager.proposal_assist.extractors.sections import SECTION_NAMES
from call_manager.proposal_assist.phases.setup import Phase0Worker
from tests.proposal_assist.conftest import (
    make_call,
    make_profile,
    make_user,
    make_workspace,
)


def _section_envelope() -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps({s: f"section text for {s}" for s in SECTION_NAMES}),
                }
            }
        ],
        "model": "claude-opus-4-7",
    }


def _rubric_envelope() -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "criteria": [
                                {
                                    "name": "Excellence",
                                    "description": "Scientific quality",
                                    "weight": 0.5,
                                    "scale_max": 5,
                                },
                                {
                                    "name": "Impact",
                                    "description": "Translational potential",
                                    "weight": 0.5,
                                    "scale_max": 5,
                                },
                            ],
                            "funder_notes": "See FAQ.",
                        }
                    ),
                }
            }
        ],
        "model": "claude-opus-4-7",
    }


def test_phase0_happy_path_workspace_to_spec(
    monkeypatch,
    pg_url,
    pg_session_factory,
    tmp_path,
):
    # === Step A: Set up the workspace + queued setup job in the DB ===
    with pg_session_factory() as s:
        owner = make_user(s, email="e2e-owner@ki.se")
        call = make_call(s)
        profile = make_profile(s, name="e2e-profile")
        s.commit()
        ws = make_workspace(
            s,
            owner=owner,
            call_id=call.id,
            profile_id=profile.id,
            current_phase="setup",
        )
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        uid = owner.id
        wsid = ws.id
        call_id = call.id

    # === Step B: Run the Phase 0 worker directly (skipping the scheduler) ===
    llm = Mock()
    llm.model = "claude-opus-4-7"
    # Two LLM calls expected: sections + rubric.
    llm.chat.side_effect = [_section_envelope(), _rubric_envelope()]

    worker = Phase0Worker(
        session_factory=pg_session_factory,
        client=llm,
        extractor_version="sections-v1",
        rubric_extractor_version="rubric-v1",
        workspaces_dir=str(tmp_path / "repos"),
    )
    processed = worker.run_one()
    assert processed is True

    # === Step C: Assert the post-Phase-0 state ===
    with pg_session_factory() as s:
        ws_post = s.get(Workspace, wsid)
        job = s.scalar(select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == wsid))
        assert job.status == "waiting_for_rubric_review"
        assert job.completed_at is None
        assert ws_post.current_phase == "setup"  # awaits owner confirmation

        # Call template tables populated (Slice A Part 1 extractors did this):
        assert (
            s.query(CallSectionExtract).filter(CallSectionExtract.call_id == call_id).count() == 6
        )
        assert s.get(CallRubric, call_id) is not None

        # Workspace snapshot columns populated:
        assert ws_post.call_body_snapshot != ""
        assert ws_post.profile_desc_snapshot != ""
        assert set(ws_post.section_extracts.keys()) == set(SECTION_NAMES)
        assert len(ws_post.rubric_criteria) == 2
        assert ws_post.rubric_funder_notes == "See FAQ."

        # rubric_initialized audit event written (Phase 0 step 5):
        init_events = s.scalars(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == wsid,
                WorkspaceEvent.kind == "rubric_initialized",
            )
        ).all()
        assert len(init_events) == 1

        # Git repo on disk:
        repo_dir = tmp_path / "repos" / str(wsid) / "repo"
        assert (repo_dir / "proposal-spec.md").exists()
        assert (repo_dir / ".git").is_dir()

    # === Step D: Owner activates the workspace via the JSON API endpoint ===
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    try:
        signer: SessionSigner = app.state.session_signer
        client.cookies.set("session", signer.make(user_id=uid))
        resp = client.post(
            f"/api/workspaces/{wsid}/activate",
            json={
                "criteria": [
                    {
                        "name": "Excellence",
                        "description": "Scientific quality",
                        "weight": 0.5,
                        "scale_max": 5,
                    },
                    {
                        "name": "Impact",
                        "description": "Translational potential",
                        "weight": 0.5,
                        "scale_max": 5,
                    },
                ],
                "funder_notes": "See FAQ.",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["criteria"][0]["name"] == "Excellence"
    finally:
        client.__exit__(None, None, None)

    # === Step E: Assert the final state ===
    with pg_session_factory() as s:
        ws_final = s.get(Workspace, wsid)
        job_final = s.scalar(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == wsid)
        )
        assert ws_final.current_phase == "active"
        assert job_final.status == "done"
        assert job_final.completed_at is not None
        # No rubric_edited event because the owner posted the same content
        # the extractor produced — content didn't change.
        edits = s.scalars(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == wsid,
                WorkspaceEvent.kind == "rubric_edited",
            )
        ).all()
        assert len(edits) == 0
