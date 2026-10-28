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

from sqlalchemy import update as _sql_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from grant_lab.db.models import Reference, ReferenceChunk
from grant_lab.embeddings.client import LLMError
from grant_lab.proposal_assist.analysis.invalidation import on_reference_text_ingested
from grant_lab.proposal_assist.references.chunker import chunk_sections
from grant_lab.proposal_assist.references.csl import generate_citekey
from grant_lab.proposal_assist.references.embed import Embedder
from grant_lab.proposal_assist.references.extract import (
    ExtractError,
)
from grant_lab.proposal_assist.references.extract_sandbox import extract_text_bounded
from grant_lab.proposal_assist.references.metadata import extract_reference_metadata
from grant_lab.proposal_assist.references.storage import ReferenceStorage

logger = logging.getLogger(__name__)

# PUBLIC text_error vocabulary. `text_error` is returned by the upload response
# and by GET /references to EVERY workspace member, and any logged-in user can
# create a workspace — so whatever lands here is effectively public inside the
# hospital. It used to be f"{type(e).__name__}: {e}", which review 2026-10-26 A7
# measured carrying the embedder's scheme, host and port:
#   "LLMError: ollama embed failed: Server error '500 …' for url
#    'http://127.0.0.1:38749/api/embed'"
# In production that is the real Ollama address. These three strings are stable,
# say which STAGE failed (which is the only part a member can act on: re-upload,
# or wait and retry), and carry no infrastructure. The exception itself still
# goes to the log via logger.exception below — dropping it everywhere would make
# the failure undebuggable, which is the opposite trade.
_TEXT_ERROR_EXTRACT = "Text extraction failed"
_TEXT_ERROR_EMBED = "Embedding service unavailable"
_TEXT_ERROR_OTHER = "Text indexing failed"


def _public_text_error(exc: BaseException) -> str:
    """Map a Phase-2 exception to its stable, member-visible message."""
    if isinstance(exc, LLMError):
        return _TEXT_ERROR_EMBED
    if isinstance(exc, ExtractError):
        return _TEXT_ERROR_EXTRACT
    return _TEXT_ERROR_OTHER


# Chunk size for reference embedding. Sized conservatively so chunks fit the
# context window of small embedding models (e.g. mxbai-embed-large ~512 tokens
# on Ollama 0.21, which 400s on ~1500-char dense chunks). The embed client's
# shrink-and-retry is the safety net; this keeps retries (and the content loss
# they cause) rare. NOTE: char-budgeted, not token-budgeted — a token-aware,
# model-derived budget is part of the planned references/ranking RAG revamp.
_REFERENCE_CHUNK_CHARS = 1000

# Cap on chunks embedded per reference (review 2026-09-02 §2.5(a)). Every chunk
# is one synchronous Ollama round-trip on the request thread inside an ingest
# slot, then one 1024-float row held in memory until a single INSERT. A 50 MB
# text/plain upload — an allowed MIME, under every byte cap — measured 58,255
# chunks; the same size as markdown, 3.7 million. 500 chunks × 1000 chars is
# ~500k characters, more than a 100-page PDF (which the extractor already caps),
# so no legitimate reference is cut; what is cut is announced in a visible
# `[truncated]` chunk exactly like the PDF page cap in extract.py.
_MAX_REFERENCE_CHUNKS = 500


def _cap_chunks(
    chunks: list[tuple[str | None, str]], *, limit: int
) -> list[tuple[str | None, str]]:
    """The first `limit` chunks, plus one `[truncated]` marker chunk when any
    were dropped. Unchanged (same list) at or under the limit."""
    if len(chunks) <= limit:
        return chunks
    marker = (
        "[truncated]",
        f"[Reference truncated: only the first {limit} of {len(chunks)} text chunks "
        "were indexed; the rest of the document is not included in this text.]",
    )
    return [*chunks[:limit], marker]


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
    # Fetch-only: fetch_pubmed_pmid returns PMC JATS as application/xml. Not in
    # the web upload allow-list, so this never accepts a user-uploaded XML.
    "application/xml": ".xml",
}


def suffix_for_mime(mime: str) -> str:
    """Public helper — web layer also needs this when sanitizing filenames."""
    if mime not in _MIME_TO_SUFFIX:
        raise ValueError(f"unsupported mime: {mime!r}")
    return _MIME_TO_SUFFIX[mime]


