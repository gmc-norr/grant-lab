"""POST /api/workspaces/{id}/sections/{n}/reanalyze — owner-only re-enqueue."""

from __future__ import annotations

import re

from call_manager.db.models import ClaimAnalysisJob
from call_manager.proposal_assist.editing.sectionizer import split_sections
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.test_analysis_pipeline import _init_repo

DOC = "# T\nintro\n## Body\nThis is a factual claim.\n"


def test_owner_reanalyze_enqueues(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        _init_repo(tmp_path, ws.id, DOC)
        s.commit()
        ws_id = ws.id
    sha = split_sections(DOC)[1].sha
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/sections/1/reanalyze")
    assert r.status_code == 200
    assert r.json()["enqueued"] is True
    with pg_session_factory() as s:
        job = s.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id).one()
        assert job.status == "queued"
        assert job.section_sha == sha
        assert re.fullmatch(r"[0-9a-f]{40}", job.commit_sha)  # real SHA, not text


def test_reanalyze_idempotent_dedup(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    # A second reanalyze of the SAME content dedups (one queued job per sha).
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        _init_repo(tmp_path, ws.id, DOC)
        s.commit()
        ws_id = ws.id
    r1 = client_authed_owner.post(f"/api/workspaces/{ws_id}/sections/1/reanalyze")
    r2 = client_authed_owner.post(f"/api/workspaces/{ws_id}/sections/1/reanalyze")
    assert r1.json()["enqueued"] is True
    assert r2.json()["enqueued"] is False  # deduped
    with pg_session_factory() as s:
        assert s.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id, status="queued").count() == 1


def test_reanalyze_out_of_range_404(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        _init_repo(tmp_path, ws.id, DOC)
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/sections/99/reanalyze")
    assert r.status_code == 404


def test_reanalyze_non_owner_403(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger-re@umu.se")
        ws = make_workspace(s, owner=stranger)
        _init_repo(tmp_path, ws.id, DOC)
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/sections/1/reanalyze")
    assert r.status_code == 403
