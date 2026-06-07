"""Tests for delete_or_tombstone_reference (Slice 2 Task 8).

An UNCITED reference hard-deletes (row + chunks + stored bytes). A CITED
reference (any evidence row in citations.json referencing it) is tombstoned:
the row stays with tombstoned_at set, chunks/bytes retained, so existing
citations keep resolving.
"""

import json
import uuid

import pytest

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.references.deletion import delete_or_tombstone_reference
from call_manager.proposal_assist.references.storage import ReferenceStorage
from tests.proposal_assist.conftest import make_user, make_workspace

EMBED_DIM = 1024


def _seed_reference(pg_session_factory, workspace_id, storage, tmp_path):
    """Seed a Reference + one chunk + stored original bytes. Returns rid."""
    with pg_session_factory() as s:
        ref = Reference(
            workspace_id=workspace_id,
            kind="paper",
            source_type="uploaded",
            filename="seed.pdf",
            storage_path="seed/seed.pdf",
            mime_type="application/pdf",
            metadata_status="approved",
        )
        s.add(ref)
        s.flush()
        rid = ref.id
        s.add(
            ReferenceChunk(
                reference_id=rid,
                workspace_id=workspace_id,
                chunk_index=0,
                section_path="Results",
                text="some chunk text",
                embedding=[0.0] * EMBED_DIM,
                embedding_model="fake-embedder",
            )
        )
        s.commit()
    # Write stored bytes so we can assert hard-delete removes them.
    storage.write_atomic(
        workspace_id=workspace_id,
        reference_id=rid,
        suffix=".pdf",
        data=b"original bytes",
    )
    return rid


def _make_ws(pg_session_factory):
    with pg_session_factory() as s:
        user = make_user(s, email=f"owner-{uuid.uuid4()}@ki.se")
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        return ws.id


def _write_ledger(repo_dir, rows):
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "citations.json").write_text(
        json.dumps({"version": 2, "evidence": rows}), encoding="utf-8"
    )


def test_uncited_reference_is_hard_deleted(pg_session_factory, tmp_path):
    wsid = _make_ws(pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, storage, tmp_path)
    repo_dir = tmp_path / str(wsid) / "repo"
    # No citations.json (or empty ledger) → uncited.
    byte_path = storage.path_for(workspace_id=wsid, reference_id=rid, suffix=".pdf")
    assert byte_path.exists()

    with pg_session_factory() as s:
        outcome = delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=rid,
            workspace_id=wsid,
            storage=storage,
        )
    assert outcome == "deleted"

    with pg_session_factory() as s:
        assert s.get(Reference, rid) is None
        remaining = s.query(ReferenceChunk).filter(ReferenceChunk.reference_id == rid).count()
        assert remaining == 0
    # Stored bytes removed too.
    assert not byte_path.exists()


def test_cited_reference_is_tombstoned_not_deleted(pg_session_factory, tmp_path):
    wsid = _make_ws(pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, storage, tmp_path)
    repo_dir = tmp_path / str(wsid) / "repo"
    _write_ledger(repo_dir, [{"id": "ev-1", "reference_id": str(rid)}])
    byte_path = storage.path_for(workspace_id=wsid, reference_id=rid, suffix=".pdf")

    with pg_session_factory() as s:
        outcome = delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=rid,
            workspace_id=wsid,
            storage=storage,
        )
    assert outcome == "tombstoned"

    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        assert ref is not None
        assert ref.tombstoned_at is not None
        # Chunks retained so existing citations keep resolving.
        remaining = s.query(ReferenceChunk).filter(ReferenceChunk.reference_id == rid).count()
        assert remaining == 1
    # Stored bytes retained.
    assert byte_path.exists()


def test_unknown_reference_raises(pg_session_factory, tmp_path):
    wsid = _make_ws(pg_session_factory)
    repo_dir = tmp_path / str(wsid) / "repo"
    with pg_session_factory() as s, pytest.raises(ValueError):
        delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=uuid.uuid4(),
            workspace_id=wsid,
        )


def test_cross_workspace_reference_raises(pg_session_factory, tmp_path):
    wsid_a = _make_ws(pg_session_factory)
    wsid_b = _make_ws(pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    rid = _seed_reference(pg_session_factory, wsid_a, storage, tmp_path)
    repo_dir = tmp_path / str(wsid_b) / "repo"
    with pg_session_factory() as s, pytest.raises(ValueError):
        delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=rid,
            workspace_id=wsid_b,
            storage=storage,
        )


def test_unreadable_ledger_fails_closed_to_tombstone(pg_session_factory, tmp_path):
    """An UNREADABLE citations.json (OSError — e.g. a directory in place of the
    file) must be treated as CITED (fail-closed).

    This mirrors the corrupt-ledger test: when we cannot read the ledger we
    cannot confirm the reference is uncited, so we must NOT hard-delete it.
    The reference must be tombstoned, with its row, chunk(s), and bytes all
    retained.
    """
    wsid = _make_ws(pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, storage, tmp_path)
    repo_dir = tmp_path / str(wsid) / "repo"
    byte_path = storage.path_for(workspace_id=wsid, reference_id=rid, suffix=".pdf")

    # Place a DIRECTORY where citations.json would be so .exists() is True but
    # read_text() raises IsADirectoryError (a subclass of OSError).
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "citations.json").mkdir()

    with pg_session_factory() as s:
        outcome = delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=rid,
            workspace_id=wsid,
            storage=storage,
        )

    # Fail-closed: unreadable ledger is treated as cited → tombstone.
    assert outcome == "tombstoned"

    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        # Row must still exist with tombstoned_at set.
        assert ref is not None
        assert ref.tombstoned_at is not None
        # Chunk(s) retained so existing citations keep resolving.
        remaining = s.query(ReferenceChunk).filter(ReferenceChunk.reference_id == rid).count()
        assert remaining == 1
    # Stored bytes retained.
    assert byte_path.exists()


def test_corrupt_ledger_fails_closed_to_tombstone(pg_session_factory, tmp_path):
    """A MALFORMED citations.json must be treated as CITED (fail-closed).

    This is the data-loss-sensitive branch: if the ledger is corrupt we cannot
    confirm the reference is uncited, so we must NOT hard-delete it. The
    reference must be tombstoned, with its row, chunk(s), and bytes all
    retained.
    """
    wsid = _make_ws(pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, storage, tmp_path)
    repo_dir = tmp_path / str(wsid) / "repo"
    byte_path = storage.path_for(workspace_id=wsid, reference_id=rid, suffix=".pdf")

    # Write a malformed (non-parseable) citations.json.
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "citations.json").write_text("{not valid json", encoding="utf-8")

    with pg_session_factory() as s:
        outcome = delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=rid,
            workspace_id=wsid,
            storage=storage,
        )

    # Fail-closed: corrupt ledger is treated as cited → tombstone.
    assert outcome == "tombstoned"

    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        # Row must still exist with tombstoned_at set.
        assert ref is not None
        assert ref.tombstoned_at is not None
        # Chunk(s) retained so existing citations keep resolving.
        remaining = s.query(ReferenceChunk).filter(ReferenceChunk.reference_id == rid).count()
        assert remaining == 1
    # Stored bytes retained.
    assert byte_path.exists()
