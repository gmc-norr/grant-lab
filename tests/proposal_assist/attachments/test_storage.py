"""Storage helper: write attachment bytes under
var/workspaces/<workspace_uuid>/attachments/<attachment_uuid>.<ext>
and resolve the path back from an attachment_id.

Atomicity is the load-bearing property: write goes to a `.partial` temp
file + fsync + os.replace so concurrent readers see either no file or
the complete file. Failure cases (rename failure, traversal in suffix,
double-delete) are explicit tests because the upload/delete handlers
depend on these guarantees.
"""

from __future__ import annotations

import os
import uuid

import pytest

from call_manager.proposal_assist.attachments.storage import (
    AttachmentStorage,
    attachment_path,
)


def test_attachment_path_shape(tmp_path):
    storage = AttachmentStorage(root=tmp_path)
    wsid = uuid.uuid4()
    aid = uuid.uuid4()
    p = storage.path_for(workspace_id=wsid, attachment_id=aid, suffix=".png")
    assert p == tmp_path / str(wsid) / "attachments" / f"{aid}.png"


def test_attachment_path_module_helper(tmp_path):
    """The module-level attachment_path() is equivalent to path_for()."""
    wsid = uuid.uuid4()
    aid = uuid.uuid4()
    p = attachment_path(
        root=tmp_path,
        workspace_id=wsid,
        attachment_id=aid,
        suffix=".jpg",
    )
    assert p == tmp_path / str(wsid) / "attachments" / f"{aid}.jpg"


def test_path_for_normalises_suffix_case(tmp_path):
    """Suffix is lowercased so ".PNG" still resolves to the .png file."""
    storage = AttachmentStorage(root=tmp_path)
    wsid = uuid.uuid4()
    aid = uuid.uuid4()
    p = storage.path_for(workspace_id=wsid, attachment_id=aid, suffix=".PNG")
    assert p.name == f"{aid}.png"


def test_write_atomic_creates_directories_and_persists_bytes(tmp_path):
    storage = AttachmentStorage(root=tmp_path)
    wsid = uuid.uuid4()
    aid = uuid.uuid4()
    storage.write_atomic(workspace_id=wsid, attachment_id=aid, suffix=".png", data=b"hello")
    p = tmp_path / str(wsid) / "attachments" / f"{aid}.png"
    assert p.exists()
    assert p.read_bytes() == b"hello"
    # No leftover .partial file in the directory.
    assert not any(child.suffix == ".partial" for child in p.parent.iterdir())


def test_write_atomic_cleans_partial_on_failure(tmp_path, monkeypatch):
    storage = AttachmentStorage(root=tmp_path)
    wsid = uuid.uuid4()
    aid = uuid.uuid4()

    # Force os.replace to fail to simulate a mid-write failure.
    def boom(src, dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        storage.write_atomic(workspace_id=wsid, attachment_id=aid, suffix=".png", data=b"x")
    # Neither the .partial nor the final file remain.
    target_dir = tmp_path / str(wsid) / "attachments"
    if target_dir.exists():
        assert list(target_dir.iterdir()) == []


def test_delete_one_is_idempotent(tmp_path):
    storage = AttachmentStorage(root=tmp_path)
    wsid = uuid.uuid4()
    aid = uuid.uuid4()
    storage.write_atomic(workspace_id=wsid, attachment_id=aid, suffix=".png", data=b"x")
    storage.delete_one(workspace_id=wsid, attachment_id=aid, suffix=".png")
    assert not (tmp_path / str(wsid) / "attachments" / f"{aid}.png").exists()
    # Calling again must not raise.
    storage.delete_one(workspace_id=wsid, attachment_id=aid, suffix=".png")


def test_delete_workspace_removes_directory(tmp_path):
    storage = AttachmentStorage(root=tmp_path)
    wsid = uuid.uuid4()
    storage.write_atomic(workspace_id=wsid, attachment_id=uuid.uuid4(), suffix=".png", data=b"x")
    assert (tmp_path / str(wsid) / "attachments").exists()
    storage.delete_workspace(workspace_id=wsid)
    # Only the attachments subdir is removed; the workspace dir may
    # contain git repo (B5). Verify only attachments is gone.
    assert not (tmp_path / str(wsid) / "attachments").exists()


def test_delete_workspace_leaves_sibling_dirs_alone(tmp_path):
    """If the workspace dir contains a B5 repo/ subdir alongside attachments/,
    delete_workspace must NOT nuke the repo. Only `attachments/` is ours."""
    storage = AttachmentStorage(root=tmp_path)
    wsid = uuid.uuid4()
    storage.write_atomic(workspace_id=wsid, attachment_id=uuid.uuid4(), suffix=".png", data=b"x")
    # Simulate B5's git repo dir co-living under the same workspace dir.
    repo_dir = tmp_path / str(wsid) / "repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / "proposal.md").write_text("kept")

    storage.delete_workspace(workspace_id=wsid)

    assert not (tmp_path / str(wsid) / "attachments").exists()
    assert (repo_dir / "proposal.md").read_text() == "kept"


def test_delete_workspace_is_idempotent(tmp_path):
    storage = AttachmentStorage(root=tmp_path)
    wsid = uuid.uuid4()
    # Never wrote anything for this workspace.
    storage.delete_workspace(workspace_id=wsid)  # MUST NOT raise


def test_path_for_rejects_traversal_in_suffix(tmp_path):
    storage = AttachmentStorage(root=tmp_path)
    with pytest.raises(ValueError):
        storage.path_for(
            workspace_id=uuid.uuid4(),
            attachment_id=uuid.uuid4(),
            suffix="../../etc/passwd",
        )


def test_path_for_rejects_unknown_suffix(tmp_path):
    """The allowlist is image-only; a .bmp suffix is rejected up-front so
    the upload handler can't accidentally write a non-image type even if
    the MIME-allowlist check upstream is bypassed."""
    storage = AttachmentStorage(root=tmp_path)
    with pytest.raises(ValueError):
        storage.path_for(
            workspace_id=uuid.uuid4(),
            attachment_id=uuid.uuid4(),
            suffix=".bmp",
        )
