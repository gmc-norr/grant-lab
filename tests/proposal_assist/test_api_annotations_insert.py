"""POST /api/workspaces/{id}/annotations/{ann}/insert — owner-only citation insert."""

from __future__ import annotations

from grant_lab.db.models import ClaimAnalysisJob, UserClaimAnnotation
from grant_lab.proposal_assist.editing.sectionizer import split_sections
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.test_analysis_pipeline import _init_repo, _seed_reference

DOC = "# T\nintro\n## Body\nThis is a factual claim.\n"
CLAIM = "This is a factual claim"
CHUNK_TEXT = "Background. verified words appear here. End."
QUOTE = "verified words"


def _seed_supported(s, tmp_path, owner):
    ws = make_workspace(s, owner=owner)
    repo = _init_repo(tmp_path, ws.id, DOC)
    sec = split_sections(DOC)[1]
    ref_id = _seed_reference(s, ws.id, CHUNK_TEXT)
    ann = UserClaimAnnotation(
        workspace_id=ws.id,
        section_sha=sec.sha,
        section_occurrence=0,
        section_heading=sec.heading,
        sentence_ordinal=0,
        char_offset=sec.text.index(CLAIM),
        claim_sha="0" * 64,
        claim_text=CLAIM,
        kind="uncited_supported",
        evidence_id="",
        reference_id=ref_id,
        chunk_index=0,
        evidence_quote=QUOTE,
        status="open",
    )
    s.add(ann)
    s.flush()
    return ws, ann.id, repo


def test_owner_insert_writes_marker_and_reenqueues(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, ann_id, repo = _seed_supported(s, tmp_path, current_user)
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/annotations/{ann_id}/insert")
    assert r.status_code == 200
    body = r.json()
    assert body["ev_id"].startswith("ev-")
    assert body["citekey"]
    assert body["commit_sha"]
    assert body["idempotent"] is False
    doc = (repo / "proposal.md").read_text()
    assert "<!-- ev:ev-" in doc
    with pg_session_factory() as s:
        assert s.get(UserClaimAnnotation, ann_id).status == "inserted"
        # post-insert re-enqueue: a queued job for the now-changed section exists.
        job = s.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id, status="queued").first()
        assert job is not None


def test_insert_already_inserted_is_idempotent(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, ann_id, _ = _seed_supported(s, tmp_path, current_user)
        s.get(UserClaimAnnotation, ann_id).status = "inserted"  # already done
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/annotations/{ann_id}/insert")
    assert r.status_code == 200
    assert r.json()["idempotent"] is True


def test_insert_non_supported_kind_409(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    # A no_match annotation is not actionable -> insert_from_annotation raises
    # InsertConflict -> 409.
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, ann_id, _ = _seed_supported(s, tmp_path, current_user)
        s.get(UserClaimAnnotation, ann_id).kind = "uncited_no_match"
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/annotations/{ann_id}/insert")
    assert r.status_code == 409


def test_insert_non_owner_403(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger-ins@umu.se")
        ws, ann_id, _ = _seed_supported(s, tmp_path, stranger)
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/annotations/{ann_id}/insert")
    assert r.status_code == 403


def test_insert_cross_workspace_annotation_404(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    # An annotation id that belongs to workspace B cannot be inserted via A.
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws_a = make_workspace(s, owner=current_user)
        _init_repo(tmp_path, ws_a.id, DOC)
        ws_b, ann_b, _ = _seed_supported(s, tmp_path, current_user)
        s.commit()
        ws_a_id, ann_b_id = ws_a.id, ann_b
    r = client_authed_owner.post(f"/api/workspaces/{ws_a_id}/annotations/{ann_b_id}/insert")
    assert r.status_code == 404
