"""JSON /api surface for image attachments (Cockpit Library Files, Slice C-media).

Owner-gated writes (upload/delete) + member-gated list + member-gated byte
serve, using the shared file-validation, storage, and token-estimation helpers.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import Attachment, User
from call_manager.proposal_assist.attachments.tokens import estimate_image_tokens
from call_manager.proposal_assist.web._file_uploads import (
    _ALLOWED_MIMES,
    _MAX_BYTES,
    _PIL_FORMAT_TO_MIME,
    _SUFFIX_BY_MIME,
    _probe_image_format,
    _read_capped,
    _sanitize_filename,
    _storage_for,
)
from call_manager.proposal_assist.web._perms import require_member, require_owner

logger = logging.getLogger(__name__)

router = APIRouter()


class FileListItemOut(BaseModel):
    id: str
    filename: str
    mime_type: str
    uploaded_at: str


class FileOut(BaseModel):
    attachment_id: str
    filename: str
    mime_type: str


@router.get("/workspaces/{workspace_id}/files", response_model=list[FileListItemOut])
def api_list_files(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> list[FileListItemOut]:
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        rows = session.scalars(
            select(Attachment)
            .where(Attachment.workspace_id == workspace_id)
            .order_by(Attachment.uploaded_at.desc())
        ).all()
        return [
            FileListItemOut(
                id=str(a.id),
                filename=a.filename,
                mime_type=a.mime_type,
                uploaded_at=a.uploaded_at.isoformat(),
            )
            for a in rows
        ]


@router.post("/workspaces/{workspace_id}/files", response_model=FileOut)
async def api_upload_file(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
    file: UploadFile = File(...),  # noqa: B008
) -> FileOut:
    sf = request.app.state.session_factory
    with sf() as session:
        require_owner(workspace_id=workspace_id, user=user, session=session)

    content_type = (file.content_type or "").lower()
    if content_type not in _ALLOWED_MIMES:
        raise HTTPException(status_code=400, detail=f"unsupported content_type: {content_type!r}")
    data = await _read_capped(file, _MAX_BYTES)  # HTTPException(400) over the 5 MB cap
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    fmt, width_px, height_px = _probe_image_format(data)  # HTTPException(400) on bad image
    detected_mime = _PIL_FORMAT_TO_MIME[fmt]
    if detected_mime != content_type:
        raise HTTPException(
            status_code=400,
            detail=(
                f"content_type {content_type!r} does not match the actual "
                f"image format {detected_mime!r}"
            ),
        )

    suffix = _SUFFIX_BY_MIME[content_type]
    aid = uuid.uuid4()
    storage = _storage_for(request)
    safe_filename = _sanitize_filename(file.filename or f"attachment{suffix}")
    relative_storage_path = f"{aid}{suffix}"

    try:
        storage.write_atomic(workspace_id=workspace_id, attachment_id=aid, suffix=suffix, data=data)
    except OSError as e:
        raise HTTPException(status_code=500, detail="storage write failed") from e

    tokens_estimate = estimate_image_tokens(width_px, height_px)
    with sf() as session:
        att = Attachment(
            id=aid,
            workspace_id=workspace_id,
            kind="figure",
            filename=safe_filename,
            storage_path=relative_storage_path,
            mime_type=content_type,
            uploaded_by=user.id,
            width_px=width_px,
            height_px=height_px,
            tokens_estimate=tokens_estimate,
        )
        session.add(att)
        try:
            session.commit()
        except Exception as e:
            session.rollback()
            with contextlib.suppress(OSError):
                storage.delete_one(workspace_id=workspace_id, attachment_id=aid, suffix=suffix)
            raise HTTPException(status_code=500, detail="failed to persist attachment") from e

    return FileOut(attachment_id=str(aid), filename=safe_filename, mime_type=content_type)


@router.delete("/workspaces/{workspace_id}/files/{attachment_id}", status_code=204)
def api_delete_file(
    workspace_id: uuid.UUID,
    attachment_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> Response:
    sf = request.app.state.session_factory
    suffix = None
    with sf() as session:
        require_owner(workspace_id=workspace_id, user=user, session=session)
        att = session.get(Attachment, attachment_id)
        if att is None or att.workspace_id != workspace_id:
            return Response(status_code=204)  # idempotent: nothing to delete
        suffix = Path(att.storage_path).suffix.lower()
        session.delete(att)  # row-first delete (source of truth)
        session.commit()

    storage = _storage_for(request)
    with contextlib.suppress(OSError):
        storage.delete_one(workspace_id=workspace_id, attachment_id=attachment_id, suffix=suffix)
    return Response(status_code=204)


@router.get("/workspaces/{workspace_id}/files/{attachment_id}")
def api_serve_file(
    workspace_id: uuid.UUID,
    attachment_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> FileResponse:
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        att = session.get(Attachment, attachment_id)
        # IDOR defense: return 404 (NOT 403) on cross-workspace lookups so we
        # don't leak the existence of attachments outside the caller's view.
        if att is None or att.workspace_id != ws.id:
            raise HTTPException(status_code=404, detail="attachment not found")
        mime_type = att.mime_type
        filename = att.filename
        storage_suffix = Path(att.storage_path).suffix.lower()

    storage = _storage_for(request)
    try:
        path = storage.path_for(
            workspace_id=workspace_id,
            attachment_id=attachment_id,
            suffix=storage_suffix,
        )
    except ValueError as e:
        # Unsupported suffix snuck into the DB somehow — treat as missing.
        logger.warning(
            "api_serve_file: attachment %s has disallowed suffix %r: %s",
            attachment_id,
            storage_suffix,
            e,
        )
        raise HTTPException(status_code=404, detail="attachment not found") from e
    if not path.exists():
        raise HTTPException(status_code=404, detail="attachment file missing")

    safe_filename = _sanitize_filename(filename or f"attachment{storage_suffix}")
    return FileResponse(
        path,
        media_type=mime_type,
        headers={
            # private: a shared proxy MUST NOT cache and serve to another user.
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": f'inline; filename="{safe_filename}"',
        },
    )
