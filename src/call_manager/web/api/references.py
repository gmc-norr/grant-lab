"""JSON /api surface for reference write/management (Cockpit Library, Slice C).

Member-gated mutations that REUSE the existing reference service functions
(ingest_bytes, dispatch_fetch, delete_or_tombstone_reference, reprocess_reference,
generate_citekey, _parse_authors) and inline the same per-route orchestration the
Jinja routes use (limiter slot, dedup, 50 MB cap, audit). The Jinja reference
routes coexist until Slice G deletes them; the short-lived duplication of the
audit `kind` literals is intentional and pinned by tests on both sides.
"""

from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import Reference, User, WorkspaceEvent
from call_manager.proposal_assist.references.admission import OverCapacity
from call_manager.proposal_assist.references.embed import OllamaEmbedderAdapter
from call_manager.proposal_assist.references.ingest import ingest_bytes, suffix_for_mime
from call_manager.proposal_assist.references.kinds import ALLOWED_KINDS
from call_manager.proposal_assist.web._perms import require_member
from call_manager.proposal_assist.web.references import (
    _ALLOWED_MIMES,
    _MAX_BYTES,
    _read_capped,
    _sanitize_filename,
    _storage_for,
)

router = APIRouter()


class _BadUpload(Exception):
    """A 400-class upload validation failure raised from inside the threadpool."""


class IngestResultOut(BaseModel):
    reference_id: str
    text_status: str
    text_error: str | None = None


@router.post("/workspaces/{workspace_id}/references", response_model=IngestResultOut)
async def api_upload_reference(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
    kind: str = Form("paper"),
    auto_classify: bool = Form(False),
    file: UploadFile = File(...),  # noqa: B008
) -> IngestResultOut:
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)

    content_type = (file.content_type or "").lower()
    if content_type not in _ALLOWED_MIMES:
        raise HTTPException(status_code=400, detail=f"unsupported content_type: {content_type!r}")
    if not auto_classify and kind not in ALLOWED_KINDS:
        raise HTTPException(status_code=400, detail=f"invalid kind: {kind!r}")

    data = await _read_capped(file, _MAX_BYTES)  # raises HTTPException(400) over the 50 MB cap
    filename = _sanitize_filename(file.filename or f"reference{suffix_for_mime(content_type)}")

    factory = request.app.state.session_factory
    storage = _storage_for(request)
    embedder = OllamaEmbedderAdapter(request.app.state.embed_client)
    limiter = request.app.state.reference_ingest_limiter
    citation_service = request.app.state.citation_service
    timeout = request.app.state.settings.reference_extract_timeout_seconds

    def _run() -> IngestResultOut:
        with limiter.slot():
            if auto_classify:
                if not data.startswith(b"%PDF-"):
                    raise _BadUpload("drag-drop accepts real PDF files only.")
                sha = hashlib.sha256(data).hexdigest()
                with factory() as sdup:
                    dup = sdup.scalar(
                        select(Reference)
                        .where(
                            Reference.workspace_id == workspace_id,
                            Reference.original_sha256 == sha,
                            Reference.tombstoned_at.is_(None),
                        )
                        .order_by(Reference.created_at.desc())
                    )
                    if dup is not None:
                        return IngestResultOut(
                            reference_id=str(dup.id), text_status=dup.text_status, text_error=None
                        )
            with factory() as own:
                result = ingest_bytes(
                    session_factory=factory,
                    session=own,
                    storage=storage,
                    embedder=embedder,
                    workspace_id=workspace_id,
                    kind="paper" if auto_classify else kind,
                    source_type="uploaded",
                    source_uri=None,
                    filename=filename,
                    mime_type=content_type,
                    data=data,
                    uploaded_by=user.id,
                    citation_service=citation_service,
                    extract_timeout_seconds=timeout,
                    auto_classify_kind=auto_classify,
                )
        with factory() as sev:
            sev.add(
                WorkspaceEvent(
                    workspace_id=workspace_id,
                    kind="reference.added",
                    actor_id=user.id,
                    payload={"reference_id": str(result.reference_id)},
                )
            )
            sev.commit()
        return IngestResultOut(
            reference_id=str(result.reference_id),
            text_status=result.text_status,
            text_error=result.text_error,
        )

    try:
        return await run_in_threadpool(_run)
    except OverCapacity:
        raise HTTPException(
            status_code=429, detail="server busy", headers={"Retry-After": "2"}
        ) from None
    except _BadUpload as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
