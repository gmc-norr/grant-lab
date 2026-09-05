"""GET /api/workspaces/{id}/annotations — Claims-tab read-model."""

from __future__ import annotations

from grant_lab.db.models import ClaimAnalysisJob, UserClaimAnnotation
from grant_lab.proposal_assist.editing.sectionizer import split_sections
from tests.proposal_assist.conftest import make_workspace
from tests.test_analysis_pipeline import _init_repo, _seed_reference

DOC = "# T\nintro\n## Body\nThis is a factual claim.\n"
CLAIM = "This is a factual claim"
CHUNK_TEXT = "Background. verified words appear here. End."
QUOTE = "verified words"


def _seed(s, tmp_path, owner, *, kind="uncited_supported", with_ref=True):
    ws = make_workspace(s, owner=owner)
    _init_repo(tmp_path, ws.id, DOC)
    sec = split_sections(DOC)[1]
    ref_id = _seed_reference(s, ws.id, CHUNK_TEXT) if with_ref else None
    ann = UserClaimAnnotation(
        workspace_id=ws.id,
        section_sha=sec.sha,
        section_occurrence=0,
        section_heading=sec.heading,
        sentence_ordinal=0,
        char_offset=sec.text.index(CLAIM),
        claim_sha="0" * 64,
        claim_text=CLAIM,
        kind=kind,
        evidence_id="",
        reference_id=ref_id,
        chunk_index=0 if with_ref else None,
        evidence_quote=QUOTE if with_ref else None,
        status="open",
    )
    s.add(ann)
    s.flush()
    return ws, sec, ann.id


def test_read_model_returns_section_with_finding(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, sec, ann_id = _seed(s, tmp_path, current_user)
        s.commit()
        ws_id, sha = ws.id, sec.sha
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/annotations")
    assert r.status_code == 200
    body = r.json()
    assert body["is_owner"] is True
    # Two live sections (preamble + Body); the finding sits on section 1.
    body_section = next(x for x in body["sections"] if x["index"] == 1)
    assert body_section["section_sha"] == sha
    assert body_section["occurrence"] == 0
    assert len(body_section["findings"]) == 1
    f = body_section["findings"][0]
    assert f["id"] == ann_id
    assert f["kind"] == "uncited_supported"
    assert f["claim_text"] == CLAIM
    assert f["can_insert"] is True
    assert f["ref_label"]  # a non-empty "Family Year"/citekey label


def test_read_model_no_proposal_is_empty_200(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    # A workspace with NO repo / proposal.md — the tab must degrade, not 404/500.
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/annotations")
    assert r.status_code == 200
    assert r.json() == {"is_owner": True, "sections": []}


def test_read_model_omits_stale_and_non_open(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, sec, ann_id = _seed(s, tmp_path, current_user, kind="uncited_no_match", with_ref=False)
        # A stale annotation whose sha is NOT at HEAD must be omitted.
        s.add(
            UserClaimAnnotation(
                workspace_id=ws.id,
                section_sha="d" * 64,  # not a live section
                section_occurrence=0,
                section_heading="ghost",
                sentence_ordinal=0,
                char_offset=0,
                claim_sha="2" * 64,
                claim_text="ghost claim",
                kind="uncited_no_match",
                evidence_id="",
                status="open",
            )
        )
        # A dismissed annotation on the live section must be omitted (status != open).
        s.add(
            UserClaimAnnotation(
                workspace_id=ws.id,
                section_sha=sec.sha,
                section_occurrence=0,
                section_heading=sec.heading,
                sentence_ordinal=5,
                char_offset=0,
                claim_sha="3" * 64,
                claim_text="already handled",
                kind="uncited_no_match",
                evidence_id="",
                status="dismissed",
            )
        )
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/annotations")
    assert r.status_code == 200
    claims = [f["claim_text"] for sec in r.json()["sections"] for f in sec["findings"]]
    assert claims == [CLAIM]  # only the one live, open finding


def test_read_model_reports_pending_job(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, sec, _ = _seed(s, tmp_path, current_user, kind="uncited_no_match", with_ref=False)
        s.add(
            ClaimAnalysisJob(
                workspace_id=ws.id,
                section_index=1,
                section_heading=sec.heading,
                section_sha=sec.sha,
                commit_sha="0" * 40,
                status="queued",
            )
        )
        s.commit()
        ws_id, sha = ws.id, sec.sha
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/annotations")
    body_section = next(x for x in r.json()["sections"] if x["section_sha"] == sha)
    assert body_section["job"]["status"] == "pending"
