"""B6 workspace web UX — list, create, main two-pane view, settings,
turn submission + SSE bridge, viewer mgmt, disclosure modal, export.

All routes mount at /workspaces. Mirrors the proposal_assist router
pattern from rubric_review.py (same _session_dep, same auth dep).

Rev2 / Codex M2: uses `request.app.state.templates` (set up by
main.py) — NOT a fresh Jinja2Templates instance. This is what keeps
the navigation in base.html working (base.html expects `user` in the
context, and main.py registers the `display_dt` / `criterion_label`
/ ... filters on that single instance).
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import uuid
from pathlib import Path

import bleach
import markdown
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import (
    Attachment,
    ChatTurn,
    TurnJob,
    User,
    Workspace,
    WorkspaceEvent,
    WorkspaceViewer,
)
from call_manager.db.session import get_db_dependency
from call_manager.proposal_assist.attachments.storage import AttachmentStorage
from call_manager.proposal_assist.attachments.tokens import estimate_image_tokens
from call_manager.proposal_assist.tools.history import (
    diff_artifact_impl,
    get_artifact_history_impl,
    read_artifact_at_impl,
)
from call_manager.proposal_assist.tools.proposal import read_proposal_impl
from call_manager.proposal_assist.tools.registry import ToolContext
from call_manager.proposal_assist.turns.caps import TokenCapExceeded
from call_manager.proposal_assist.turns.submit import submit_user_turn
from call_manager.proposal_assist.web.files import (
    _ALLOWED_MIMES,
    _MAX_BYTES,
    _PIL_FORMAT_TO_MIME,
    _SUFFIX_BY_MIME,
    _probe_image_format,
    _read_capped,
    _sanitize_filename,
)
from call_manager.workspaces import create_workspace as _create_workspace

# Rev2 / Codex H2: sanitize agent-controlled markdown before render.
_ALLOWED_TAGS = [
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "br",
    "hr",
    "strong",
    "em",
    "code",
    "pre",
    "ul",
    "ol",
    "li",
    "blockquote",
    "a",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
]
_ALLOWED_ATTRS = {"a": ["href", "title"]}


def _render_proposal_markdown(raw: str) -> str:
    """Render proposal.md content to safe HTML.

    H2 fix: markdown.markdown() emits raw HTML for any raw HTML in the
    input; bleach.clean strips tags not in the allowlist + dangerous
    attributes. Output is then trusted to render via `| safe` in the
    template.
    """
    html = markdown.markdown(raw, extensions=["extra"])
    return bleach.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        strip=True,
    )


_CHAT_TURN_PARTIAL = "proposal_assist/partials/chat_turn.html"


def _default_templates() -> Jinja2Templates:
    """Construct a Jinja2Templates rooted at the same TEMPLATES_DIR
    used by main.py. Cached after first call so tests that exercise
    _render_chat_turn_html without an app context don't pay the cost
    twice.
    """
    global _DEFAULT_TEMPLATES
    if _DEFAULT_TEMPLATES is None:
        # Import here (not at module top) to avoid a hard dependency on
        # the main module's import order; main.py itself imports this
        # module.
        from call_manager.main import TEMPLATES_DIR

        _DEFAULT_TEMPLATES = Jinja2Templates(directory=str(TEMPLATES_DIR))
    return _DEFAULT_TEMPLATES


_DEFAULT_TEMPLATES: Jinja2Templates | None = None


def _render_chat_turn_html(
    turn: ChatTurn,
    *,
    templates: Jinja2Templates | None = None,
) -> str:
    """Render one chat_turns row to a small HTML snippet for SSE emission.

    Delegates to ``partials/chat_turn.html`` — the single source of truth
    for chat-turn HTML (also used by workspace_main.html's history loop
    and post_turn's immediate echo).

    Rev2 / Codex H2: Jinja's default auto-escape escapes text content +
    tool names + image attachment ids, so chat content cannot inject
    HTML. The XSS regression tests in test_web_workspaces_sse.py assert
    this remains true.

    B7 / T4: image blocks in list-of-blocks content render as <img>
    thumbnails. Per-block dispatch lives in the partial.
    """
    engine = templates if templates is not None else _default_templates()
    rendered = engine.env.get_template(_CHAT_TURN_PARTIAL).render(turn=turn)
    # SSE `data:` lines cannot contain raw newlines (the protocol
    # uses LF as a record separator). Collapse them — the partial uses
    # ``{%- ... -%}`` to keep this no-op for normal content.
    return rendered.replace("\n", "")


# Status values that terminate the SSE stream. Shared so a future
# schema addition (e.g. `'orphaned'`) gets surfaced as a hang risk
# at this single point rather than silently breaking the stream.
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


async def _stream_turn_generator(
    *,
    session_factory,
    workspace_id: uuid.UUID,
    turn_job_id: uuid.UUID,
    poll_seconds: float = 0.2,
    request: Request | None = None,
    templates: Jinja2Templates | None = None,
):
    """Async generator that yields SSE-formatted strings.

    Extracted from the route handler so tests can drive it directly
    (skipping the HTTP layer that's hard to interleave with background
    writes in a single test).

    B6-residuals: when `request` is provided, the generator checks
    `request.is_disconnected()` each poll and exits cleanly if the
    consumer's TCP connection went away. Without this, a closed browser
    tab leaves the loop polling the DB at `poll_seconds` cadence until
    the turn_job reaches a terminal status — accumulating dead loops
    indefinitely under viewer fan-out. Tests pass `request=None` to
    keep the unit test deterministic.

    B7 / T4: ``templates`` is forwarded to _render_chat_turn_html so
    the SSE renderer uses the same partial as the chat-history loop.
    When omitted, _render_chat_turn_html falls back to a default
    Jinja2Templates instance rooted at the project's TEMPLATES_DIR.
    """
    # Seed last_seen_position at the user_turn's position (so we don't
    # echo the user turn back — it was already shown by the POST handler).
    with session_factory() as s:
        job = s.get(TurnJob, turn_job_id)
        if job is None or job.workspace_id != workspace_id:
            yield "event: done\ndata: missing\n\n"
            return
        last_seen_position = (
            s.scalar(select(ChatTurn.position).where(ChatTurn.id == job.user_turn_id)) or 0
        )

    while True:
        if request is not None and await request.is_disconnected():
            return
        with session_factory() as s:
            new_rows = list(
                s.scalars(
                    select(ChatTurn)
                    .where(
                        ChatTurn.workspace_id == workspace_id,
                        ChatTurn.position > last_seen_position,
                    )
                    .order_by(ChatTurn.position)
                )
            )
            for turn in new_rows:
                rendered = _render_chat_turn_html(turn, templates=templates)
                yield f"data: {rendered}\n\n"
                last_seen_position = turn.position
            tj = s.get(TurnJob, turn_job_id)
            if tj.status in TERMINAL_STATUSES:
                yield f"event: done\ndata: {tj.status}\n\n"
                return
        await asyncio.sleep(poll_seconds)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def _session_dep(request: Request):
    factory = request.app.state.session_factory
    yield from get_db_dependency(factory)()


def _load_workspace_or_404(
    workspace_id: uuid.UUID,
    session: Session,
) -> Workspace:
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
        session.query(WorkspaceViewer)
        .filter_by(
            workspace_id=workspace_id,
            user_id=user.id,
        )
        .first()
        is not None
    )
    if not is_viewer:
        raise HTTPException(
            status_code=403,
            detail="you do not have access to this workspace",
        )
    return ws


@router.get("/new", response_class=HTMLResponse)
def get_new_workspace_form(
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
    call_id: int | None = None,
    profile_id: int | None = None,
) -> HTMLResponse:
    """New-workspace form. Optional ?call_id= and ?profile_id= pre-fill
    the form fields (used by the /calls/{id} 'Start drafting' button)."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "proposal_assist/workspace_new.html",
        {"prefill_call_id": call_id, "prefill_profile_id": profile_id, "user": user},
    )


