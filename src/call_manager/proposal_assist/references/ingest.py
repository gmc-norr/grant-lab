"""Three-phase reference ingest.

Phase 1 (atomic, B7 pattern): validate, write_atomic, insert Reference
row, commit. On any failure here → no row, no file (caller raises and
returns 400/500).

Phase 2 (sync Stage 1, retain-on-failure): extract text, chunk, embed,
INSERT chunks, UPDATE text_status='done'. On Stage 1 failure → UPDATE
text_status='failed' + text_error, leave row + bytes on disk for retry.

Phase 3 (optional, non-fatal): bibliographic metadata extraction in its
own session after Phase 2. Runs a heuristic pass (PDF /Info) and/or an
LLM pass via CitationLLMService, then writes csl_json_extracted, title,
citekey, metadata_status, and metadata_source. Skipped entirely when
both source_meta and citation_service are absent (metadata_status stays
'pending'). Any failure in Phase 3 is caught, logged, and
metadata_status is set to 'failed' — Phase 1+2 result is always
returned regardless.

The three phases use SEPARATE transactions because Stage 1 and Stage 2
can take several seconds and we don't want to hold a tx open for that
long.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.references.chunker import chunk_sections
from call_manager.proposal_assist.references.csl import generate_citekey
from call_manager.proposal_assist.references.embed import Embedder
from call_manager.proposal_assist.references.extract import (
    ExtractError,
    extract_text,
)
from call_manager.proposal_assist.references.metadata import extract_reference_metadata
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
    source_meta: dict | None = None,
    citation_service=None,
) -> IngestResult:
    """Run all phases. Caller is responsible for committing the session
    after Phase 1 (we commit Phase 2 and Phase 3 internally on fresh sessions).

    Phase 3 (metadata extraction + citekey) is skipped entirely when both
    source_meta is falsy and citation_service is None, leaving
    metadata_status='pending'. Phase 3 failure is non-fatal: Phase 1+2
    result is always returned."""

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
        original_sha256 = hashlib.sha256(data).hexdigest()
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
            original_sha256=original_sha256,
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
        ingest_result = IngestResult(reference_id=reference_id, text_status="done", text_error=None)
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
        ingest_result = IngestResult(
            reference_id=reference_id, text_status="failed", text_error=msg
        )

    # Phase 3: extract bibliographic metadata + assign citekey.
    # Skip entirely when there is nothing to extract from (preserves prior
    # behaviour and avoids a pointless DB round-trip for callers that supply
    # neither source_meta nor citation_service).
    if source_meta or citation_service is not None:
        try:
            with session_factory() as s3:
                ref3 = s3.get(Reference, reference_id)
                if ref3 is None:
                    # Extremely unlikely — Phase 1 succeeded; log and skip.
                    logger.warning(
                        "ingest Phase 3: ref not found workspace=%s reference=%s",
                        workspace_id,
                        reference_id,
                    )
                else:
                    # Best-effort read of extracted text (written by Phase 2).
                    extracted_text: str | None = None
                    if ref3.text_status == "done":
                        try:
                            text_path = storage.text_path_for(
                                workspace_id=workspace_id,
                                reference_id=reference_id,
                            )
                            extracted_text = text_path.read_text(encoding="utf-8")
                        except Exception:
                            logger.warning(
                                "ingest Phase 3: could not read text.txt for "
                                "workspace=%s reference=%s",
                                workspace_id,
                                reference_id,
                            )

                    csl, source, status = extract_reference_metadata(
                        source_type=ref3.source_type,
                        source_meta=source_meta,
                        mime_type=ref3.mime_type,
                        data=data,
                        text=extracted_text,
                        workspace_id=workspace_id,
                        citation_service=citation_service,
                    )

                    if csl:
                        ref3.csl_json_extracted = csl
                        title_val = csl.get("title")
                        if isinstance(title_val, str) and title_val:
                            ref3.title = title_val
                        ref3.metadata_source = source
                        ref3.metadata_status = status
                        ref3.citekey = generate_citekey(csl, workspace_id, s3)
                    else:
                        ref3.metadata_status = "failed"
                        # Still assign a fallback citekey so a metadata-less reference can
                        # be cited downstream (generate_citekey({}) → "ref-<12hex>").
                        if ref3.citekey is None:
                            ref3.citekey = generate_citekey({}, workspace_id, s3)
                    s3.commit()
        except Exception:
            logger.warning(
                "ingest Phase 3 failed for workspace=%s reference=%s — "
                "metadata extraction error (Phase 1+2 result preserved)",
                workspace_id,
                reference_id,
                exc_info=True,
            )
            # Best-effort: mark metadata_status='failed' so the row reflects
            # the outcome without masking the Phase 1+2 success.
            try:
                with session_factory() as s3:
                    ref3 = s3.get(Reference, reference_id)
                    if ref3 is not None:
                        ref3.metadata_status = "failed"
                        s3.commit()
            except Exception:
                logger.warning(
                    "ingest Phase 3 fallback status update also failed workspace=%s reference=%s",
                    workspace_id,
                    reference_id,
                    exc_info=True,
                )

    return ingest_result
