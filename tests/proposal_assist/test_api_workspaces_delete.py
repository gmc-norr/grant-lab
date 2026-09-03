"""DELETE /api/workspaces/{id} — owner-only soft-delete (cockpit gap-closer)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from call_manager.db.models import (
    ChatTurn,
    CoherenceJob,
    TurnJob,
    Workspace,
    WorkspaceEvent,
    WorkspaceViewer,
)
from call_manager.proposal_assist.attachments.storage import AttachmentStorage
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.references.storage import ReferenceStorage
from tests.proposal_assist.conftest import make_user, make_workspace


def _seed_workspace_with_bytes(
    pg_session_factory,
    owner,
    tmp_path: Path,
    *,
    attachment: bytes,
    reference: bytes,
    proposal: str,
) -> uuid.UUID:
    """Create a workspace owned by `owner` and place one real attachment, one
    real reference and a proposal document at the paths production actually
    uses — via AttachmentStorage/ReferenceStorage, not paths we invented."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=owner)
        s.commit()
        ws_id = ws.id

    AttachmentStorage(root=tmp_path).write_atomic(
        workspace_id=ws_id,
        attachment_id=uuid.uuid4(),
        suffix=".png",
        data=attachment,
    )
    ReferenceStorage(root=tmp_path).write_atomic(
        workspace_id=ws_id,
        reference_id=uuid.uuid4(),
        suffix=".txt",
        data=reference,
    )
    repo = workspace_repo_dir(tmp_path, ws_id)
    repo.mkdir(parents=True)
    (repo / "proposal.md").write_text(proposal, encoding="utf-8")

    return ws_id


def _attachment_path(tmp_path: Path, ws_id: uuid.UUID) -> Path:
    return next((tmp_path / str(ws_id) / "attachments").glob("*"))


def _reference_path(tmp_path: Path, ws_id: uuid.UUID) -> Path:
    return next((tmp_path / str(ws_id) / "references").glob("*/original*"))


def _proposal_path(tmp_path: Path, ws_id: uuid.UUID) -> Path:
    return workspace_repo_dir(tmp_path, ws_id) / "proposal.md"


def test_delete_keeps_every_byte_on_disk(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """Slice A: deletion FREEZES a workspace. Attachment, reference and repository
    bytes must all survive byte-identical, so a later restore is lossless.

    Contents, not existence: a scrub that emptied a directory without removing it
    would satisfy an exists() check while destroying exactly what restore needs.
    """
    _app.state.settings.workspaces_dir = str(tmp_path)
    ws_id = _seed_workspace_with_bytes(
        pg_session_factory,
        current_user,
        tmp_path,
        attachment=b"ATTACHMENT SENTINEL",
        reference=b"REFERENCE SENTINEL",
        proposal="# PROPOSAL SENTINEL\n",
    )

    attach = _attachment_path(tmp_path, ws_id)
    ref = _reference_path(tmp_path, ws_id)
    repo_doc = _proposal_path(tmp_path, ws_id)
    before = (attach.read_bytes(), ref.read_bytes(), repo_doc.read_text(encoding="utf-8"))

    r = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
    assert r.status_code == 204

    assert attach.read_bytes() == before[0], "attachment bytes were destroyed"
    assert ref.read_bytes() == before[1], "reference bytes were destroyed"
    assert repo_doc.read_text(encoding="utf-8") == before[2], "proposal was destroyed"


def test_owner_delete_204_cascades_and_keeps_bytes(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        s.add(CoherenceJob(workspace_id=ws.id, status="queued"))
        ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(ut)
        s.flush()
        s.add(
            TurnJob(
                workspace_id=ws.id,
                user_turn_id=ut.id,
                submitted_by_user_id=current_user.id,
                status="queued",
                queued_at=datetime.now(UTC),
            )
        )
        s.commit()
        ws_id = ws.id

    attach_dir = tmp_path / str(ws_id) / "attachments"
    attach_dir.mkdir(parents=True)
    (attach_dir / "blob.bin").write_bytes(b"x")

    r = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
    assert r.status_code == 204

    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is not None
        assert (
            s.scalar(select(CoherenceJob.status).where(CoherenceJob.workspace_id == ws_id))
            == "failed"
        )
        # Carry forward the emit_chat=False guarantee (G2 deletes the Jinja-route test):
        assert (
            s.scalar(select(TurnJob.status).where(TurnJob.workspace_id == ws_id)) == "cancelled"
        )
        assert (
            s.scalar(
                select(WorkspaceEvent.id).where(WorkspaceEvent.kind == "cloud_turn_cancelled")
            )
            is not None
        )
        assert s.scalar(select(ChatTurn).where(ChatTurn.role == "system")) is None
    assert attach_dir.exists(), (
        "Slice A: deleting a workspace FREEZES it and destroys nothing, so a "
        "later restore is lossless. Only an admin purge removes bytes."
    )


def test_non_owner_delete_403(client_authed_owner, current_user, pg_session_factory):
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger@umu.se")
        ws = make_workspace(s, owner=stranger)  # current_user has no relationship to it
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
    assert r.status_code == 403
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is None  # untouched


def test_viewer_delete_403(client_authed_owner, current_user, pg_session_factory):
    # A workspace member (viewer) is still not the owner → 403 (spec §4.1: viewer AND stranger).
    with pg_session_factory() as s:
        stranger = make_user(s, email="viewer-owner@umu.se")
        ws = make_workspace(s, owner=stranger)
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
    assert r.status_code == 403
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is None  # untouched


def test_unknown_workspace_delete_404(client_authed_owner, current_user, pg_session_factory):
    # require_owner returns 404 for a workspace that does not exist (spec §4.1).
    assert client_authed_owner.delete(f"/api/workspaces/{uuid.uuid4()}").status_code == 404


def test_redelete_is_404(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id
    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204
    # require_owner filters soft_deleted_at IS NULL → second delete is 404 (not idempotent).
    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 404
