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


class FetchIn(BaseModel):
    input: str
    kind: str = "paper"


@router.post("/workspaces/{workspace_id}/references/fetch", response_model=IngestResultOut)
async def api_fetch_reference(
    workspace_id: uuid.UUID,
    body: FetchIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> IngestResultOut:
    from call_manager.proposal_assist.references.dispatcher import (
        detect_source_type,
        dispatch_fetch,
    )
    from call_manager.proposal_assist.references.fetcher import FetchError, SSRFError

    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
    if body.kind not in ALLOWED_KINDS:
        raise HTTPException(status_code=400, detail=f"invalid kind: {body.kind!r}")
    try:
        detect_source_type(body.input)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    factory = request.app.state.session_factory
    storage = _storage_for(request)
    embedder = OllamaEmbedderAdapter(request.app.state.embed_client)
    limiter = request.app.state.reference_ingest_limiter
    citation_service = request.app.state.citation_service
    timeout = request.app.state.settings.reference_extract_timeout_seconds

    def _run():
        with limiter.slot():
            fetch_data, filename, mime_type, source_meta = dispatch_fetch(body.input)
            with factory() as own:
                result = ingest_bytes(
                    session_factory=factory,
                    session=own,
                    storage=storage,
                    embedder=embedder,
                    workspace_id=workspace_id,
                    kind=body.kind,
                    source_type=source_meta["source"],
                    source_uri=body.input,
                    filename=_sanitize_filename(filename),
                    mime_type=mime_type,
                    data=fetch_data,
                    uploaded_by=user.id,
                    source_meta=source_meta,
                    citation_service=citation_service,
                    extract_timeout_seconds=timeout,
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
        return result

    try:
        result = await run_in_threadpool(_run)
    except OverCapacity:
        raise HTTPException(
            status_code=429, detail="server busy", headers={"Retry-After": "2"}
        ) from None
    except SSRFError as e:  # subclass of FetchError — must precede the FetchError arm
        raise HTTPException(status_code=400, detail=str(e)) from None
    except FetchError as e:
        raise HTTPException(status_code=502, detail=f"fetch failed: {e}") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return IngestResultOut(
        reference_id=str(result.reference_id),
        text_status=result.text_status,
        text_error=result.text_error,
    )


class MetadataIn(BaseModel):
    title: str
    authors: list[str] = []
    year: str | None = None
    venue: str | None = None
    doi: str | None = None


class MetadataOut(BaseModel):
    citekey: str | None
    metadata_status: str
    csl_json: dict


@router.post(
    "/workspaces/{workspace_id}/references/{reference_id}/metadata", response_model=MetadataOut
)
def api_approve_metadata(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    body: MetadataIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> MetadataOut:
    import contextlib

    from sqlalchemy.exc import IntegrityError

    from call_manager.proposal_assist.references.csl import generate_citekey
    from call_manager.proposal_assist.web.references import _parse_authors

    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        ref = session.scalar(select(Reference).where(Reference.id == reference_id))
        if ref is None or ref.workspace_id != workspace_id or ref.tombstoned_at is not None:
            raise HTTPException(status_code=404, detail="reference not found")

        csl: dict = {}
        title = (body.title or "").strip()
        if title:
            csl["title"] = title
        parsed_authors = _parse_authors("\n".join(body.authors))
        if parsed_authors:
            csl["author"] = parsed_authors
        year_raw = (body.year or "").strip()
        if year_raw:
            with contextlib.suppress(ValueError):
                csl["issued"] = {"date-parts": [[int(year_raw)]]}
        venue = (body.venue or "").strip()
        if venue:
            csl["container-title"] = venue
        doi = (body.doi or "").strip()
        if doi:
            csl["DOI"] = doi

        if not csl or not csl.get("title"):
            raise HTTPException(
                status_code=400, detail="provide at least a title before approving."
            )

        ref.csl_json = csl
        if isinstance(csl.get("title"), str) and csl["title"]:
            ref.title = csl["title"]
        ref.metadata_status = "approved"
        if ref.citekey is None or ref.citekey.startswith("ref-"):
            new_key = generate_citekey(csl, workspace_id, session)
            if new_key and not new_key.startswith("ref-"):
                ref.citekey = new_key

        try:
            session.add(
                WorkspaceEvent(
                    workspace_id=workspace_id,
                    kind="reference.metadata_approved",
                    actor_id=user.id,
                    payload={"reference_id": str(reference_id)},
                )
            )
            session.commit()
        except IntegrityError:
            session.rollback()
            ref = session.scalar(select(Reference).where(Reference.id == reference_id))
            ref.csl_json = csl
            if isinstance(csl.get("title"), str) and csl["title"]:
                ref.title = csl["title"]
            ref.metadata_status = "approved"
            session.add(
                WorkspaceEvent(
                    workspace_id=workspace_id,
                    kind="reference.metadata_approved",
                    actor_id=user.id,
                    payload={"reference_id": str(reference_id)},
                )
            )
            session.commit()

        return MetadataOut(citekey=ref.citekey, metadata_status=ref.metadata_status, csl_json=csl)