def run_postwrite_phases(
    *,
    session_factory,
    storage: ReferenceStorage,
    embedder: Embedder,
    reference_id: uuid.UUID,
    workspace_id: uuid.UUID,
    mime_type: str,
    data: bytes,
    source_meta: dict | None,
    citation_service,
    extract_timeout_seconds: float,
    auto_classify_kind: bool,
) -> IngestResult:
    """Run Phase 2 (extract+chunk+embed, retain-on-failure) + claim-analysis
    invalidation + Phase 3 (metadata/citekey/optional kind classification)
    against an ALREADY-INSERTED, COMMITTED Reference row whose bytes are
    already on disk. Used by both ingest_bytes (after Phase 1) and
    reprocess_reference (after claiming failed→running). Phase-2 failure is
    non-fatal (sets text_status='failed' and returns)."""
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
        sections = extract_text_bounded(
            mime=mime_type, data=data, timeout_seconds=extract_timeout_seconds
        )
        chunks = chunk_sections(sections, max_chunk_chars=_REFERENCE_CHUNK_CHARS)
        if not chunks:
            raise ExtractError("no chunks produced from extracted text")
        if len(chunks) > _MAX_REFERENCE_CHUNKS:
            logger.warning(
                "ingest: chunk cap hit — embedding %d of %d chunks (workspace=%s reference=%s)",
                _MAX_REFERENCE_CHUNKS,
                len(chunks),
                workspace_id,
                reference_id,
            )
            chunks = _cap_chunks(chunks, limit=_MAX_REFERENCE_CHUNKS)

        # Persist text.txt for v1.1 PageIndex + reembed CLI.
        full_text = "\n\n".join(t for (_, t) in sections)
        storage.write_text(
            workspace_id=workspace_id,
            reference_id=reference_id,
            text=full_text,
        )

        # Embed every chunk BEFORE opening a transaction: each embed is a
        # network round-trip to Ollama (seconds under load + the shrink-retry),
        # and holding a write tx open across them is idle-in-transaction that
        # pins a connection and can block autovacuum. Compute vectors first,
        # then INSERT + flip status in one short tx.
        #
        # Defect-1 fix: embed() returns EmbedResult(vector, embedded_chars).
        # When the shrink-and-retry path fires, embedded_chars < len(text) —
        # meaning the vector represents only a prefix of the chunk. We MUST
        # store that prefix as the chunk text; otherwise the DB row has a
        # vector for ~25-50% of the text but text_status='done' with the full
        # text visible, silently degrading RAG retrieval quality.
        def _embed_chunk(i: int, section_path: str, text: str):
            result = embedder.embed(text)
            n = result.embedded_chars
            stored_text = text if n >= len(text) else text[:n]
            if n < len(text):
                logger.warning(
                    "ingest: embed truncated chunk %d from %d to %d chars "
                    "(workspace=%s reference=%s) — storing truncated prefix",
                    i,
                    len(text),
                    result.embedded_chars,
                    workspace_id,
                    reference_id,
                )
            return i, section_path, stored_text, result.vector

        embedded = [
            _embed_chunk(i, section_path, text) for i, (section_path, text) in enumerate(chunks)
        ]
        with session_factory() as s2:
            for i, section_path, stored_text, vec in embedded:
                s2.add(
                    ReferenceChunk(
                        reference_id=reference_id,
                        workspace_id=workspace_id,
                        chunk_index=i,
                        section_path=section_path,
                        text=stored_text,
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
        msg = _public_text_error(e)
        # logger.exception keeps the full traceback AND the original message
        # (host, port, status line) server-side; only `msg` is persisted.
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

    if ingest_result.text_status == "done":
        # Library grew (spec 2026-06-10 §3): right after text_status flips to
        # 'done' this reference's chunks become searchable, so stale cached
        # no_match outcomes and re-enqueue sections with open no-match
        # findings. Fresh session, own commit — the Phase-2 'done' commit is
        # already durable. storage.root IS the workspaces_dir (ReferenceStorage
        # layout: <workspaces_dir>/<workspace_id>/references/...).
        try:
            with session_factory() as s2:
                on_reference_text_ingested(
                    s2, workspace_id=workspace_id, workspaces_dir=storage.root
                )
                s2.commit()
        except Exception:  # noqa: BLE001 — invalidation is best-effort
            logger.exception("claim-analysis invalidation failed (ingest)")

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

                    csl, source, status, classified_kind = extract_reference_metadata(
                        source_type=ref3.source_type,
                        source_meta=source_meta,
                        mime_type=ref3.mime_type,
                        data=data,
                        text=extracted_text,
                        workspace_id=workspace_id,
                        citation_service=citation_service,
                    )

                    # Assign citekey + commit with retry: a concurrent ingest of
                    # the same author/year can win the unique (workspace_id,
                    # citekey) index between our SELECT and COMMIT; on conflict,
                    # roll back and re-derive (we'll now see the competitor and
                    # pick the next suffix).
                    key_source = csl if csl else {}
                    for attempt in range(4):
                        try:
                            ref3 = s3.get(Reference, reference_id)
                            if ref3 is None:
                                # Concurrent delete between attempts — nothing to update.
                                logger.warning(
                                    "ingest Phase 3 retry: ref vanished workspace=%s "
                                    "reference=%s (attempt %d) — aborting citekey loop",
                                    workspace_id,
                                    reference_id,
                                    attempt,
                                )
                                break
                            if csl:
                                ref3.csl_json_extracted = csl
                                title_val = csl.get("title")
                                if isinstance(title_val, str) and title_val:
                                    ref3.title = title_val
                                ref3.metadata_source = source
                                ref3.metadata_status = status
                            else:
                                ref3.metadata_status = "failed"
                            if auto_classify_kind and classified_kind:
                                ref3.kind = classified_kind
                            ref3.citekey = generate_citekey(key_source, workspace_id, s3)
                            s3.commit()
                            break
                        except IntegrityError:
                            s3.rollback()
                            if attempt == 3:
                                raise  # let outer Phase-3 except mark it failed
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
    extract_timeout_seconds: float = 30.0,
    auto_classify_kind: bool = False,
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

    return run_postwrite_phases(
        session_factory=session_factory,
        storage=storage,
        embedder=embedder,
        reference_id=reference_id,
        workspace_id=workspace_id,
        mime_type=mime_type,
        data=data,
        source_meta=source_meta,
        citation_service=citation_service,
        extract_timeout_seconds=extract_timeout_seconds,
        auto_classify_kind=auto_classify_kind,
    )


class ReprocessConflict(Exception):
    """Raised when a reference cannot be claimed for reprocess (not failed,
    or already being reprocessed)."""


def reprocess_reference(
    *,
    session_factory,
    storage: ReferenceStorage,
    embedder: Embedder,
    reference_id: uuid.UUID,
    workspace_id: uuid.UUID,
    citation_service,
    extract_timeout_seconds: float = 30.0,
) -> IngestResult:
    """Re-run extract/embed/metadata on an EXISTING reference's stored bytes.

    Atomically claims failed->running so concurrent reprocess calls cannot both
    proceed (they would collide on the unique (reference_id, chunk_index)
    index). Deletes any leftover chunks before re-embedding. Raises
    ReprocessConflict if the row is not in 'failed' state."""
    with session_factory() as s:
        claimed = s.execute(
            _sql_update(Reference)
            .where(
                Reference.id == reference_id,
                Reference.workspace_id == workspace_id,
                Reference.text_status == "failed",
            )
            .values(text_status="running", text_error=None)
            .returning(Reference.mime_type)
        ).first()
        if claimed is None:
            s.rollback()
            raise ReprocessConflict(f"reference {reference_id} not claimable for reprocess")
        mime_type = claimed[0]
        s.query(ReferenceChunk).filter(
            ReferenceChunk.reference_id == reference_id,
            ReferenceChunk.workspace_id == workspace_id,
        ).delete(synchronize_session=False)
        s.commit()

    try:
        suffix = suffix_for_mime(mime_type)
        data = storage.path_for(
            workspace_id=workspace_id, reference_id=reference_id, suffix=suffix
        ).read_bytes()
        return run_postwrite_phases(
            session_factory=session_factory,
            storage=storage,
            embedder=embedder,
            reference_id=reference_id,
            workspace_id=workspace_id,
            mime_type=mime_type,
            data=data,
            source_meta=None,
            citation_service=citation_service,
            extract_timeout_seconds=extract_timeout_seconds,
            auto_classify_kind=False,
        )
    except Exception:
        logger.exception(
            "reprocess_reference aborted after claim workspace=%s reference=%s"
            " — reverting to failed",
            workspace_id,
            reference_id,
        )
        with session_factory() as s_rev:
            ref_rev = s_rev.get(Reference, reference_id)
            if ref_rev is not None:
                ref_rev.text_status = "failed"
                ref_rev.text_error = "reprocess aborted before extraction"
                s_rev.commit()
        raise