@router.post("/new")
def post_new_workspace(
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
    title: str = Form(...),
    call_id: int | None = Form(None),
    profile_id: int | None = Form(None),
    deadline: str | None = Form(None),
) -> RedirectResponse:
    """Create + enqueue Phase 0 + redirect to the new workspace's main view."""
    from datetime import datetime

    parsed_deadline = None
    if deadline:
        parsed_deadline = datetime.fromisoformat(deadline)

    ws = _create_workspace(
        session=session,
        owner_id=user.id,
        title=title,
        call_id=call_id,
        profile_id=profile_id,
    )
    if parsed_deadline:
        ws.deadline = parsed_deadline
    session.commit()
    return RedirectResponse(url=f"/workspaces/{ws.id}", status_code=303)


@router.get("", response_class=HTMLResponse)
def list_workspaces(
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
    status: str | None = None,
) -> HTMLResponse:
    """List workspaces visible to the user: owned OR viewer-of, not
    soft-deleted. Optional ?status=drafting|submitted|... filter."""
    viewer_ids = session.scalars(
        select(WorkspaceViewer.workspace_id).where(
            WorkspaceViewer.user_id == user.id,
        )
    ).all()
    q = (
        select(Workspace)
        .where(
            Workspace.soft_deleted_at.is_(None),
            or_(
                Workspace.owner_id == user.id,
                Workspace.id.in_(viewer_ids),
            ),
        )
        .order_by(Workspace.updated_at.desc())
    )
    if status:
        q = q.where(Workspace.status == status)
    workspaces = list(session.scalars(q))
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "proposal_assist/workspaces_index.html",
        {"workspaces": workspaces, "status_filter": status, "user": user},
    )


