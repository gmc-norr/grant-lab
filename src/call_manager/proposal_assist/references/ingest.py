"""Two-phase reference ingest.

Phase 1 (atomic, B7 pattern): validate, write_atomic, insert Reference
row, commit. On any failure here → no row, no file (caller raises and
returns 400/500).

Phase 2 (sync Stage 1, retain-on-failure): extract text, chunk, embed,
INSERT chunks, UPDATE text_status='done'. On Stage 1 failure → UPDATE
text_status='failed' + text_error, leave row + bytes on disk for retry.

The two phases use SEPARATE transactions because Stage 1 can take
several seconds and we don't want to hold a tx open for that long.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.references.chunker import chunk_sections
from call_manager.proposal_assist.references.embed import Embedder
from call_manager.proposal_assist.references.extract import (
    ExtractError,
    extract_text,
)
from call_manager.proposal_assist.references.storage import ReferenceStorage

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    reference_id: uuid.UUID
    text_status: str  # 'done' | 'failed'
    text_error: str | None


_MIME_TO_SUFFIX = {
    "application/pdf": ".pdf",
    "text/markdown": ".md",
    "text/plain": ".txt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/rtf": ".rtf",
    "text/csv": ".csv",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/x-ipynb+json": ".ipynb",
}


def suffix_for_mime(mime: str) -> str:
    """Public helper — web layer also needs this when sanitizing filenames."""
    if mime not in _MIME_TO_SUFFIX:
        raise ValueError(f"unsupported mime: {mime!r}")
    return _MIME_TO_SUFFIX[mime]


def ingest_bytes(
    *,
    session_factory,
    session: Session,
    storage: ReferenceStorage,
    embedder: Embedder,
    workspace_id: uuid.UUID,
    kind: str,
    source_type: str,
    source_uri: str | None,
    filename: str,
    mime_type: str,
    data: bytes,
    uploaded_by: int | None,
) -> IngestResult:
    """Run both phases. Caller is responsible for committing the session
    after Phase 1 (we commit Phase 2 internally on a fresh session)."""

    suffix = suffix_for_mime(mime_type)
    reference_id = uuid.uuid4()

    # Phase 1: write bytes, then insert row in the caller's tx.
    # On any post-write failure (INSERT, commit, etc.), unlink the bytes
    # to avoid orphan files (B7 attachments pattern; code-review residuals fix).
    storage.write_atomic(
        workspace_id=workspace_id,
        reference_id=reference_id,
        suffix=suffix,
        data=data,
    )
    try:
        relative_path = f"{workspace_id}/references/{reference_id}/original{suffix}"
        ref = Reference(
            id=reference_id,
            workspace_id=workspace_id,
            kind=kind,
            source_type=source_type,
            source_uri=source_uri,
            title=None,  # Slice 2 will derive from metadata; v1 leaves NULL
            filename=filename,
            storage_path=relative_path,
            mime_type=mime_type,
            bytes_total=len(data),
            text_status="pending",
            outline_status="skipped",  # v1; v1.1 changes to 'pending'
            uploaded_by=uploaded_by,
        )
        session.add(ref)
        session.commit()
        # Caller may call s.commit() again; it is a harmless no-op.
    except Exception:
        # Clean up the bytes we just wrote. The row either was never
        # inserted (pre-commit) or the commit failed (no row visible).
        # Either way the file is now an orphan; remove it.
        try:
            storage.delete_one(workspace_id=workspace_id, reference_id=reference_id)
        except Exception:
            # Don't mask the original failure if cleanup also fails.
            logger.exception(
                "ingest Phase 1 rollback: storage.delete_one failed for workspace=%s reference=%s",
                workspace_id,
                reference_id,
            )
        raise

    # Phase 2: run Stage 1 in a fresh session (row is now visible).
    with session_factory() as s2:
        ref2 = s2.get(Reference, reference_id)
        if ref2 is None:
            # Caller rolled back Phase 1 between flush and Phase 2.
            return IngestResult(
                reference_id=reference_id,
                text_status="failed",
                text_error="row not committed",
            )
        ref2.text_status = "running"
        s2.commit()

    try:
        sections = extract_text(mime=mime_type, data=data)
        chunks = chunk_sections(sections)
        if not chunks:
            raise ExtractError("no chunks produced from extracted text")

        # Persist text.txt for v1.1 PageIndex + reembed CLI.
        full_text = "\n\n".join(t for (_, t) in sections)
        storage.write_text(
            workspace_id=workspace_id,
            reference_id=reference_id,
            text=full_text,
        )

        # Embed + insert chunks in one tx.
        with session_factory() as s2:
            for i, (section_path, text) in enumerate(chunks):
                vec = embedder.embed(text)
                s2.add(
                    ReferenceChunk(
                        reference_id=reference_id,
                        workspace_id=workspace_id,
                        chunk_index=i,
                        section_path=section_path,
                        text=text,
                        embedding=vec,
                        embedding_model=embedder.model,
                    )
                )
            ref2 = s2.get(Reference, reference_id)
            ref2.text_status = "done"
            ref2.text_indexed_at = datetime.now(UTC)
            ref2.text_chars_total = len(full_text)
            s2.commit()
        return IngestResult(reference_id=reference_id, text_status="done", text_error=None)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        logger.exception(
            "ingest Phase 2 failed for workspace=%s reference=%s",
            workspace_id,
            reference_id,
        )
        with session_factory() as s2:
            ref2 = s2.get(Reference, reference_id)
            if ref2 is not None:
                ref2.text_status = "failed"
                ref2.text_error = msg
                s2.commit()
        return IngestResult(reference_id=reference_id, text_status="failed", text_error=msg)
