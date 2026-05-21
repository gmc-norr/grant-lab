"""B7 files panel router — upload / list / serve / delete attachments.

Permission policy (Codex review of the B7 plan, rev2):

- GET  /files            (panel page)   owner OR viewer  — viewers need to
                                                          see chat images.
- POST /files            (upload)       owner only       — single-write.
- GET  /files/<id>       (serve bytes)  owner OR viewer  — same reason as panel.
- POST /files/<id>/delete                owner only       — single-write.

Atomicity ordering (load-bearing — see plan's "Plan-Critical Constraints"):

  upload:
    1. _require_owner
    2. content_type allowlist     (cheap MIME check)
    3. stream-read up to MAX+1    (avoid memory DoS)
    4. Pillow probe               (defense layer 2: bytes really ARE an image)
    5. derive suffix from MIME    (NEVER from upload filename)
    6. storage.write_atomic       (temp + fsync + os.replace)
    7. INSERT Attachment row
    8. session.commit()
    If 7-8 raises → storage.delete_one to drop the orphan file.

  delete:
    1. _require_owner
    2. SELECT Attachment for (id, workspace_id) — idempotent if absent
    3. session.delete(att); session.commit()  — row is source of truth
    4. storage.delete_one (best-effort; log on failure)

Cross-workspace IDOR: serve/delete look up the row by id, then return 404
(not 403) when ``att.workspace_id != path workspace_id`` — we do not leak
the existence of attachments the caller can't see.
"""

from __future__ import annotations

import io
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import Attachment, Workspace, WorkspaceViewer
from call_manager.db.session import get_db_dependency
from call_manager.proposal_assist.attachments.storage import AttachmentStorage
from call_manager.proposal_assist.attachments.tokens import estimate_image_tokens

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["files"])

# Hard caps (intentionally not env-gated for v1 — revisit if users hit them).
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per image.
_CHUNK_BYTES = 64 * 1024

_ALLOWED_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})

_SUFFIX_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# Pillow exposes formats as JPEG/PNG/WEBP/GIF (uppercase). Map back to the
# allow-listed MIME so the "client claims X, bytes are actually Y" check
# is a single equality comparison.
_PIL_FORMAT_TO_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


# ---------------------------------------------------------------------------
# Dependency wiring (mirrors workspaces.py / rubric_review.py)
# ---------------------------------------------------------------------------


def _session_dep(request: Request):
    factory = request.app.state.session_factory
    yield from get_db_dependency(factory)()


def _load_workspace_or_404(workspace_id: uuid.UUID, session: Session) -> Workspace:
    ws = session.get(Workspace, workspace_id)
    if ws is None or ws.soft_deleted_at is not None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return ws


def _require_owner(
    *,
    workspace_id: uuid.UUID,
    user,
    session: Session,
) -> Workspace:
    ws = _load_workspace_or_404(workspace_id, session)
    if ws.owner_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="only the workspace owner may perform this action",
        )
    return ws


def _require_owner_or_viewer(
    *,
    workspace_id: uuid.UUID,
    user,
    session: Session,
) -> Workspace:
    ws = _load_workspace_or_404(workspace_id, session)
    if ws.owner_id == user.id:
        return ws
    is_viewer = (
        session.query(WorkspaceViewer).filter_by(workspace_id=workspace_id, user_id=user.id).first()
        is not None
    )
    if not is_viewer:
        raise HTTPException(
            status_code=403,
            detail="you do not have access to this workspace",
        )
    return ws


def _storage_for(request: Request) -> AttachmentStorage:
    settings = request.app.state.settings
    return AttachmentStorage(root=Path(settings.workspaces_dir))


# ---------------------------------------------------------------------------
# Filename sanitization (re-used from the B6 markdown-export hardening)
# ---------------------------------------------------------------------------


def _sanitize_filename(name: str, *, default: str = "attachment") -> str:
    """Allowlist-based: keep alnum + ``-_.``; everything else becomes ``_``.

    Explicitly rejects ``..`` runs (would otherwise pass through as ``..``
    after the allowlist filter and let a server-side header consumer
    misinterpret the path) — they collapse to a single ``.``.
    """
    if not name:
        return default
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)
    # Defense-in-depth: collapse ".." runs that survived the allowlist.
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    cleaned = cleaned.strip("._") or default
    return cleaned[:120]


