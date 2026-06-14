"""Web routes for references — Slice 1 (panel + upload + raw serve).

Permission policy:
- GET  /workspaces/<id>/references                    owner OR viewer
- POST /workspaces/<id>/references                    owner only
- GET  /workspaces/<id>/references/<id>/raw           owner OR viewer
- GET  /workspaces/<id>/references/<id>/metadata      owner only
- POST /workspaces/<id>/references/<id>/metadata      owner only

Atomicity: mirrors B7 attachments — Phase 1 atomic, Phase 2 sync Stage 1
retain-on-failure. See ingest.py.
"""

from __future__ import annotations

import contextlib
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
from call_manager.db.models import Reference, Workspace, WorkspaceEvent, WorkspaceViewer
from call_manager.db.session import get_db_dependency
from call_manager.proposal_assist.references.embed import OllamaEmbedderAdapter
from call_manager.proposal_assist.references.ingest import (
    IngestResult,
    ingest_bytes,
    suffix_for_mime,
)
from call_manager.proposal_assist.references.kinds import ALLOWED_KINDS
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
    if kind not in ALLOWED_KINDS:
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
        # Phase 3 (metadata extraction) runs when citation_service is
        # provided; uploads have no fetcher-supplied source_meta.
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
            citation_service=request.app.state.citation_service,
            extract_timeout_seconds=request.app.state.settings.reference_extract_timeout_seconds,
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
    if kind not in ALLOWED_KINDS:
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
            source_meta=source_meta,
            citation_service=request.app.state.citation_service,
            extract_timeout_seconds=request.app.state.settings.reference_extract_timeout_seconds,
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
# GET  /workspaces/<id>/references/<id>/metadata — owner metadata review form
# POST /workspaces/<id>/references/<id>/metadata — owner metadata approve/correct
# ---------------------------------------------------------------------------


def _authors_to_lines(authors: list) -> str:
    """Convert a CSL author list to a newline-separated string for the textarea.

    Encoding rules (round-trip safe with ``_parse_authors``):
    - family + given  → "family, given"
    - family only     → "family,"  (trailing comma marks it as structured)
    - literal only    → literal string as-is
    """
    lines = []
    for a in authors:
        if isinstance(a, dict):
            family = a.get("family", "")
            given = a.get("given", "")
            if family:
                lines.append(f"{family}, {given}" if given else f"{family},")
            elif a.get("literal"):
                lines.append(a["literal"])
        else:
            lines.append(str(a))
    return "\n".join(lines)


def _parse_authors(text: str) -> list[dict]:
    """Parse a newline-separated author string into a CSL author list.

    Decoding rules (inverse of ``_authors_to_lines``):
    - Lines containing a comma are split on the FIRST comma into family/given.
      ``given`` is only included in the dict when it is non-empty, so
      ``"WHO,"`` → ``{"family": "WHO"}`` and ``"Roe, Jane"`` →
      ``{"family": "Roe", "given": "Jane"}``.
    - Lines with no comma → ``{"literal": line}``.
    - Blank lines are skipped.
    """
    authors = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "," in line:
            family, _, given = line.partition(",")
            family = family.strip()
            given = given.strip()
            entry: dict = {"family": family}
            if given:
                entry["given"] = given
            authors.append(entry)
        else:
            authors.append({"literal": line})
    return authors


@router.get("/{workspace_id}/references/{reference_id}/metadata", response_class=HTMLResponse)
def get_reference_metadata(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    ws = _require_owner(workspace_id=workspace_id, user=user, session=session)
    ref = session.scalar(select(Reference).where(Reference.id == reference_id))
    if ref is None or ref.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="reference not found")

    csl: dict = ref.csl_json or ref.csl_json_extracted or {}

    # Derive display values from the best-available CSL.
    title = csl.get("title", "")
    authors_text = _authors_to_lines(csl.get("author") or [])
    issued = csl.get("issued") or {}
    date_parts = issued.get("date-parts") or []
    year = str(date_parts[0][0]) if date_parts and date_parts[0] else ""
    venue = csl.get("container-title", "")
    doi = csl.get("DOI", "")

    return request.app.state.templates.TemplateResponse(
        request,
        "proposal_assist/reference_metadata_review.html",
        {
            "user": user,
            "workspace": ws,
            "ref": ref,
            "title": title,
            "authors_text": authors_text,
            "year": year,
            "venue": venue,
            "doi": doi,
        },
    )