@router.get("/{workspace_id}", response_class=HTMLResponse)
def get_workspace_main(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    """Two-pane main view: chat history (left) + sanitized markdown artifact (right).

    Uses `request.app.state.templates` (the existing instance with registered
    filters + base.html nav). Context key is `user` (NOT `current_user`) to
    match base.html.  artifact_rendered is bleach-cleaned by
    _render_proposal_markdown before it reaches the template (rev2 / H2).
    """
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    chat_turns = list(
        session.scalars(
            select(ChatTurn)
            .where(ChatTurn.workspace_id == workspace_id)
            .order_by(ChatTurn.position)
        )
    )

    # B7: list of workspace-scoped attachments for the composer picker.
    # Ordered newest-first so the most-recently uploaded image surfaces
    # next to the file input. Viewers also see this list (read-only) so
    # the chat-image rendering Task 4 will produce can resolve the
    # ``<img src=>`` references without a separate API round-trip.
    workspace_attachments = list(
        session.scalars(
            select(Attachment)
            .where(Attachment.workspace_id == workspace_id)
            .order_by(Attachment.uploaded_at.desc())
        )
    )

    # Per-user, per-workspace disclosure check.
    prior_ack = session.scalar(
        select(WorkspaceEvent.id)
        .where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.kind == "disclosure_acknowledged",
            WorkspaceEvent.actor_id == user.id,
        )
        .limit(1)
    )
    needs_disclosure = prior_ack is None

    settings = request.app.state.settings
    repo_dir = Path(settings.workspaces_dir) / str(workspace_id) / "repo"
    proposal_path = repo_dir / "proposal.md"
    if proposal_path.exists():
        raw = proposal_path.read_text()
        rendered = _render_proposal_markdown(raw)
    else:
        raw, rendered = "", "<em>(no proposal.md yet)</em>"

    is_owner = ws.owner_id == user.id
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "proposal_assist/workspace_main.html",
        {
            "workspace": ws,
            "user": user,
            "is_owner": is_owner,
            "chat_turns": chat_turns,
            "artifact_rendered": rendered,
            "artifact_raw": raw,
            "needs_disclosure": needs_disclosure,
            "workspace_attachments": workspace_attachments,
        },
    )


