"""ingest_bytes bounds Phase-2 extraction by a configurable wall-clock timeout
and maps a timeout to text_status='failed' (the untrusted-file DoS guard)."""

from __future__ import annotations

from sqlalchemy import select

from grant_lab.db.models import Reference
from grant_lab.proposal_assist.references import ingest as ingest_mod
from grant_lab.proposal_assist.references.embed import FakeEmbedder
from grant_lab.proposal_assist.references.extract import ExtractError
from grant_lab.proposal_assist.references.ingest import ingest_bytes
from grant_lab.proposal_assist.references.storage import ReferenceStorage
from tests.proposal_assist.conftest import make_user, make_workspace


def test_ingest_forwards_timeout_and_marks_failed_on_timeout(
    pg_session_factory, tmp_path, monkeypatch
):
    """ingest_bytes must call the *bounded* extractor with the timeout it was
    given, and a raised ExtractError must land as text_status='failed' (row +
    bytes retained), not crash the request."""
    captured: dict[str, float] = {}

    def _spy_bounded(*, mime, data, timeout_seconds):
        captured["timeout_seconds"] = timeout_seconds
        raise ExtractError("simulated slow-file timeout")

    # Patch the name as ingest.py imported it.
    monkeypatch.setattr(ingest_mod, "extract_text_bounded", _spy_bounded)

    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid, uid = ws.id, user.id

    with pg_session_factory() as s:
        result = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=embedder,
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            filename="paper.md",
            mime_type="text/markdown",
            data=b"# Title\n\nbody",
            uploaded_by=uid,
            extract_timeout_seconds=7.0,
        )
        s.commit()

    assert captured["timeout_seconds"] == 7.0
    assert result.text_status == "failed"
    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == result.reference_id))
    assert ref is not None  # row retained
    assert ref.text_status == "failed"
