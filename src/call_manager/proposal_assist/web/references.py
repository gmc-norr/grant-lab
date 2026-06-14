"""Web routes for references — Slice 1 (panel + upload + raw serve).

Permission policy (member = owner OR a WorkspaceViewer row):
- GET  /workspaces/<id>/references                    any workspace member
- POST /workspaces/<id>/references                    any workspace member
- GET  /workspaces/<id>/references/<id>/raw           any workspace member
- GET  /workspaces/<id>/references/<id>/metadata      any workspace member
- POST /workspaces/<id>/references/<id>/metadata      any workspace member

Atomicity: mirrors B7 attachments — Phase 1 atomic, Phase 2 sync Stage 1
retain-on-failure. See ingest.py.
"""

from __future__ import annotations

import contextlib
import hashlib
import html as _html
import logging
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import Reference, WorkspaceEvent
from call_manager.db.session import get_db_dependency
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.references.admission import OverCapacity
from call_manager.proposal_assist.references.deletion import delete_or_tombstone_reference
from call_manager.proposal_assist.references.embed import OllamaEmbedderAdapter
from call_manager.proposal_assist.references.ingest import (
    IngestResult,
    ingest_bytes,
    suffix_for_mime,
)
from call_manager.proposal_assist.references.kinds import ALLOWED_KINDS
from call_manager.proposal_assist.references.storage import ReferenceStorage
from call_manager.proposal_assist.web._perms import is_member, require_member

logger = logging.getLogger(__name__)


