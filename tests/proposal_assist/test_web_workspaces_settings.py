import pytest


def test_get_settings_renders_for_owner(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.title = "Original"
        s.commit()
        wsid = ws.id
    response = client_authed_owner.get(f"/workspaces/{wsid}/settings")
    assert response.status_code == 200
    assert b"Original" in response.content
    assert b"<form" in response.content


def test_get_settings_rejects_non_owner(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        owner = make_user(s, email="o@ki.se")
        ws = make_workspace(s, owner=owner)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        wsid = ws.id
    response = client_authed_user.get(f"/workspaces/{wsid}/settings")
    assert response.status_code == 403


def test_post_settings_updates_title_status_deadline(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from sqlalchemy import select

    from call_manager.db.models import Workspace
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/settings",
        data={
            "title": "Updated title",
            "status": "submitted",
            "deadline": "2026-12-31",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"] == f"/workspaces/{wsid}"

    with pg_session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.id == wsid))
    assert ws.title == "Updated title"
    assert ws.status == "submitted"
    assert ws.deadline is not None
    assert ws.deadline.year == 2026


def test_post_settings_rejects_invalid_status(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """An out-of-allowlist status returns a clean 400, not a DB-constraint 500."""
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/settings",
        data={"title": "ok", "status": "not-a-real-status"},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_post_settings_rejects_blank_title(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/settings",
        data={"title": "   ", "status": "drafting"},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_soft_delete_redirects_and_excludes_from_list(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from sqlalchemy import select

    from call_manager.db.models import Workspace
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.title = "ToDelete"
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/delete",
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"] == "/workspaces"

    with pg_session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.id == wsid))
    assert ws.soft_deleted_at is not None

    # Verify list page no longer shows it.
    list_resp = client_authed_owner.get("/workspaces")
    assert b"ToDelete" not in list_resp.content


def test_post_settings_rejects_non_owner(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        owner = make_user(s, email="o@ki.se")
        ws = make_workspace(s, owner=owner)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        wsid = ws.id
    response = client_authed_user.post(
        f"/workspaces/{wsid}/settings",
        data={"title": "x", "status": "drafting"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# B7 Task 5: soft-delete scrubs attachment bytes from disk.
#
# Contract: DB commit happens FIRST (durable "deleted" state) and FS
# cleanup is best-effort. Attachment ROWS are preserved for the audit
# trail; only the bytes go. An FS failure logs WARNING but does not
# fail the request — orphans are harmless because the row-source-of-
# truth gate (`_load_workspace_or_404`) refuses to serve a soft-deleted
# workspace.
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolated_workspaces_dir_for_settings(client_authed_owner, tmp_path):
    """Point the live app at a fresh tmp_path so file writes don't leak
    into the repo's var/ tree. Mirrors the fixture in
    test_web_workspaces_files.py but scoped to the soft-delete tests."""
    client_authed_owner.app.state.settings.workspaces_dir = str(tmp_path)
    return tmp_path


def _seed_attachment_with_file(
    pg_session_factory,
    *,
    workspace_id,
    owner_id,
    root,
    data: bytes = b"\x89PNG\r\n\x1a\n-fake-",
):
    """Seed one Attachment row + write its bytes to disk via the storage
    helper. Returns the attachment's on-disk path."""
    import uuid as _uuid
    from pathlib import Path as _Path

    from call_manager.db.models import Attachment
    from call_manager.proposal_assist.attachments.storage import AttachmentStorage

    aid = _uuid.uuid4()
    storage = AttachmentStorage(root=_Path(root))
    file_path = storage.write_atomic(
        workspace_id=workspace_id,
        attachment_id=aid,
        suffix=".png",
        data=data,
    )
    with pg_session_factory() as s:
        att = Attachment(
            id=aid,
            workspace_id=workspace_id,
            kind="figure",
            filename="seed.png",
            storage_path=f"{aid}.png",
            mime_type="image/png",
            uploaded_by=owner_id,
        )
        s.add(att)
        s.commit()
    return file_path, aid


def test_soft_delete_scrubs_attachment_files_keeps_repo_dir(
    client_authed_owner,
    pg_session_factory,
    current_user,
    _isolated_workspaces_dir_for_settings,
):
    """Happy path: soft-delete removes the attachments/ subdir from disk
    while preserving Attachment DB rows (audit trail) and leaving any
    sibling B5 repo/ subdir untouched."""
    from pathlib import Path

    from sqlalchemy import select

    from call_manager.db.models import Attachment, Workspace
    from tests.proposal_assist.conftest import make_workspace

    tmp_root = _isolated_workspaces_dir_for_settings

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    file_path, aid = _seed_attachment_with_file(
        pg_session_factory,
        workspace_id=wsid,
        owner_id=current_user.id,
        root=tmp_root,
    )
    assert file_path.exists()

    # Simulate a B5 git repo sibling — must NOT be touched by soft-delete.
    repo_dir = Path(tmp_root) / str(wsid) / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    canary = repo_dir / "HEAD"
    canary.write_text("ref: refs/heads/main\n")

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/delete",
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    # DB: workspace soft-deleted, Attachment row preserved.
    with pg_session_factory() as s:
        ws_row = s.scalar(select(Workspace).where(Workspace.id == wsid))
        att_row = s.scalar(select(Attachment).where(Attachment.id == aid))
    assert ws_row.soft_deleted_at is not None
    assert att_row is not None, "Attachment row must be preserved for audit trail"

    # FS: file is gone, attachments/ subdir is gone, repo/ subdir untouched.
    assert not file_path.exists()
    assert not (Path(tmp_root) / str(wsid) / "attachments").exists()
    assert canary.exists()
    assert canary.read_text() == "ref: refs/heads/main\n"


def test_soft_delete_idempotent_on_fs_failure(
    client_authed_owner,
    pg_session_factory,
    current_user,
    _isolated_workspaces_dir_for_settings,
    monkeypatch,
    caplog,
):
    """If shutil.rmtree raises OSError during FS cleanup, the DB commit
    must still record soft_deleted_at (durable state) and the handler
    must log a WARNING. A subsequent soft-delete (with rmtree restored)
    must succeed idempotently and finally remove the file."""
    import logging
    import shutil
    import types

    from sqlalchemy import select

    from call_manager.db.models import Workspace
    from tests.proposal_assist.conftest import make_workspace

    tmp_root = _isolated_workspaces_dir_for_settings

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    file_path, _aid = _seed_attachment_with_file(
        pg_session_factory,
        workspace_id=wsid,
        owner_id=current_user.id,
        root=tmp_root,
    )
    assert file_path.exists()

    # Patch the storage module's shutil reference to a fake namespace so
    # only `storage.delete_workspace` sees the failing rmtree. Patching
    # `storage_mod.shutil.rmtree` directly would mutate the global shutil
    # module and break pytest's tmp_path cleanup at teardown.
    from call_manager.proposal_assist.attachments import storage as storage_mod

    def _boom(*_args, **_kwargs):
        raise OSError("simulated failure")

    fake_shutil = types.SimpleNamespace(rmtree=_boom)
    monkeypatch.setattr(storage_mod, "shutil", fake_shutil)

    caplog.set_level(logging.WARNING, logger="call_manager.proposal_assist.web.workspaces")
    response = client_authed_owner.post(
        f"/workspaces/{wsid}/delete",
        follow_redirects=False,
    )
    assert response.status_code in (302, 303), response.text

    # DB still records the soft-delete (commit happened BEFORE FS cleanup).
    with pg_session_factory() as s:
        ws_row = s.scalar(select(Workspace).where(Workspace.id == wsid))
    assert ws_row.soft_deleted_at is not None

    # WARNING was logged by the workspaces module.
    warnings = [
        rec
        for rec in caplog.records
        if rec.name == "call_manager.proposal_assist.web.workspaces"
        and rec.levelno == logging.WARNING
    ]
    assert warnings, f"expected WARNING log; got records: {caplog.records}"
    assert any("attachment cleanup failed" in rec.getMessage() for rec in warnings)

    # File is still on disk because rmtree was patched to fail.
    assert file_path.exists()

    # Restore the real shutil. The HTTP route can't be re-driven (the
    # workspace is already soft-deleted, so `_require_owner` returns 404),
    # but a future sweep job (or any direct re-invocation of
    # AttachmentStorage) MUST be idempotent and clean up the orphan file.
    # Drive that path directly — same semantics, same code, but bypasses
    # the auth gate the plan explicitly contemplates ("a future sweep
    # job will collect them").
    monkeypatch.setattr(storage_mod, "shutil", shutil)
    from call_manager.proposal_assist.attachments.storage import AttachmentStorage

    AttachmentStorage(root=tmp_root).delete_workspace(workspace_id=wsid)
    assert not file_path.exists()
    # Second call is a no-op (idempotent).
    AttachmentStorage(root=tmp_root).delete_workspace(workspace_id=wsid)


def test_soft_delete_with_no_attachments_is_noop(
    client_authed_owner,
    pg_session_factory,
    current_user,
    _isolated_workspaces_dir_for_settings,
):
    """A workspace with zero attachments soft-deletes cleanly — the FS
    cleanup is a no-op (no attachments/ subdir to remove)."""
    from sqlalchemy import select

    from call_manager.db.models import Workspace
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/delete",
        follow_redirects=False,
    )
    assert response.status_code in (302, 303), response.text

    with pg_session_factory() as s:
        ws_row = s.scalar(select(Workspace).where(Workspace.id == wsid))
    assert ws_row.soft_deleted_at is not None
