"""Per-reference metadata-waiver route (Slice 4 Task 7).

POST /workspaces/{id}/references/{ref_id}/waiver lets the OWNER override the
fail-closed metadata gate for one reference so build_bibliography / composed
export can cite it without full approval. The change is audited via a
WorkspaceEvent. Viewers cannot waive.
"""

import pytest
from fastapi.testclient import TestClient

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import (
    Reference,
    User,
    Workspace,
    WorkspaceEvent,
    WorkspaceViewer,
)
from call_manager.main import create_app
from call_manager.proposal_assist.citations.bibliography import (
    BuildBibliographyBlocked,
    build_bibliography,
)
from call_manager.proposal_assist.citations.markers import build_marker

_SMITH_CSL = {
    "author": [{"family": "Smith", "given": "Jane"}],
    "title": "Adjuvant therapy outcomes",
    "issued": {"date-parts": [[2021]]},
}


def _build_app(monkeypatch, pg_url, tmp_path):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))
    return create_app()


def _authed(app, *, user_id):
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=user_id))
    return client


def _seed(session, *, email, metadata_status="pending"):
    owner = User(status="active", email=email)
    session.add(owner)
    session.flush()
    ws = Workspace(owner_id=owner.id, title="T")
    session.add(ws)
    session.flush()
    ref = Reference(
        workspace_id=ws.id,
        kind="paper",
        source_type="uploaded",
        filename="seed.pdf",
        storage_path="seed/seed.pdf",
        mime_type="application/pdf",
        citekey="smith2021",
        csl_json=_SMITH_CSL,
        metadata_status=metadata_status,
    )
    session.add(ref)
    session.flush()
    oid, wsid, rid = owner.id, ws.id, ref.id
    session.commit()
    return oid, wsid, rid


def _ledger(rid):
    return {
        "version": 2,
        "evidence": [{"id": "ev-aaaaaaaaaaaa", "citekey": "smith2021", "reference_id": str(rid)}],
    }


def _proposal():
    return f"# P\n\nWe argue {build_marker('smith2021', 'ev-aaaaaaaaaaaa')} here.\n"


def test_owner_waiver_persists_audits_and_unblocks(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid, rid = _seed(s, email="waive-ok@ki.se")

    # Pre-condition: unapproved ref blocks the bibliography.
    with pg_session_factory() as s, pytest.raises(BuildBibliographyBlocked):
        build_bibliography(
            proposal_text=_proposal(),
            ledger=_ledger(rid),
            session=s,
            workspace_id=wsid,
            style="vancouver",
            repo_dir=tmp_path,
            commit=False,
        )

    client = _authed(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references/{rid}/waiver",
            data={"waiver": "true", "reason": "trusted preprint"},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    assert "metadata waived" in resp.text
    assert "trusted preprint" in resp.text

    # Persisted + audited.
    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        assert ref.metadata_waiver is True
        assert ref.metadata_waiver_reason == "trusted preprint"
        events = s.query(WorkspaceEvent).filter(WorkspaceEvent.workspace_id == wsid).all()
        assert any(e.kind == "reference_metadata_waived" for e in events)

    # The bibliography now builds (waiver overrides the gate).
    with pg_session_factory() as s:
        result = build_bibliography(
            proposal_text=_proposal(),
            ledger=_ledger(rid),
            session=s,
            workspace_id=wsid,
            style="vancouver",
            repo_dir=tmp_path,
            commit=False,
        )
    assert result["citekey_count"] == 1


def test_clearing_waiver_reblocks(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid, rid = _seed(s, email="waive-clear@ki.se")
        # Start already waived.
        ref = s.get(Reference, rid)
        ref.metadata_waiver = True
        ref.metadata_waiver_reason = "temp"
        s.commit()

    client = _authed(app, user_id=uid)
    try:
        resp = client.post(f"/workspaces/{wsid}/references/{rid}/waiver", data={"waiver": "false"})
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text

    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        assert ref.metadata_waiver is False
        assert ref.metadata_waiver_reason is None
        events = s.query(WorkspaceEvent).filter(WorkspaceEvent.workspace_id == wsid).all()
        assert any(e.kind == "reference_metadata_waiver_cleared" for e in events)

    # Re-blocked.
    with pg_session_factory() as s, pytest.raises(BuildBibliographyBlocked):
        build_bibliography(
            proposal_text=_proposal(),
            ledger=_ledger(rid),
            session=s,
            workspace_id=wsid,
            style="vancouver",
            repo_dir=tmp_path,
            commit=False,
        )


def test_clear_with_reason_still_nulls_reason(monkeypatch, pg_url, pg_session_factory, tmp_path):
    # Posting waiver=false must null the reason even if a reason field is sent.
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid, rid = _seed(s, email="waive-clear-reason@ki.se")
        ref = s.get(Reference, rid)
        ref.metadata_waiver = True
        ref.metadata_waiver_reason = "old"
        s.commit()

    client = _authed(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references/{rid}/waiver",
            data={"waiver": "false", "reason": "ignored on clear"},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        assert ref.metadata_waiver is False
        assert ref.metadata_waiver_reason is None


def test_non_member_cannot_waive(monkeypatch, pg_url, pg_session_factory, tmp_path):
    # An authenticated user who is neither owner nor viewer → 403.
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        _, wsid, rid = _seed(s, email="waive-nonmember-owner@ki.se")
        other = User(status="active", email="waive-nonmember-other@ki.se")
        s.add(other)
        s.commit()
        other_id = other.id
    client = _authed(app, user_id=other_id)
    try:
        resp = client.post(f"/workspaces/{wsid}/references/{rid}/waiver", data={"waiver": "true"})
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403
    with pg_session_factory() as s:
        assert s.get(Reference, rid).metadata_waiver is False


def test_viewer_cannot_waive(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        _, wsid, rid = _seed(s, email="waive-viewer-owner@ki.se")
        viewer = User(status="active", email="waive-viewer@ki.se")
        s.add(viewer)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=wsid, user_id=viewer.id))
        s.commit()
        viewer_id = viewer.id

    client = _authed(app, user_id=viewer_id)
    try:
        resp = client.post(f"/workspaces/{wsid}/references/{rid}/waiver", data={"waiver": "true"})
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403
    with pg_session_factory() as s:
        assert s.get(Reference, rid).metadata_waiver is False


def test_waiver_other_workspace_reference_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid, _ = _seed(s, email="waive-404-a@ki.se")
        _, other_wsid, other_rid = _seed(s, email="waive-404-b@ki.se")
    client = _authed(app, user_id=uid)
    try:
        # Owner of wsid tries to waive a reference belonging to other_wsid.
        resp = client.post(
            f"/workspaces/{wsid}/references/{other_rid}/waiver", data={"waiver": "true"}
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 404
