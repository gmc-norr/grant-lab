"""Web routes for references — Slice 1 (panel + upload + raw serve).

Permission policy:
- GET  /workspaces/<id>/references           owner OR viewer
- POST /workspaces/<id>/references           owner only
- GET  /workspaces/<id>/references/<id>/raw  owner OR viewer

Atomicity: mirrors B7 attachments — Phase 1 atomic, Phase 2 sync Stage 1
retain-on-failure. See ingest.py.
"""

from __future__ import annotations

import html as _html
import logging
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import Reference, Workspace, WorkspaceViewer
from call_manager.db.session import get_db_dependency
from call_manager.proposal_assist.references.embed import OllamaEmbedderAdapter
from call_manager.proposal_assist.references.ingest import (
    IngestResult,
    ingest_bytes,
    suffix_for_mime,
)
from call_manager.proposal_assist.references.storage import ReferenceStorage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["references"])

_ALLOWED_MIMES = frozenset(
    {
        "application/pdf",
        "text/markdown",
        "text/plain",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/rtf",
        "text/csv",
        # Legacy .xls (application/vnd.ms-excel) is intentionally NOT accepted:
        # openpyxl can't read the binary BIFF format, so it would always fail
        # extraction. Users should upload .xlsx (OOXML).
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/x-ipynb+json",
    }
)
_ALLOWED_KINDS = frozenset({"paper", "prior_proposal", "analysis", "data"})
_MAX_BYTES = 50 * 1024 * 1024  # 50 MB per spec REFERENCES_MAX_BYTES default
_CHUNK_BYTES = 64 * 1024


# ---------------------------------------------------------------------------
# Dependency wiring (mirrors files.py / rubric_review.py — get_db_dependency
# is a factory that needs the app's session_factory)
# ---------------------------------------------------------------------------


def _session_dep(request: Request):
    factory = request.app.state.session_factory
    yield from get_db_dependency(factory)()


def _require_owner(*, workspace_id: uuid.UUID, user, session: Session) -> Workspace:
    ws = session.scalar(
        select(Workspace).where(Workspace.id == workspace_id, Workspace.soft_deleted_at.is_(None))
    )
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if ws.owner_id != user.id:
        raise HTTPException(status_code=403, detail="owner only")
    return ws


def _require_owner_or_viewer(*, workspace_id: uuid.UUID, user, session: Session) -> Workspace:
    ws = session.scalar(
        select(Workspace).where(Workspace.id == workspace_id, Workspace.soft_deleted_at.is_(None))
    )
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if ws.owner_id == user.id:
        return ws
    is_viewer = session.scalar(
        select(WorkspaceViewer.user_id).where(
            WorkspaceViewer.workspace_id == workspace_id,
            WorkspaceViewer.user_id == user.id,
        )
    )
    if is_viewer is None:
        raise HTTPException(status_code=403, detail="owner or viewer only")
    return ws


_FILENAME_OK = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_filename(name: str) -> str:
    cleaned = _FILENAME_OK.sub("_", name)
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    cleaned = cleaned.strip("._")
    return cleaned[:120] or "reference"


def _storage_for(request: Request) -> ReferenceStorage:
    return ReferenceStorage(root=Path(request.app.state.settings.workspaces_dir))


async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    buf = bytearray()
    while True:
        chunk = await upload.read(_CHUNK_BYTES)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            # Include the actual size observed so far (capped to max+1)
            # so the user knows roughly how oversize it is — "your 75 MB
            # file exceeds the 50 MB limit" is more actionable than
            # "exceeds 50 MB".
            observed_mb = len(buf) / (1024 * 1024)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"file (>{observed_mb:.1f} MB) exceeds the "
                    f"{max_bytes // (1024 * 1024)} MB upload limit"
                ),
            )
    return bytes(buf)


# ---------------------------------------------------------------------------
# GET /workspaces/<id>/references — panel page
# ---------------------------------------------------------------------------