@router.post("/{workspace_id}/disclosure")
def post_disclosure(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    """Record that the current user has read + acknowledged the data
    disclosure for this workspace. Owner OR viewer can call this
    (each user has their own ack). Idempotent: multiple POSTs simply
    write multiple audit rows."""
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    session.add(
        WorkspaceEvent(
            workspace_id=ws.id,
            kind="disclosure_acknowledged",
            actor_id=user.id,
            payload={},
        )
    )
    session.commit()
    # Empty body — HTMX swaps the modal away via outerHTML on the
    # modal div itself.
    return HTMLResponse("")


_MAX_TURN_ATTACHMENTS = 5


@router.post("/{workspace_id}/turns")
async def post_turn(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
    content: str = Form(...),
    attachment_ids: list[uuid.UUID] = Form(default_factory=list),  # noqa: B008
    files: list[UploadFile] = File(default_factory=list),  # noqa: B008
) -> HTMLResponse:
    """Enqueue a user turn and return TWO HTML fragments (H1):
    1. The rendered user-turn row (immediate feedback in #chat-history).
    2. An SSE-connector div that opens EventSource to the stream endpoint
       and self-removes when the stream emits the 'done' event.

    The composer form uses hx-target='#chat-history' hx-swap='beforeend',
    so both fragments are appended to the chat div in one swap.

    Returns 403 for non-owners (shared-read, single-write spec).
    Returns 429 when the daily token cap is exceeded.

    B7: accepts multipart with two optional fields layered on top of the
    legacy ``content`` text:

      * ``files`` — fresh uploads, each one validated + persisted via
        the same atomic ordering as ``POST /files``. Any per-upload
        failure (oversize, bad MIME, etc.) ROLLS BACK every prior
        upload from this same request (DB row + on-disk file) before
        returning 400 — no half-committed state.
      * ``attachment_ids`` — references to pre-existing rows from this
        workspace's files panel. Cross-workspace IDs raise 400 via
        :func:`submit_user_turn`.

    The combined attachment count is capped at 5; overshooting returns
    400 with all just-uploaded files cleaned up.
    """
    ws = _require_owner(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    settings = request.app.state.settings
    storage = AttachmentStorage(root=Path(settings.workspaces_dir))

    # Track (workspace_id, attachment_id, suffix) for any files we
    # persisted during THIS request so we can roll them back atomically
    # if a later step (another upload, the submit call) fails. Plan-
    # critical: if any new-file upload fails we MUST delete every
    # already-written prior upload, not leave half a turn's worth on
    # disk.
    created_uploads: list[tuple[uuid.UUID, uuid.UUID, str]] = []

    def _rollback_created_uploads() -> None:
        # Best-effort cleanup; the surrounding session.rollback drops the
        # rows, so a failed unlink only leaves an orphan file (no broken
        # DB pointer). Log and continue — never let a cleanup error mask
        # the original failure.
        for ws_id, aid, suf in created_uploads:
            try:
                storage.delete_one(
                    workspace_id=ws_id,
                    attachment_id=aid,
                    suffix=suf,
                )
            except OSError:
                logger.warning(
                    "post_turn: orphan cleanup failed for attachment %s",
                    aid,
                    exc_info=True,
                )

    # --- Inline upload branch (same ordering as files.py:upload_file) ---
    real_files = [f for f in files if f and f.filename]
    new_upload_ids: list[uuid.UUID] = []
    for upload in real_files:
        content_type = (upload.content_type or "").lower()
        if content_type not in _ALLOWED_MIMES:
            session.rollback()
            _rollback_created_uploads()
            return HTMLResponse(
                f"<p>Unsupported content_type: {content_type!r}</p>",
                status_code=400,
            )
        try:
            data = await _read_capped(upload, _MAX_BYTES)
        except HTTPException as e:
            session.rollback()
            _rollback_created_uploads()
            return HTMLResponse(f"<p>{e.detail}</p>", status_code=e.status_code)
        if not data:
            session.rollback()
            _rollback_created_uploads()
            return HTMLResponse("<p>empty upload</p>", status_code=400)
        try:
            fmt, width_px, height_px = _probe_image_format(data)
        except HTTPException as e:
            session.rollback()
            _rollback_created_uploads()
            return HTMLResponse(f"<p>{e.detail}</p>", status_code=e.status_code)
        detected_mime = _PIL_FORMAT_TO_MIME[fmt]
        if detected_mime != content_type:
            session.rollback()
            _rollback_created_uploads()
            return HTMLResponse(
                f"<p>content_type {content_type!r} does not match the actual "
                f"image format {detected_mime!r}</p>",
                status_code=400,
            )
        suffix = _SUFFIX_BY_MIME[content_type]
        aid = uuid.uuid4()
        safe_filename = _sanitize_filename(upload.filename or f"attachment{suffix}")
        relative_storage_path = f"{aid}{suffix}"
        try:
            storage.write_atomic(
                workspace_id=ws.id,
                attachment_id=aid,
                suffix=suffix,
                data=data,
            )
        except OSError as e:
            logger.warning(
                "post_turn: storage.write_atomic failed for workspace=%s: %s",
                ws.id,
                e,
            )
            session.rollback()
            _rollback_created_uploads()
            return HTMLResponse("<p>storage write failed</p>", status_code=500)
        # Record now so a downstream failure rolls back even files we
        # haven't yet inserted a row for (the row insert below could
        # still raise).
        created_uploads.append((ws.id, aid, suffix))
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
        new_upload_ids.append(aid)

    # Combine: new uploads + explicit references. Order preserved so the
    # composer's display order matches what we render in the chat row.
    all_ids: list[uuid.UUID] = new_upload_ids + list(attachment_ids)
    if len(all_ids) > _MAX_TURN_ATTACHMENTS:
        session.rollback()
        _rollback_created_uploads()
        return HTMLResponse(
            f"<p>Too many attachments: {len(all_ids)} > {_MAX_TURN_ATTACHMENTS} max per turn.</p>",
            status_code=400,
        )

    try:
        job = submit_user_turn(
            session=session,
            workspace_id=ws.id,
            content_text=content,
            attachment_ids=all_ids or None,
            submitted_by_user_id=user.id,
            settings=settings,
            workspaces_dir=Path(settings.workspaces_dir),
        )
        session.commit()
    except TokenCapExceeded as e:
        session.rollback()
        _rollback_created_uploads()
        return HTMLResponse(
            f"<p>Workspace daily token cap exceeded: {e.reason}. "
            f"Used {e.used_in} input / {e.used_out} output today; "
            f"cap is {e.cap_in} / {e.cap_out}. Try again after midnight "
            f"Europe/Stockholm or ask an admin to raise the cap.</p>",
            status_code=429,
        )
    except ValueError as e:
        session.rollback()
        _rollback_created_uploads()
        return HTMLResponse(
            f"<p>{_html.escape(str(e))}</p>",
            status_code=400,
        )

    # B7 / T4: render the just-posted user turn through the chat_turn
    # partial — the single source of truth for chat-turn HTML — so the
    # immediate echo, the SSE renderer, and the history loop all agree.
    # We build a content shape that matches what submit_user_turn just
    # persisted: list of {text} + {image} blocks when attachments are
    # present, single text dict otherwise. Jinja's auto-escape inside
    # the partial handles HTML in `content`.
    if all_ids:
        echo_content: object = [{"type": "text", "text": content}] + [
            {"type": "image", "attachment_id": str(aid)} for aid in all_ids
        ]
    else:
        echo_content = {"type": "text", "text": content}
    echo_turn = ChatTurn(
        workspace_id=ws.id,
        role="user",
        content=echo_content,  # type: ignore[arg-type]
    )
    user_turn_html = request.app.state.templates.env.get_template(
        _CHAT_TURN_PARTIAL,
    ).render(turn=echo_turn)
    sse_connector = (
        f"<div id='sse-mount-{job.id}' "
        f"hx-ext='sse' "
        f"sse-connect='/workspaces/{workspace_id}/turns/{job.id}/stream' "
        f"sse-swap='message' "
        f"sse-close='done' "
        f"hx-target='#chat-history' "
        f"hx-swap='beforeend'></div>"
    )
    return HTMLResponse(user_turn_html + sse_connector)


@router.get("/{workspace_id}/turns/{turn_job_id}/stream")
async def stream_turn(
    workspace_id: uuid.UUID,
    turn_job_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> StreamingResponse:
    """SSE bridge: polls chat_turns for new rows written by the processor
    and emits them as SSE events. Closes with 'event: done' when the
    turn_job reaches a terminal state (completed/failed/cancelled).

    Viewers may connect (read-only access is sufficient to watch the stream).
    """
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    job = session.get(TurnJob, turn_job_id)
    if job is None or job.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="turn_job not found")

    return StreamingResponse(
        _stream_turn_generator(
            session_factory=request.app.state.session_factory,
            workspace_id=ws.id,
            turn_job_id=turn_job_id,
            request=request,  # B6-residuals: enables client-disconnect detection
            templates=request.app.state.templates,
        ),
        media_type="text/event-stream",
    )


def _make_tool_ctx(request: Request, ws: Workspace) -> ToolContext:
    """Build a ToolContext bound to `ws` and this request's app state."""
    settings = request.app.state.settings
    return ToolContext(
        workspace_id=ws.id,
        repo_dir=Path(settings.workspaces_dir) / str(ws.id) / "repo",
        session_factory=request.app.state.session_factory,
        settings=settings,
    )


@router.get("/{workspace_id}/artifact/raw")
async def artifact_raw(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
):
    """Return the current proposal.md content as plain text."""
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    result = await read_proposal_impl(_make_tool_ctx(request, ws))
    if "error" in result:
        raise HTTPException(404, result["error"])
    return Response(result["content"], media_type="text/plain")


@router.get("/{workspace_id}/artifact/history", response_class=HTMLResponse)
async def artifact_history(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
):
    """Render the commit history for proposal.md as an HTML partial."""
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    result = await get_artifact_history_impl(_make_tool_ctx(request, ws), limit=50)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "proposal_assist/partials/history.html",
        {"workspace": ws, "commits": result.get("commits", []), "user": user},
    )


@router.get("/{workspace_id}/artifact/at/{commit_sha}")
async def artifact_at(
    workspace_id: uuid.UUID,
    commit_sha: str,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
):
    """Return proposal.md content at a specific commit sha."""
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    result = await read_artifact_at_impl(
        _make_tool_ctx(request, ws),
        commit_sha=commit_sha,
    )
    if "error" in result:
        raise HTTPException(400, result["error"])
    return Response(result["content"], media_type="text/plain")


@router.get("/{workspace_id}/artifact/diff")
async def artifact_diff(
    workspace_id: uuid.UUID,
    request: Request,
    commit_a: str,
    commit_b: str,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
):
    """Return a unified diff of proposal.md between two commits."""
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    result = await diff_artifact_impl(
        _make_tool_ctx(request, ws),
        commit_a=commit_a,
        commit_b=commit_b,
    )
    if "error" in result:
        raise HTTPException(400, result["error"])
    return Response(result["diff"], media_type="text/plain")


@router.get("/{workspace_id}/settings", response_class=HTMLResponse)
def get_settings(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
):
    ws = _require_owner(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    viewers = list(
        session.execute(
            select(WorkspaceViewer, User)
            .join(User, User.id == WorkspaceViewer.user_id)
            .where(WorkspaceViewer.workspace_id == ws.id)
        ).all()
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "proposal_assist/workspace_settings.html",
        {"workspace": ws, "user": user, "viewers": viewers},
    )


@router.post("/{workspace_id}/viewers")
def add_viewer(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
    email: str = Form(...),
):
    ws = _require_owner(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    target = session.scalar(select(User).where(User.email == email))
    if target is None:
        raise HTTPException(status_code=400, detail=f"no user with email {email}")
    existing = (
        session.query(WorkspaceViewer).filter_by(workspace_id=ws.id, user_id=target.id).first()
    )
    if existing is None:
        session.add(WorkspaceViewer(workspace_id=ws.id, user_id=target.id))
        session.commit()
    return RedirectResponse(
        url=f"/workspaces/{ws.id}/settings",
        status_code=303,
    )


@router.post("/{workspace_id}/viewers/{user_id}/remove")
def remove_viewer(
    workspace_id: uuid.UUID,
    user_id: int,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
):
    ws = _require_owner(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    session.query(WorkspaceViewer).filter_by(
        workspace_id=ws.id,
        user_id=user_id,
    ).delete()
    session.commit()
    return RedirectResponse(
        url=f"/workspaces/{ws.id}/settings",
        status_code=303,
    )


@router.post("/{workspace_id}/settings")
def post_settings(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
    title: str = Form(...),
    status: str = Form(...),
    deadline: str | None = Form(None),
):
    from datetime import datetime

    ws = _require_owner(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    ws.title = title
    ws.status = status
    if deadline:
        ws.deadline = datetime.fromisoformat(deadline)
    else:
        ws.deadline = None
    session.commit()
    return RedirectResponse(url=f"/workspaces/{ws.id}", status_code=303)


@router.post("/{workspace_id}/delete")
def soft_delete(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
):
    from datetime import UTC, datetime

    ws = _require_owner(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    ws.soft_deleted_at = datetime.now(UTC)
    session.commit()
    return RedirectResponse(url="/workspaces", status_code=303)


@router.get("/{workspace_id}/export.md")
def export_markdown(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
):
    """Markdown-only export. Word/PDF would require pandoc; deferred."""
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    repo_dir = Path(request.app.state.settings.workspaces_dir) / str(ws.id) / "repo"
    proposal = repo_dir / "proposal.md"
    if not proposal.exists():
        raise HTTPException(status_code=404, detail="no proposal.md yet")
    # Sanitize the filename — workspace titles can contain anything,
    # but we want a safe header value.
    safe_title = (
        "".join(c if c.isalnum() or c in "-_." else "_" for c in ws.title)[:80] or "proposal"
    )
    return Response(
        proposal.read_text(),
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_title}.md"',
        },
    )


@router.post("/{workspace_id}/turns/{turn_job_id}/cancel")
def cancel_turn(
    workspace_id: uuid.UUID,
    turn_job_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    """Mark queued turn_job as 'cancelled'. No-op if status != 'queued'.

    Race-safe: the WHERE clause includes status='queued', so a concurrent
    processor that has already moved it to 'running' wins; this handler
    returns the noop message.

    Audit: on successful cancel, writes a `cloud_turn_cancelled`
    WorkspaceEvent in the same transaction. Without this, the
    submit-time `cloud_turn_sent` row has no matching outcome event
    and the compliance chain breaks (see B6-residuals review).
    """
    from datetime import UTC, datetime

    ws = _require_owner(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    result = session.execute(
        update(TurnJob)
        .where(
            TurnJob.id == turn_job_id,
            TurnJob.workspace_id == ws.id,
            TurnJob.status == "queued",
        )
        .values(status="cancelled", completed_at=datetime.now(UTC))
    )
    if result.rowcount:
        session.add(
            WorkspaceEvent(
                workspace_id=ws.id,
                kind="cloud_turn_cancelled",
                actor_id=user.id,
                payload={
                    "turn_job_id": str(turn_job_id),
                    "tokens_in": 0,
                    "tokens_out": 0,
                },
            )
        )
    session.commit()
    if result.rowcount:
        return HTMLResponse("<p>cancelled</p>")
    return HTMLResponse("<p>too late — already started or finished</p>")
