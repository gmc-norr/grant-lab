"""JSON /api surface for reference write/management (Cockpit Library, Slice C).

Member-gated mutations that REUSE the existing reference service functions
(ingest_bytes, dispatch_fetch, delete_or_tombstone_reference, reprocess_reference,
generate_citekey, _parse_authors) with route-level orchestration for the limiter
slot, deduplication, 50 MB cap, and audit events.
"""

from __future__ import annotations

import hashlib
import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from grant_lab.auth.dependencies import get_current_user
from grant_lab.db.models import Reference, User, WorkspaceEvent
from grant_lab.proposal_assist.paths import workspace_repo_dir
from grant_lab.proposal_assist.references.admission import OverCapacity
from grant_lab.proposal_assist.references.deletion import delete_or_tombstone_reference
from grant_lab.proposal_assist.references.embed import OllamaEmbedderAdapter
from grant_lab.proposal_assist.references.ingest import (
    ReprocessConflict,
    ingest_bytes,
    reprocess_reference,
    suffix_for_mime,
)
from grant_lab.proposal_assist.references.kinds import ALLOWED_KINDS
from grant_lab.proposal_assist.references.uploads import (
    _ALLOWED_MIMES,
    _MAX_BYTES,
    _read_capped,
    _sanitize_filename,
    _storage_for,
)
from grant_lab.proposal_assist.web._cloud_gate import citation_cloud_permitted
from grant_lab.proposal_assist.web._perms import require_member

logger = logging.getLogger(__name__)

router = APIRouter()


class _BadUpload(Exception):
    """A 400-class upload validation failure raised from inside the threadpool."""


class IngestResultOut(BaseModel):
    reference_id: str
    text_status: str
    text_error: str | None = None



