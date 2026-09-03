"""POST /api/workspaces/{id}/annotations/{ann}/dismiss — owner-only audited dismiss."""

from __future__ import annotations

from sqlalchemy import select

from call_manager.db.models import UserClaimAnnotation, WorkspaceEvent
from tests.proposal_assist.conftest import make_user, make_workspace


def _seed_open(s, owner):
    ws = make_workspace(s, owner=owner)
    ann = UserClaimAnnotation(
        workspace_id=ws.id,
        section_sha="a" * 64,
        section_occurrence=0,
        section_heading="Body",
        sentence_ordinal=0,
        char_offset=0,
        claim_sha="0" * 64,
        claim_text="a claim",
        kind="uncited_no_match",
        evidence_id="",
        status="open",
    )
    s.add(ann)
    s.flush()
    return ws, ann.id


def test_owner_dismiss_with_reason_audits(
    client_authed_owner, current_user, pg_session_factory
):
    with pg_session_factory() as s:
        ws, ann_id = _seed_open(s, current_user)
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(
        f"/api/workspaces/{ws_id}/annotations/{ann_id}/dismiss",
        json={"reason": "cited in the cover letter"},
    )
    assert r.status_code == 200
    assert r.json()["dismissed"] is True
    with pg_session_factory() as s:
        ann = s.get(UserClaimAnnotation, ann_id)
        assert ann.status == "dismissed"
        assert ann.dismissed_reason == "cited in the cover letter"
        assert ann.resolved_by_user_id == current_user.id
        ev = s.scalar(
            select(WorkspaceEvent).where(WorkspaceEvent.kind == "user_claim_dismissed")
        )
        assert ev.payload == {"annotation_id": ann_id, "reason": "cited in the cover letter"}


def test_dismiss_empty_reason_422(client_authed_owner, current_user, pg_session_factory):
    with pg_session_factory() as s:
        ws, ann_id = _seed_open(s, current_user)
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(
        f"/api/workspaces/{ws_id}/annotations/{ann_id}/dismiss",
        json={"reason": "   "},
    )
    assert r.status_code == 422
    with pg_session_factory() as s:
        assert s.get(UserClaimAnnotation, ann_id).status == "open"  # untouched


def test_dismiss_already_resolved_409(client_authed_owner, current_user, pg_session_factory):
    with pg_session_factory() as s:
        ws, ann_id = _seed_open(s, current_user)
        s.get(UserClaimAnnotation, ann_id).status = "inserted"  # raced
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(
        f"/api/workspaces/{ws_id}/annotations/{ann_id}/dismiss",
        json={"reason": "not needed"},
    )
    assert r.status_code == 409


def test_dismiss_non_owner_403(client_authed_owner, current_user, pg_session_factory):
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger-dis@umu.se")
        ws, ann_id = _seed_open(s, stranger)
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(
        f"/api/workspaces/{ws_id}/annotations/{ann_id}/dismiss",
        json={"reason": "x"},
    )
    assert r.status_code == 403
    with pg_session_factory() as s:
        assert s.get(UserClaimAnnotation, ann_id).status == "open"
