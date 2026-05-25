"""Two-phase ingest:
Phase 1 — write bytes atomically + insert Reference row + commit.
Phase 2 — sync Stage 1 (extract + chunk + embed) in a separate tx.
          On failure, text_status='failed', row + bytes retained.
"""

import uuid

from sqlalchemy import select

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.references.embed import FakeEmbedder
from call_manager.proposal_assist.references.ingest import (
    IngestResult,
    ingest_bytes,
)
from call_manager.proposal_assist.references.storage import ReferenceStorage
from tests.proposal_assist.conftest import make_user, make_workspace


def test_ingest_text_markdown_creates_row_and_chunks(pg_session_factory, tmp_path):
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    md = b"# Title\n\nIntro.\n\n## Methods\n\nMethod text.\n"

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
            data=md,
            uploaded_by=uid,
        )
        s.commit()

    assert isinstance(result, IngestResult)
    assert result.text_status == "done"

    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == result.reference_id))
        chunks = list(
            s.scalars(
                select(ReferenceChunk).where(ReferenceChunk.reference_id == result.reference_id)
            )
        )

    assert ref.text_status == "done"
    assert ref.text_indexed_at is not None
    assert ref.text_chars_total > 0
    assert len(chunks) >= 2  # at least Title and Methods sections
    section_paths = [c.section_path for c in chunks]
    assert any(p and "Methods" in p for p in section_paths)
    # File on disk
    p = tmp_path / str(wsid) / "references" / str(ref.id) / "original.md"
    assert p.read_bytes() == md
    text_p = tmp_path / str(wsid) / "references" / str(ref.id) / "text.txt"
    assert text_p.exists()


def test_ingest_extraction_failure_persists_row_with_failed_status(pg_session_factory, tmp_path):
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid, uid = ws.id, user.id

    # Send "PDF" bytes that aren't actually a PDF — extraction will fail.
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
            filename="bad.pdf",
            mime_type="application/pdf",
            data=b"not a real pdf",
            uploaded_by=uid,
        )
        s.commit()

    assert result.text_status == "failed"

    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == result.reference_id))
        chunks = list(
            s.scalars(
                select(ReferenceChunk).where(ReferenceChunk.reference_id == result.reference_id)
            )
        )

    # Row + file PERSIST (retain-on-failure model — Codex High #1 rev4 resolution).
    assert ref is not None
    assert ref.text_status == "failed"
    assert ref.text_error is not None
    assert "pdf" in ref.text_error.lower()
    p = tmp_path / str(wsid) / "references" / str(ref.id) / "original.pdf"
    assert p.exists()
    # No chunks inserted.
    assert chunks == []


def test_ingest_data_csv_uses_skipped_status(pg_session_factory, tmp_path):
    """kind='data' rows are still text-indexed in v1 (CSV/XLSX produce
    schema+sample text), so text_status goes to 'done' but outline
    stays 'skipped' per spec."""
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    csv = b"name,age\nalice,30\nbob,25\n"

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
            kind="data",
            source_type="uploaded",
            source_uri=None,
            filename="d.csv",
            mime_type="text/csv",
            data=csv,
            uploaded_by=uid,
        )
        s.commit()

    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == result.reference_id))
    # For data kind we still text-index (so summarize_data has something);
    # outline stays skipped (was already skipped at insert; v1.1 may change).
    assert ref.text_status == "done"
    assert ref.outline_status == "skipped"