def _citation_service_if_permitted(request, *, workspace_id, user):
    """`app.state.citation_service`, or None when using it would send this user's
    reference text off-prem without their consent.

    DEGRADE, do not reject: uploading a PDF is not an AI action, and refusing the
    upload would break a non-AI feature over an AI consent. `ingest_bytes` and
    `reprocess_reference` already treat `citation_service=None` as "extract
    metadata heuristically", so None is an existing, tested path — not a new one.
    Returns the service unchanged under the default on-prem provider.
    """
    sf = request.app.state.session_factory
    with sf() as gate_session:
        permitted = citation_cloud_permitted(
            request=request, session=gate_session, workspace_id=workspace_id, user=user
        )
    if permitted:
        return request.app.state.citation_service
    logger.warning(
        "reference metadata extraction skipped — cloud citation LLM not permitted "
        "for this user/workspace",
        extra={
            "event": "citation_cloud_egress_withheld",
            "workspace_id": str(workspace_id),
            "user_id": user.id,
        },
    )
    return None


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

    factory = request.app.state.session_factory
    storage = _storage_for(request)
    embedder = OllamaEmbedderAdapter(request.app.state.embed_client)
    limiter = request.app.state.reference_ingest_limiter
    citation_service = _citation_service_if_permitted(
        request, workspace_id=workspace_id, user=user
    )
    timeout = request.app.state.settings.reference_extract_timeout_seconds

    try:
        # Acquire the ingest slot BEFORE buffering the upload (mirrors the Jinja
        # path): an over-capacity request returns 429 immediately, without first
        # reading up to 50 MB into memory. The threading-semaphore slot is held
        # across the async read AND the threadpool ingest.
        with limiter.slot():
            data = await _read_capped(file, _MAX_BYTES)  # HTTPException(400) over the 50 MB cap
            filename = _sanitize_filename(
                file.filename or f"reference{suffix_for_mime(content_type)}"
            )
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
                            reference_id=str(dup.id),
                            text_status=dup.text_status,
                            text_error=None,
                        )

            def _ingest():
                with factory() as own:
                    return ingest_bytes(
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

            result = await run_in_threadpool(_ingest)
    except OverCapacity:
        raise HTTPException(
            status_code=429, detail="server busy", headers={"Retry-After": "2"}
        ) from None
    except _BadUpload as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

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
    from grant_lab.proposal_assist.references.dispatcher import (
        detect_source_type,
        dispatch_fetch,
    )
    from grant_lab.proposal_assist.references.fetcher import FetchError, SSRFError

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
    citation_service = _citation_service_if_permitted(
        request, workspace_id=workspace_id, user=user
    )
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

    from grant_lab.proposal_assist.references.csl import generate_citekey
    from grant_lab.proposal_assist.references.uploads import _parse_authors

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
        except IntegrityError as _exc:
            # Only retry on a citekey unique-constraint violation (SQLSTATE 23505).
            # An unrelated constraint (FK, check, etc.) is re-raised so it isn't
            # silently masked as a sanitised 500.
            _orig = getattr(_exc, "orig", None)
            _sqlstate = getattr(_orig, "sqlstate", None) or getattr(_orig, "pgcode", None)
            if _sqlstate != "23505":
                raise
            session.rollback()
            # Re-fetch after rollback: a concurrent delete leaves None here.
            ref = session.scalar(select(Reference).where(Reference.id == reference_id))
            if ref is None:
                raise HTTPException(status_code=404, detail="reference not found") from None
            ref.csl_json = csl
            if isinstance(csl.get("title"), str) and csl["title"]:
                ref.title = csl["title"]
            ref.metadata_status = "approved"
            # Regenerate the citekey: the collision may have left ref.citekey at
            # its pre-approval value (null or a ref-fallback). Call generate_citekey
            # again so it resolves to the next available suffix (e.g. "roe2021a").
            if ref.citekey is None or ref.citekey.startswith("ref-"):
                new_key = generate_citekey(csl, workspace_id, session)
                if new_key and not new_key.startswith("ref-"):
                    ref.citekey = new_key
            session.add(
                WorkspaceEvent(
                    workspace_id=workspace_id,
                    kind="reference.metadata_approved",
                    actor_id=user.id,
                    payload={"reference_id": str(reference_id)},
                )
            )
            # Guard the RETRY commit too: a concurrent insert can take the next
            # suffix between our rollback and this re-commit, raising a SECOND
            # 23505. Without this guard that double-collision propagates as a
            # sanitised 500; return a clean 409 instead so the client can retry.
            try:
                session.commit()
            except IntegrityError as _exc2:
                _orig2 = getattr(_exc2, "orig", None)
                _sqlstate2 = (
                    getattr(_orig2, "sqlstate", None) or getattr(_orig2, "pgcode", None)
                )
                if _sqlstate2 != "23505":
                    raise
                session.rollback()
                raise HTTPException(
                    status_code=409,
                    detail="citekey collision; please retry approval.",
                ) from None

        return MetadataOut(citekey=ref.citekey, metadata_status=ref.metadata_status, csl_json=csl)


_WAIVER_MAX_REASON = 500  # mirrors proposal_assist/web/references.py:_MAX_WAIVER_REASON_LEN


class WaiverIn(BaseModel):
    waiver: bool
    reason: str | None = None


class WaiverOut(BaseModel):
    metadata_waiver: bool
    reason: str | None = None


@router.post(
    "/workspaces/{workspace_id}/references/{reference_id}/waiver", response_model=WaiverOut
)
def api_set_waiver(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    body: WaiverIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> WaiverOut:
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        ref = session.scalar(select(Reference).where(Reference.id == reference_id))
        if ref is None or ref.workspace_id != workspace_id or ref.tombstoned_at is not None:
            raise HTTPException(status_code=404, detail="reference not found")
        ref.metadata_waiver = body.waiver
        ref.metadata_waiver_reason = (
            ((body.reason or "").strip()[:_WAIVER_MAX_REASON] or None) if body.waiver else None
        )
        session.add(
            WorkspaceEvent(
                workspace_id=workspace_id,
                kind="reference_metadata_waived"
                if body.waiver
                else "reference_metadata_waiver_cleared",
                actor_id=user.id,
                payload={
                    "reference_id": str(ref.id),
                    "waiver": body.waiver,
                    "reason": ref.metadata_waiver_reason,
                },
            )
        )
        session.commit()
        return WaiverOut(metadata_waiver=ref.metadata_waiver, reason=ref.metadata_waiver_reason)


class KindIn(BaseModel):
    kind: str


class KindOut(BaseModel):
    kind: str


@router.patch("/workspaces/{workspace_id}/references/{reference_id}/kind", response_model=KindOut)
def api_update_kind(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    body: KindIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> KindOut:
    if body.kind not in ALLOWED_KINDS:
        raise HTTPException(status_code=400, detail=f"invalid kind: {body.kind!r}")
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        ref = session.scalar(select(Reference).where(Reference.id == reference_id))
        if ref is None or ref.workspace_id != workspace_id or ref.tombstoned_at is not None:
            raise HTTPException(status_code=404, detail="reference not found")
        old = ref.kind
        ref.kind = body.kind
        session.add(
            WorkspaceEvent(
                workspace_id=workspace_id,
                kind="reference.kind_changed",
                actor_id=user.id,
                payload={"reference_id": str(ref.id), "from": old, "to": body.kind},
            )
        )
        session.commit()
        return KindOut(kind=body.kind)


class DeleteResultOut(BaseModel):
    outcome: str  # "deleted" | "tombstoned"


@router.post(
    "/workspaces/{workspace_id}/references/{reference_id}/delete", response_model=DeleteResultOut
)
def api_delete_reference(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> DeleteResultOut:
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        storage = _storage_for(request)
        repo_dir = workspace_repo_dir(request.app.state.settings.workspaces_dir, workspace_id)
        try:
            outcome = delete_or_tombstone_reference(
                session=session,
                repo_dir=repo_dir,
                reference_id=reference_id,
                workspace_id=workspace_id,
                storage=storage,
                actor_id=user.id,
            )
        except ValueError:
            raise HTTPException(status_code=404, detail="reference not found") from None
    return DeleteResultOut(outcome=outcome)


@router.post(
    "/workspaces/{workspace_id}/references/{reference_id}/reprocess",
    response_model=IngestResultOut,
)
async def api_reprocess_reference(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> IngestResultOut:
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        ref = session.scalar(select(Reference).where(Reference.id == reference_id))
        if ref is None or ref.workspace_id != workspace_id or ref.tombstoned_at is not None:
            raise HTTPException(status_code=404, detail="reference not found")

    factory = request.app.state.session_factory
    storage = _storage_for(request)
    embedder = OllamaEmbedderAdapter(request.app.state.embed_client)
    limiter = request.app.state.reference_ingest_limiter
    citation_service = _citation_service_if_permitted(
        request, workspace_id=workspace_id, user=user
    )
    timeout = request.app.state.settings.reference_extract_timeout_seconds

    def _run():
        with limiter.slot():
            return reprocess_reference(
                session_factory=factory,
                storage=storage,
                embedder=embedder,
                reference_id=reference_id,
                workspace_id=workspace_id,
                citation_service=citation_service,
                extract_timeout_seconds=timeout,
            )

    try:
        result = await run_in_threadpool(_run)
    except OverCapacity:
        raise HTTPException(
            status_code=429, detail="server busy", headers={"Retry-After": "2"}
        ) from None
    except ReprocessConflict:
        raise HTTPException(
            status_code=409, detail="reference is not in a reprocessable state"
        ) from None

    with factory() as sev:
        sev.add(
            WorkspaceEvent(
                workspace_id=workspace_id,
                kind="reference.reprocessed",
                actor_id=user.id,
                payload={"reference_id": str(reference_id)},
            )
        )
        sev.commit()
    return IngestResultOut(
        reference_id=str(result.reference_id),
        text_status=result.text_status,
        text_error=result.text_error,
    )


@router.get("/workspaces/{workspace_id}/references/{reference_id}/raw")
def api_serve_reference_raw(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> FileResponse:
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        ref = session.scalar(select(Reference).where(Reference.id == reference_id))
        if ref is None or ref.workspace_id != ws.id:
            # Don't leak existence — 404 for cross-workspace lookups too.
            raise HTTPException(status_code=404, detail="reference not found")
        mime_type = ref.mime_type
        filename = ref.filename

    storage = _storage_for(request)
    suffix = suffix_for_mime(mime_type)
    path = storage.path_for(workspace_id=workspace_id, reference_id=reference_id, suffix=suffix)
    if not path.exists():
        raise HTTPException(status_code=404, detail="reference bytes missing")

    safe_name = _sanitize_filename(filename)
    return FileResponse(
        path,
        media_type=mime_type,
        headers={
            # Force download — references serve a wide MIME range
            # (HTML/SVG/etc would otherwise be a same-origin XSS vector).
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=300",
        },
    )