# ---------------------------------------------------------------------------
# GET /workspaces/<id>/files  — panel page
# ---------------------------------------------------------------------------


@router.get("/{workspace_id}/files", response_class=HTMLResponse)
def list_files(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    attachments = list(
        session.scalars(
            select(Attachment)
            .where(Attachment.workspace_id == ws.id)
            .order_by(Attachment.uploaded_at.desc())
        )
    )
    is_owner = ws.owner_id == user.id
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "proposal_assist/workspace_files.html",
        {
            "workspace": ws,
            "user": user,
            "is_owner": is_owner,
            "attachments": attachments,
        },
    )


# ---------------------------------------------------------------------------
# POST /workspaces/<id>/files  — upload
# ---------------------------------------------------------------------------


async def _read_capped(file: UploadFile, max_bytes: int) -> bytes:
    """Read up to ``max_bytes + 1`` bytes from the upload, in chunks.

    If we exceed the cap, raise ``HTTPException(400)`` BEFORE returning —
    the caller never sees a too-large payload and the bytes are released
    when the function returns. Reading all-at-once via ``await file.read()``
    would be a memory DoS vector.
    """
    buf = bytearray()
    while True:
        chunk = await file.read(_CHUNK_BYTES)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"file exceeds {max_bytes // (1024 * 1024)} MB limit",
            )
    return bytes(buf)


