import hashlib
import uuid

import pytest

from call_manager.db.models import Reference
from call_manager.proposal_assist.references.embed import FakeEmbedder
from call_manager.proposal_assist.references.ingest import ReprocessConflict, reprocess_reference
from call_manager.proposal_assist.references.storage import ReferenceStorage
from tests.proposal_assist.conftest import make_workspace


def _insert_failed_ref(session, storage, *, workspace_id, data, mime="text/markdown"):
    rid = uuid.uuid4()
    storage.write_atomic(workspace_id=workspace_id, reference_id=rid, suffix=".md", data=data)
    ref = Reference(
        id=rid,
        workspace_id=workspace_id,
        kind="paper",
        source_type="uploaded",
        source_uri=None,
        filename="x.md",
        storage_path=f"{workspace_id}/references/{rid}/original.md",
        mime_type=mime,
        bytes_total=len(data),
        original_sha256=hashlib.sha256(data).hexdigest(),
        text_status="failed",
        text_error="boom",
        outline_status="skipped",
        uploaded_by=None,
    )
    session.add(ref)
    session.commit()
    return rid


def test_reprocess_flips_failed_to_done(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    storage = ReferenceStorage(root=tmp_path)
    with pg_session_factory() as s:
        rid = _insert_failed_ref(s, storage, workspace_id=wsid, data=b"# H\n\nreal body now")
    res = reprocess_reference(
        session_factory=pg_session_factory,
        storage=storage,
        embedder=FakeEmbedder(),
        reference_id=rid,
        workspace_id=wsid,
        citation_service=None,
        extract_timeout_seconds=30.0,
    )
    assert res.text_status == "done"
    with pg_session_factory() as s:
        assert s.get(Reference, rid).text_status == "done"


def test_reprocess_rejects_non_failed(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    storage = ReferenceStorage(root=tmp_path)
    with pg_session_factory() as s:
        rid = _insert_failed_ref(s, storage, workspace_id=wsid, data=b"# H\n\nbody")
        ref = s.get(Reference, rid)
        ref.text_status = "done"
        s.commit()
    with pytest.raises(ReprocessConflict):
        reprocess_reference(
            session_factory=pg_session_factory,
            storage=storage,
            embedder=FakeEmbedder(),
            reference_id=rid,
            workspace_id=wsid,
            citation_service=None,
            extract_timeout_seconds=30.0,
        )