@router.get("/{workspace_id}/references", response_class=HTMLResponse)
def references_panel(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    ws = _require_owner_or_viewer(workspace_id=workspace_id, user=user, session=session)
    refs = list(
        session.scalars(
            select(Reference)
            .where(Reference.workspace_id == workspace_id)
            .order_by(Reference.created_at.desc())
        )
    )
    is_owner = ws.owner_id == user.id
    return request.app.state.templates.TemplateResponse(
        request,
        "proposal_assist/workspace_references.html",
        {
            "user": user,
            "workspace": ws,
            "references": refs,
            "is_owner": is_owner,
        },
    )


# ---------------------------------------------------------------------------
# POST /workspaces/<id>/references — upload (two-phase ingest)
# ---------------------------------------------------------------------------


@router.post("/{workspace_id}/references")
async def upload_reference(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
    kind: str = Form("paper"),
    file: UploadFile = File(...),  # noqa: B008
) -> HTMLResponse:
    ws = _require_owner(workspace_id=workspace_id, user=user, session=session)

    content_type = (file.content_type or "").lower()
    if content_type not in _ALLOWED_MIMES:
        return HTMLResponse(
            f"<p>Unsupported content_type: {_html.escape(repr(content_type))}</p>",
            status_code=400,
        )
    if kind not in _ALLOWED_KINDS:
        return HTMLResponse(
            f"<p>Invalid kind: {_html.escape(repr(kind))}</p>",
            status_code=400,
        )

    try:
        data = await _read_capped(file, _MAX_BYTES)
    except HTTPException as e:
        return HTMLResponse(f"<p>{_html.escape(str(e.detail))}</p>", status_code=e.status_code)

    storage = _storage_for(request)
    embed_client = request.app.state.embed_client  # OllamaEmbedClient from main.py
    embedder = OllamaEmbedderAdapter(embed_client)

    try:
        # ingest_bytes runs Phase 1 (atomic write + INSERT) using the
        # caller's session, then Phase 2 (Stage 1 extract+chunk+embed) on
        # fresh internal sessions. Phase 2 is retain-on-failure: on
        # extract/embed errors the row + bytes stay on disk with
        # text_status='failed'.
        result: IngestResult = ingest_bytes(
            session_factory=request.app.state.session_factory,
            session=session,
            storage=storage,
            embedder=embedder,
            workspace_id=ws.id,
            kind=kind,
            source_type="uploaded",
            source_uri=None,
            filename=_sanitize_filename(
                file.filename or f"reference{suffix_for_mime(content_type)}"
            ),
            mime_type=content_type,
            data=data,
            uploaded_by=user.id,
        )
    except OSError as e:
        logger.warning(
            "upload_reference: write_atomic failed for workspace=%s: %s",
            ws.id,
            e,
        )
        session.rollback()
        return HTMLResponse("<p>storage write failed</p>", status_code=500)
    except ValueError as e:
        session.rollback()
        return HTMLResponse(f"<p>{_html.escape(str(e))}</p>", status_code=400)

    # Phase 2 has already run on its own sessions. Re-read the row from a
    # fresh session so we see the final text_status/text_error written by
    # Phase 2 (the caller's `session` may still hold a stale snapshot).
    factory = request.app.state.session_factory
    with factory() as s2:
        ref = s2.scalar(select(Reference).where(Reference.id == result.reference_id))
        templates = request.app.state.templates
        return templates.TemplateResponse(
            request,
            "proposal_assist/partials/reference_row.html",
            {"ref": ref, "workspace": ws, "is_owner": True},
        )


# ---------------------------------------------------------------------------
# POST /workspaces/<id>/references/fetch — fetch by DOI / PMID / arXiv / URL
# ---------------------------------------------------------------------------


@router.post("/{workspace_id}/references/fetch")
def fetch_reference_route(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
    input: str = Form(...),  # noqa: A002
    kind: str = Form("paper"),
) -> HTMLResponse:
    """User-facing counterpart to the agent's fetch_reference MCP
    tool. Owner-only; runs the same dispatcher; reuses ingest_bytes."""
    from call_manager.proposal_assist.references.dispatcher import dispatch_fetch
    from call_manager.proposal_assist.references.fetcher import (
        FetchError,
        SSRFError,
    )

    ws = _require_owner(workspace_id=workspace_id, user=user, session=session)
    if kind not in _ALLOWED_KINDS:
        return HTMLResponse(
            f"<p>Invalid kind: {_html.escape(repr(kind))}</p>",
            status_code=400,
        )

    try:
        data, filename, mime_type, source_meta = dispatch_fetch(input)
    except ValueError as e:
        # detect_source_type rejected the input.
        return HTMLResponse(
            f"<p>{_html.escape(str(e))}</p>",
            status_code=400,
        )
    except SSRFError as e:
        return HTMLResponse(
            f"<p>{_html.escape(str(e))}</p>",
            status_code=400,
        )
    except FetchError as e:
        return HTMLResponse(
            f"<p>fetch failed: {_html.escape(str(e))}</p>",
            status_code=502,
        )

    storage = _storage_for(request)
    embed_client = request.app.state.embed_client
    embedder = OllamaEmbedderAdapter(embed_client)

    try:
        result: IngestResult = ingest_bytes(
            session_factory=request.app.state.session_factory,
            session=session,
            storage=storage,
            embedder=embedder,
            workspace_id=ws.id,
            kind=kind,
            source_type=source_meta["source"],
            source_uri=input,
            filename=_sanitize_filename(filename),
            mime_type=mime_type,
            data=data,
            uploaded_by=user.id,
        )
        session.commit()
    except OSError as e:
        logger.warning(
            "fetch_reference_route: write_atomic failed for workspace=%s: %s",
            ws.id,
            e,
        )
        session.rollback()
        return HTMLResponse("<p>storage write failed</p>", status_code=500)
    except ValueError as e:
        session.rollback()
        return HTMLResponse(f"<p>{_html.escape(str(e))}</p>", status_code=400)

    # Reload via fresh session to render the final (post-Phase-2) state.
    factory = request.app.state.session_factory
    with factory() as s2:
        ref = s2.scalar(select(Reference).where(Reference.id == result.reference_id))
        templates = request.app.state.templates
        return templates.TemplateResponse(
            request,
            "proposal_assist/partials/reference_row.html",
            {"ref": ref, "workspace": ws, "is_owner": True},
        )


# ---------------------------------------------------------------------------
# GET /workspaces/<id>/references/<id>/raw — serve original bytes
# ---------------------------------------------------------------------------


@router.get("/{workspace_id}/references/{reference_id}/raw")
def serve_reference_raw(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> Response:
    ws = _require_owner_or_viewer(workspace_id=workspace_id, user=user, session=session)
    ref = session.scalar(select(Reference).where(Reference.id == reference_id))
    if ref is None or ref.workspace_id != ws.id:
        # Don't leak existence — 404 for cross-workspace lookups too.
        raise HTTPException(status_code=404, detail="reference not found")

    storage = _storage_for(request)
    suffix = suffix_for_mime(ref.mime_type)
    path = storage.path_for(workspace_id=ws.id, reference_id=ref.id, suffix=suffix)
    if not path.exists():
        raise HTTPException(status_code=404, detail="reference bytes missing")

    safe_name = _sanitize_filename(ref.filename)
    # Stream the bytes (FileResponse sets Content-Length + chunks) instead
    # of reading the whole file into memory. The hardening headers below
    # (force-download Content-Disposition, nosniff) are preserved verbatim.
    return FileResponse(
        path,
        media_type=ref.mime_type,
        headers={
            # Force download — references serve a wide MIME range
            # (HTML/SVG/etc would otherwise be a same-origin XSS vector).
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=300",
        },
    )
