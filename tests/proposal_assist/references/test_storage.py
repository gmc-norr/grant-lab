"""ReferenceStorage mirrors AttachmentStorage from B7: per-workspace
filesystem layout, atomic writes, idempotent deletes."""

import os
import uuid

import pytest

from grant_lab.proposal_assist.references.storage import ReferenceStorage


def test_path_for_shape(tmp_path):
    storage = ReferenceStorage(root=tmp_path)
    wsid = uuid.uuid4()
    rid = uuid.uuid4()
    p = storage.path_for(workspace_id=wsid, reference_id=rid, suffix=".pdf")
    assert p == tmp_path / str(wsid) / "references" / str(rid) / "original.pdf"


def test_write_atomic_creates_directories_and_persists_bytes(tmp_path):
    storage = ReferenceStorage(root=tmp_path)
    wsid = uuid.uuid4()
    rid = uuid.uuid4()
    storage.write_atomic(workspace_id=wsid, reference_id=rid, suffix=".pdf", data=b"PDF bytes")
    p = tmp_path / str(wsid) / "references" / str(rid) / "original.pdf"
    assert p.exists()
    assert p.read_bytes() == b"PDF bytes"
    # No leftover .partial file.
    assert not any(child.suffix == ".partial" for child in p.parent.iterdir())


def test_write_atomic_cleans_partial_on_failure(tmp_path, monkeypatch):
    storage = ReferenceStorage(root=tmp_path)
    wsid = uuid.uuid4()
    rid = uuid.uuid4()

    def boom(src, dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        storage.write_atomic(workspace_id=wsid, reference_id=rid, suffix=".pdf", data=b"x")
    target_dir = tmp_path / str(wsid) / "references" / str(rid)
    if target_dir.exists():
        assert list(target_dir.iterdir()) == []


def test_write_text_persists_text_txt(tmp_path):
    storage = ReferenceStorage(root=tmp_path)
    wsid = uuid.uuid4()
    rid = uuid.uuid4()
    storage.path_for(workspace_id=wsid, reference_id=rid, suffix=".pdf").parent.mkdir(parents=True)
    storage.write_text(workspace_id=wsid, reference_id=rid, text="hello world")
    p = tmp_path / str(wsid) / "references" / str(rid) / "text.txt"
    assert p.exists()
    assert p.read_text() == "hello world"


def test_delete_one_is_idempotent(tmp_path):
    storage = ReferenceStorage(root=tmp_path)
    wsid = uuid.uuid4()
    rid = uuid.uuid4()
    storage.write_atomic(workspace_id=wsid, reference_id=rid, suffix=".pdf", data=b"x")
    storage.delete_one(workspace_id=wsid, reference_id=rid)
    assert not (tmp_path / str(wsid) / "references" / str(rid)).exists()
    # Calling again must not raise.
    storage.delete_one(workspace_id=wsid, reference_id=rid)


def test_delete_workspace_removes_references_tree(tmp_path):
    storage = ReferenceStorage(root=tmp_path)
    wsid = uuid.uuid4()
    rid = uuid.uuid4()
    storage.write_atomic(workspace_id=wsid, reference_id=rid, suffix=".pdf", data=b"orig")
    storage.path_for(workspace_id=wsid, reference_id=rid, suffix=".pdf")  # ensure dir
    storage.write_text(workspace_id=wsid, reference_id=rid, text="extracted")
    refs_dir = tmp_path / str(wsid) / "references"
    assert refs_dir.exists()
    storage.delete_workspace(workspace_id=wsid)
    assert not refs_dir.exists()
    # Idempotent — second call must not raise.
    storage.delete_workspace(workspace_id=wsid)


def test_delete_workspace_leaves_sibling_dirs(tmp_path):
    """Only the references/ subdir is removed; sibling repo/ and
    attachments/ trees are untouched."""
    storage = ReferenceStorage(root=tmp_path)
    wsid = uuid.uuid4()
    rid = uuid.uuid4()
    storage.write_atomic(workspace_id=wsid, reference_id=rid, suffix=".pdf", data=b"orig")
    sibling = tmp_path / str(wsid) / "repo"
    sibling.mkdir(parents=True)
    (sibling / "keep.txt").write_text("keep")
    storage.delete_workspace(workspace_id=wsid)
    assert not (tmp_path / str(wsid) / "references").exists()
    assert (sibling / "keep.txt").exists()


def test_path_for_rejects_disallowed_suffix(tmp_path):
    storage = ReferenceStorage(root=tmp_path)
    with pytest.raises(ValueError, match="disallowed suffix"):
        storage.path_for(
            workspace_id=uuid.uuid4(),
            reference_id=uuid.uuid4(),
            suffix="../../etc/passwd",
        )