def _caps(ws, user, session) -> dict:
    """Compute per-user capability flags for a workspace.

    ``is_owner`` gates owner-only surfaces (Settings link, future
    owner-exclusive actions).  ``can_manage_references`` is the wider
    gate that covers any workspace member (owner OR WorkspaceViewer row)
    — used by reference upload / metadata review / waiver controls.
    """
    owner = ws.owner_id == user.id
    return {
        "is_owner": owner,
        "can_manage_references": owner or is_member(workspace_id=ws.id, user=user, session=session),
    }


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
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
    refs = list(
        session.scalars(
            select(Reference)
            .where(
                Reference.workspace_id == workspace_id,
                Reference.tombstoned_at.is_(None),
            )
            .order_by(Reference.created_at.desc())
        )
    )
    settings = request.app.state.settings
    return request.app.state.templates.TemplateResponse(
        request,
        "proposal_assist/workspace_references.html",
        {
            "user": user,
            "workspace": ws,
            "references": refs,
            "reference_max_batch_files": settings.reference_max_batch_files,
            "reference_max_batch_bytes": settings.reference_max_batch_bytes,
            **_caps(ws, user, session),
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
    auto_classify: bool = Form(False),
    file: UploadFile = File(...),  # noqa: B008
) -> HTMLResponse:
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
    content_type = (file.content_type or "").lower()
    if content_type not in _ALLOWED_MIMES:
        return HTMLResponse(
            f"<p>Unsupported content_type: {_html.escape(repr(content_type))}</p>",
            status_code=400,
        )
    if not auto_classify and kind not in ALLOWED_KINDS:
        return HTMLResponse(
            f"<p>Invalid kind: {_html.escape(repr(kind))}</p>",
            status_code=400,
        )

    storage = _storage_for(request)
    embedder = OllamaEmbedderAdapter(request.app.state.embed_client)
    factory = request.app.state.session_factory
    limiter = request.app.state.reference_ingest_limiter
    settings = request.app.state.settings
    caps = _caps(ws, user, session)

    try:
        with limiter.slot():
            data = await _read_capped(file, _MAX_BYTES)
            if auto_classify and not data.startswith(b"%PDF-"):
                return HTMLResponse(
                    "<p>Drag-drop accepts real PDF files only.</p>", status_code=400
                )

            # Dedup by content hash: a re-dropped file that matches a live
            # (non-tombstoned) reference returns that row instead of creating a
            # duplicate. A 'done' match shows "already present"; a failed/in-progress
            # match returns its row so the user can recover it via Reprocess.
            sha = hashlib.sha256(data).hexdigest()
            with factory() as sdup:
                dup = sdup.scalar(
                    select(Reference)
                    .where(
                        Reference.workspace_id == ws.id,
                        Reference.original_sha256 == sha,
                        Reference.tombstoned_at.is_(None),
                    )
                    .order_by(Reference.created_at.desc())
                )
                dup_id = dup.id if dup else None
                dup_done = bool(dup and dup.text_status == "done")
            if dup_id is not None:
                with factory() as s2:
                    ref = s2.get(Reference, dup_id)
                    return request.app.state.templates.TemplateResponse(
                        request,
                        "proposal_assist/partials/reference_row.html",
                        {"ref": ref, "workspace": ws, "already_present": dup_done, **caps},
                    )
            effective_kind = "paper" if auto_classify else kind

            def _run():
                with factory() as own:  # thread-owned session, never the request session
                    return ingest_bytes(
                        session_factory=factory,
                        session=own,
                        storage=storage,
                        embedder=embedder,
                        workspace_id=ws.id,
                        kind=effective_kind,
                        source_type="uploaded",
                        source_uri=None,
                        filename=_sanitize_filename(
                            file.filename or f"reference{suffix_for_mime(content_type)}"
                        ),
                        mime_type=content_type,
                        data=data,
                        uploaded_by=user.id,
                        citation_service=request.app.state.citation_service,
                        extract_timeout_seconds=settings.reference_extract_timeout_seconds,
                        auto_classify_kind=auto_classify,
                    )

            result: IngestResult = await run_in_threadpool(_run)
    except OverCapacity:
        return HTMLResponse(
            "<p>Server busy — retry shortly.</p>",
            status_code=429,
            headers={"Retry-After": "2"},
        )
    except HTTPException as e:
        return HTMLResponse(f"<p>{_html.escape(str(e.detail))}</p>", status_code=e.status_code)
    except OSError:
        return HTMLResponse("<p>storage write failed</p>", status_code=500)
    except ValueError as e:
        return HTMLResponse(f"<p>{_html.escape(str(e))}</p>", status_code=400)

    # (Task 14 will insert the reference.added audit event here.)
    with factory() as s2:
        ref = s2.scalar(select(Reference).where(Reference.id == result.reference_id))
        return request.app.state.templates.TemplateResponse(
            request,
            "proposal_assist/partials/reference_row.html",
            {"ref": ref, "workspace": ws, **caps},
        )


# ---------------------------------------------------------------------------
# POST /workspaces/<id>/references/fetch — fetch by DOI / PMID / arXiv / URL
# ---------------------------------------------------------------------------


@router.post("/{workspace_id}/references/fetch")
async def fetch_reference_route(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
    input: str = Form(...),  # noqa: A002
    kind: str = Form("paper"),
) -> HTMLResponse:
    """User-facing counterpart to the agent's fetch_reference MCP
    tool. Member-only (owner or workspace member); runs the same dispatcher; reuses ingest_bytes."""
    from call_manager.proposal_assist.references.dispatcher import (
        detect_source_type,
        dispatch_fetch,
    )
    from call_manager.proposal_assist.references.fetcher import (
        FetchError,
        SSRFError,
    )

    ws = require_member(workspace_id=workspace_id, user=user, session=session)
    if kind not in ALLOWED_KINDS:
        return HTMLResponse(
            f"<p>Invalid kind: {_html.escape(repr(kind))}</p>",
            status_code=400,
        )

    # Validate input before acquiring the limiter slot — bad input shouldn't
    # block a slot that a valid request could use.
    try:
        detect_source_type(input)
    except ValueError as e:
        return HTMLResponse(f"<p>{_html.escape(str(e))}</p>", status_code=400)

    factory = request.app.state.session_factory
    storage = _storage_for(request)
    embedder = OllamaEmbedderAdapter(request.app.state.embed_client)
    limiter = request.app.state.reference_ingest_limiter
    settings = request.app.state.settings
    caps = _caps(ws, user, session)

    try:
        with limiter.slot():

            def _run():
                # The fetch (network I/O + DNS) and ingest are both
                # blocking; run them together in the thread so the event
                # loop is free while we wait.
                try:
                    fetch_data, filename, mime_type, source_meta = dispatch_fetch(input)
                except (ValueError, SSRFError, FetchError):
                    raise

                with factory() as own:
                    result = ingest_bytes(
                        session_factory=factory,
                        session=own,
                        storage=storage,
                        embedder=embedder,
                        workspace_id=ws.id,
                        kind=kind,
                        source_type=source_meta["source"],
                        source_uri=input,
                        filename=_sanitize_filename(filename),
                        mime_type=mime_type,
                        data=fetch_data,
                        uploaded_by=user.id,
                        source_meta=source_meta,
                        citation_service=request.app.state.citation_service,
                        extract_timeout_seconds=settings.reference_extract_timeout_seconds,
                    )
                return result

            result: IngestResult = await run_in_threadpool(_run)
    except OverCapacity:
        return HTMLResponse(
            "<p>Server busy — retry shortly.</p>",
            status_code=429,
            headers={"Retry-After": "2"},
        )
    except ValueError as e:
        # detect_source_type rejected the input (re-raised from _run).
        return HTMLResponse(f"<p>{_html.escape(str(e))}</p>", status_code=400)
    except SSRFError as e:
        return HTMLResponse(f"<p>{_html.escape(str(e))}</p>", status_code=400)
    except FetchError as e:
        return HTMLResponse(f"<p>fetch failed: {_html.escape(str(e))}</p>", status_code=502)
    except OSError:
        return HTMLResponse("<p>storage write failed</p>", status_code=500)

    # Reload via fresh session to render the final (post-Phase-2) state.
    with factory() as s2:
        ref = s2.scalar(select(Reference).where(Reference.id == result.reference_id))
        return request.app.state.templates.TemplateResponse(
            request,
            "proposal_assist/partials/reference_row.html",
            {"ref": ref, "workspace": ws, **caps},
        )


# ---------------------------------------------------------------------------
# GET  /workspaces/<id>/references/<id>/metadata — member metadata review form
# POST /workspaces/<id>/references/<id>/metadata — member metadata approve/correct
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
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
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
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
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
# POST /workspaces/<id>/references/<id>/waiver — a member overrides the metadata
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
    """Set or clear a reference's metadata waiver (any workspace member).

    The waiver lets any member deliberately override the fail-closed metadata gate
    for ONE reference so build_bibliography / composed export can cite it without
    full metadata approval. The change is audited via a WorkspaceEvent. Returns
    the updated reference-row partial for an HTMX swap.
    """
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
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
        {"ref": ref, "workspace": ws, **_caps(ws, user, session)},
    )