@router.post("/{workspace_id}/references/{reference_id}/metadata")
async def post_reference_metadata(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> Response:
    ws = _require_owner(workspace_id=workspace_id, user=user, session=session)
    ref = session.scalar(select(Reference).where(Reference.id == reference_id))
    if ref is None or ref.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="reference not found")

    form = dict(await request.form())

    csl: dict = {}

    title = (form.get("title") or "").strip()
    if title:
        csl["title"] = title

    authors_text = form.get("authors") or ""
    parsed_authors = _parse_authors(authors_text)
    if parsed_authors:
        csl["author"] = parsed_authors

    year_raw = (form.get("year") or "").strip()
    if year_raw:
        with contextlib.suppress(ValueError):
            csl["issued"] = {"date-parts": [[int(year_raw)]]}

    venue = (form.get("venue") or "").strip()
    if venue:
        csl["container-title"] = venue

    doi = (form.get("doi") or "").strip()
    if doi:
        csl["DOI"] = doi

    # FIX 1: reject empty approvals — require at least a title.
    if not csl or not csl.get("title"):
        raise HTTPException(
            status_code=400,
            detail="Provide at least a title before approving.",
        )

    ref.csl_json = csl
    if isinstance(csl.get("title"), str) and csl["title"]:
        ref.title = csl["title"]
    ref.metadata_status = "approved"

    # FIX 2: upgrade a fallback citekey to a human-readable one on approve.
    if ref.citekey is None or ref.citekey.startswith("ref-"):
        from call_manager.proposal_assist.references.csl import generate_citekey

        new_key = generate_citekey(csl, ws.id, session)
        if new_key and not new_key.startswith("ref-"):
            ref.citekey = new_key

    from sqlalchemy.exc import IntegrityError

    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        # Re-fetch to get a clean state, then re-apply everything except citekey.
        ref = session.scalar(select(Reference).where(Reference.id == reference_id))
        ref.csl_json = csl
        if isinstance(csl.get("title"), str) and csl["title"]:
            ref.title = csl["title"]
        ref.metadata_status = "approved"
        session.commit()

    target = f"/workspaces/{ws.id}/references"
    return Response(status_code=303, headers={"Location": target})


# ---------------------------------------------------------------------------
# POST /workspaces/<id>/references/<id>/waiver — owner overrides the metadata
# gate so an unapproved reference can still be cited in the bibliography/export.
# ---------------------------------------------------------------------------


_WAIVER_TRUE = frozenset({"true", "1", "on", "yes"})
_MAX_WAIVER_REASON_LEN = 500


@router.post("/{workspace_id}/references/{reference_id}/waiver", response_class=HTMLResponse)
def post_reference_waiver(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    request: Request,
    waiver: str = Form(...),
    reason: str = Form(""),
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    """Set or clear a reference's metadata waiver (owner only).

    The waiver lets the owner deliberately override the fail-closed metadata gate
    for ONE reference so build_bibliography / composed export can cite it without
    full metadata approval. The change is audited via a WorkspaceEvent. Returns
    the updated reference-row partial for an HTMX swap.
    """
    ws = _require_owner(workspace_id=workspace_id, user=user, session=session)
    ref = session.scalar(select(Reference).where(Reference.id == reference_id))
    if ref is None or ref.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="reference not found")

    new_state = waiver.strip().lower() in _WAIVER_TRUE
    ref.metadata_waiver = new_state
    # Cap the reason so a stray multi-MB paste can't bloat the row + audit payload.
    ref.metadata_waiver_reason = (
        (reason.strip()[:_MAX_WAIVER_REASON_LEN] or None) if new_state else None
    )

    session.add(
        WorkspaceEvent(
            workspace_id=ws.id,
            kind="reference_metadata_waived" if new_state else "reference_metadata_waiver_cleared",
            actor_id=user.id,
            payload={
                "reference_id": str(ref.id),
                "waiver": new_state,
                "reason": ref.metadata_waiver_reason,
            },
        )
    )
    session.commit()

    return request.app.state.templates.TemplateResponse(
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