def _probe_image_format(data: bytes) -> tuple[str, int, int]:
    """Open the bytes with Pillow and return ``(format, width, height)``.

    Raises ``HTTPException(400)`` if the bytes are not a recognized image
    OR if the detected format is not in our allowlist. This is defense
    layer 2 against a client lying about ``content_type``.

    B7: the size is needed at upload time to pre-populate the
    ``Attachment.tokens_estimate`` column so submit-time cap accounting
    doesn't need to re-read bytes for every queued turn.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format
            width, height = img.size
    except (UnidentifiedImageError, OSError) as e:
        raise HTTPException(status_code=400, detail=f"unrecognised image data: {e}") from e
    if fmt not in _PIL_FORMAT_TO_MIME:
        raise HTTPException(status_code=400, detail=f"unsupported image format: {fmt!r}")
    return fmt, width, height


@router.post("/{workspace_id}/files", response_class=HTMLResponse)
async def upload_file(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
    file: UploadFile = File(...),  # noqa: B008
) -> HTMLResponse:
    ws = _require_owner(workspace_id=workspace_id, user=user, session=session)

    # 1. Content-Type allowlist (cheap defense layer 1).
    content_type = (file.content_type or "").lower()
    if content_type not in _ALLOWED_MIMES:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported content_type: {content_type!r}",
        )

    # 2. Stream-read in chunks; reject oversize BEFORE touching disk.
    data = await _read_capped(file, _MAX_BYTES)
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")

    # 3. Pillow probe (defense layer 2 — bytes must actually be an image,
    #    and the detected format must match the claimed content_type).
    #    Also extracts dims for the B7 token-estimate backfill below.
    fmt, width_px, height_px = _probe_image_format(data)
    detected_mime = _PIL_FORMAT_TO_MIME[fmt]
    if detected_mime != content_type:
        raise HTTPException(
            status_code=400,
            detail=(
                f"content_type {content_type!r} does not match the actual "
                f"image format {detected_mime!r}"
            ),
        )

    # 4. Derive suffix from MIME (NEVER from the client filename — that's
    #    the path-traversal vector).
    suffix = _SUFFIX_BY_MIME[content_type]

    # 5. Generate the attachment id up-front so we can write the file
    #    BEFORE inserting the row. If the DB insert/commit fails, we know
    #    the exact path to clean up.
    aid = uuid.uuid4()
    storage = _storage_for(request)
    safe_filename = _sanitize_filename(file.filename or f"attachment{suffix}")
    relative_storage_path = f"{aid}{suffix}"

    # 6. Atomic write to disk (temp + fsync + os.replace).
    try:
        final_path = storage.write_atomic(
            workspace_id=ws.id,
            attachment_id=aid,
            suffix=suffix,
            data=data,
        )
    except OSError as e:
        logger.warning(
            "upload_file: storage.write_atomic failed for workspace=%s: %s",
            ws.id,
            e,
        )
        raise HTTPException(status_code=500, detail="storage write failed") from e

    # 7. Insert row + commit. If commit fails, clean up the orphan file
    #    via storage.delete_one — the DB has no row pointing at it.
    #    Token estimate is pre-computed here so submit-time cap accounting
    #    is one cheap sum() instead of a per-turn re-read of bytes.
    tokens_estimate = estimate_image_tokens(width_px, height_px)
    att = Attachment(
        id=aid,
        workspace_id=ws.id,
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
        try:
            storage.delete_one(workspace_id=ws.id, attachment_id=aid, suffix=suffix)
        except OSError:
            # The DB commit already failed; orphan file is harmless (no row
            # references it). Log and move on so we surface the real error.
            logger.warning(
                "upload_file: orphan cleanup failed for attachment %s at %s",
                aid,
                final_path,
                exc_info=True,
            )
        logger.warning(
            "upload_file: commit failed for workspace=%s; orphan file removed: %s",
            ws.id,
            e,
        )
        raise HTTPException(status_code=500, detail="failed to persist attachment") from e

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "proposal_assist/partials/file_row.html",
        {"att": att, "workspace": ws, "user": user, "is_owner": True},
    )


# ---------------------------------------------------------------------------
# GET /workspaces/<id>/files/<aid>  — serve bytes
# ---------------------------------------------------------------------------


@router.get("/{workspace_id}/files/{attachment_id}")
def serve_file(
    workspace_id: uuid.UUID,
    attachment_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> Response:
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    att = session.get(Attachment, attachment_id)
    # IDOR defense: return 404 (NOT 403) on cross-workspace lookups so we
    # don't leak the existence of attachments outside the caller's view.
    if att is None or att.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="attachment not found")

    suffix = Path(att.storage_path).suffix.lower()
    storage = _storage_for(request)
    try:
        path = storage.path_for(
            workspace_id=ws.id,
            attachment_id=att.id,
            suffix=suffix,
        )
    except ValueError as e:
        # Unsupported suffix snuck into the DB somehow — treat as missing.
        logger.warning(
            "serve_file: attachment %s has disallowed suffix %r: %s",
            att.id,
            suffix,
            e,
        )
        raise HTTPException(status_code=404, detail="attachment not found") from e
    if not path.exists():
        raise HTTPException(status_code=404, detail="attachment file missing")

    safe_filename = _sanitize_filename(att.filename or f"attachment{suffix}")
    return Response(
        content=path.read_bytes(),
        media_type=att.mime_type,
        headers={
            # private: a shared proxy MUST NOT cache and serve to another user.
            # max-age=300: tolerable for chat-UX latency; the bytes are
            # otherwise immutable per attachment id.
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": f'inline; filename="{safe_filename}"',
        },
    )


# ---------------------------------------------------------------------------
# POST /workspaces/<id>/files/<aid>/delete
# ---------------------------------------------------------------------------


@router.post("/{workspace_id}/files/{attachment_id}/delete")
def delete_file(
    workspace_id: uuid.UUID,
    attachment_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> Response:
    ws = _require_owner(workspace_id=workspace_id, user=user, session=session)

    att = session.get(Attachment, attachment_id)
    if att is None or att.workspace_id != ws.id:
        # Idempotent: nothing to delete. 303 back to the panel for browsers,
        # but for HTMX swaps we want an empty 200 so the row vanishes.
        # Detect HTMX via the HX-Request header.
        if request.headers.get("hx-request"):
            return HTMLResponse("")
        return RedirectResponse(url=f"/workspaces/{ws.id}/files", status_code=303)

    suffix = Path(att.storage_path).suffix.lower()

    # Row-first delete (Codex-revised ordering): the DB row is the source
    # of truth. Commit it BEFORE touching the filesystem so a unlink
    # failure can never leave the row pointing at a missing file.
    session.delete(att)
    session.commit()

    storage = _storage_for(request)
    try:
        storage.delete_one(
            workspace_id=ws.id,
            attachment_id=attachment_id,
            suffix=suffix,
        )
    except OSError:
        # Row is already gone; orphan file is harmless. Log + sweep later.
        logger.warning(
            "delete_file: unlink failed for attachment %s; row already removed",
            attachment_id,
            exc_info=True,
        )

    if request.headers.get("hx-request"):
        return HTMLResponse("")
    return RedirectResponse(url=f"/workspaces/{ws.id}/files", status_code=303)