# ---------------------------------------------------------------------------
# PATCH /workspaces/<id>/references/<id>/kind — member-gated kind correction
# ---------------------------------------------------------------------------


@router.patch("/{workspace_id}/references/{reference_id}/kind", response_class=HTMLResponse)
def patch_reference_kind(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    request: Request,
    kind: str = Form(...),
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
    if kind not in ALLOWED_KINDS:
        return HTMLResponse(f"<p>Invalid kind: {_html.escape(repr(kind))}</p>", status_code=400)
    ref = session.scalar(select(Reference).where(Reference.id == reference_id))
    if ref is None or ref.workspace_id != ws.id or ref.tombstoned_at is not None:
        raise HTTPException(status_code=404, detail="reference not found")
    old = ref.kind
    ref.kind = kind
    session.add(
        WorkspaceEvent(
            workspace_id=ws.id,
            kind="reference.kind_changed",
            actor_id=user.id,
            payload={"reference_id": str(ref.id), "from": old, "to": kind},
        )
    )
    session.commit()
    return request.app.state.templates.TemplateResponse(
        request,
        "proposal_assist/partials/reference_row.html",
        {"ref": ref, "workspace": ws, **_caps(ws, user, session)},
    )


# ---------------------------------------------------------------------------
# POST /workspaces/<id>/references/<id>/delete — member-gated delete/tombstone
# ---------------------------------------------------------------------------


@router.post("/{workspace_id}/references/{reference_id}/delete", response_class=HTMLResponse)
def delete_reference_route(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
    storage = _storage_for(request)
    repo_dir = workspace_repo_dir(request.app.state.settings.workspaces_dir, ws.id)
    try:
        outcome = delete_or_tombstone_reference(
            session=session,
            repo_dir=repo_dir,
            reference_id=reference_id,
            workspace_id=ws.id,
            storage=storage,
            actor_id=user.id,
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="reference not found") from None
    if outcome == "tombstoned":
        # Row removed (empty main content); OOB toast explains retention.
        return HTMLResponse(
            '<span id="ref-toast" hx-swap-oob="true">'
            "Reference deleted (retained because it is cited).</span>"
        )
    return HTMLResponse("")  # hard delete → empty body removes the row


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
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
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
