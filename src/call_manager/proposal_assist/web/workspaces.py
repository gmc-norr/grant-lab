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
import uuid
from pathlib import Path

import bleach
import markdown
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import ChatTurn, TurnJob, Workspace, WorkspaceViewer
from call_manager.db.session import get_db_dependency
from call_manager.proposal_assist.turns.caps import TokenCapExceeded
from call_manager.proposal_assist.turns.submit import submit_user_turn
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


def _render_chat_turn_html(turn: ChatTurn) -> str:
    """Render one chat_turns row to a small HTML snippet for SSE emission.
    Matches the partials/chat_turn.html template's per-row markup.

    Rev2 / Codex H2: every interpolated field is run through
    html.escape — chat content + tool names are agent-controlled and
    could contain <script> otherwise. The XSS regression tests in
    test_web_workspaces_sse.py assert this.
    """
    c = turn.content
    safe_role = _html.escape(turn.role)
    if isinstance(c, dict) and c.get("type") == "text":
        body = f"<p>{_html.escape(str(c.get('text', '')))}</p>"
    elif isinstance(c, dict) and c.get("type") == "tool_use":
        body = f"<p><em>(tool call: {_html.escape(str(c.get('name', '?')))})</em></p>"
    elif isinstance(c, dict) and c.get("type") == "tool_result":
        err = " — error" if c.get("is_error") else ""
        body = f"<p><em>(tool result{err})</em></p>"
    else:
        body = f"<pre>{_html.escape(str(c))}</pre>"
    snippet = (
        f"<div class='chat-turn' data-role='{safe_role}'><strong>{safe_role}:</strong>{body}</div>"
    )
    # Remove newlines so the value is valid in an SSE data: line.
    return snippet.replace("\n", "")


async def _stream_turn_generator(
    *,
    session_factory,
    workspace_id: uuid.UUID,
    turn_job_id: uuid.UUID,
    poll_seconds: float = 0.2,
):
    """Async generator that yields SSE-formatted strings.

    Extracted from the route handler so tests can drive it directly
    (skipping the HTTP layer that's hard to interleave with background
    writes in a single test).
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
                rendered = _render_chat_turn_html(turn)
                yield f"data: {rendered}\n\n"
                last_seen_position = turn.position
            tj = s.get(TurnJob, turn_job_id)
            if tj.status in ("completed", "failed", "cancelled"):
                yield f"event: done\ndata: {tj.status}\n\n"
                return
        await asyncio.sleep(poll_seconds)


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
        },
    )


@router.post("/{workspace_id}/turns")
def post_turn(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
    content: str = Form(...),
) -> HTMLResponse:
    """Enqueue a user turn and return TWO HTML fragments (H1):
    1. The rendered user-turn row (immediate feedback in #chat-history).
    2. An SSE-connector div that opens EventSource to the stream endpoint
       and self-removes when the stream emits the 'done' event.

    The composer form uses hx-target='#chat-history' hx-swap='beforeend',
    so both fragments are appended to the chat div in one swap.

    Returns 403 for non-owners (shared-read, single-write spec).
    Returns 429 when the daily token cap is exceeded.
    """
    ws = _require_owner(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    settings = request.app.state.settings
    try:
        job = submit_user_turn(
            session=session,
            workspace_id=ws.id,
            content={"type": "text", "text": content},
            submitted_by_user_id=user.id,
            settings=settings,
        )
        session.commit()
    except TokenCapExceeded as e:
        session.rollback()
        return HTMLResponse(
            f"<p>Workspace daily token cap exceeded: {e.reason}. "
            f"Used {e.used_in} input / {e.used_out} output today; "
            f"cap is {e.cap_in} / {e.cap_out}. Try again after midnight "
            f"Europe/Stockholm or ask an admin to raise the cap.</p>",
            status_code=429,
        )

    # Rev2 / H2: every interpolated user value goes through html.escape —
    # never f-string'd raw into the response body.
    safe_text = _html.escape(content)
    user_turn_html = (
        f"<div class='chat-turn' data-role='user'><strong>user:</strong><p>{safe_text}</p></div>"
    )
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
        ),
        media_type="text/event-stream",
    )
