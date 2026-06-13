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
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import (
    Attachment,
    ChatTurn,
    Reference,
    TurnJob,
    User,
    Workspace,
    WorkspaceEvent,
    WorkspaceSetupJob,
    WorkspaceViewer,
)
from call_manager.db.session import get_db_dependency
from call_manager.proposal_assist.attachments.storage import AttachmentStorage
from call_manager.proposal_assist.attachments.tokens import estimate_image_tokens
from call_manager.proposal_assist.citations.bibliography import CITATION_STYLES
from call_manager.proposal_assist.drafting.locking import BUSY_ERROR_MESSAGE, CommitMutexTimeout
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.references.storage import ReferenceStorage
from call_manager.proposal_assist.tools.history import (
    diff_artifact_impl,
    get_artifact_history_impl,
    read_artifact_at_impl,
)
from call_manager.proposal_assist.tools.proposal import read_proposal_impl
from call_manager.proposal_assist.tools.registry import ToolContext
from call_manager.proposal_assist.turns.caps import GlobalTokenCapExceeded, TokenCapExceeded
from call_manager.proposal_assist.turns.outcomes import record_terminal_outcome
from call_manager.proposal_assist.turns.submit import (
    MAX_TURN_INCLUSIONS,
    submit_user_turn,
)
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

# Valid Workspace.status values (mirrors the ck_workspaces_status CHECK
# constraint in db/models.py). Used to allowlist the ?status filter.
_WORKSPACE_STATUSES = frozenset({"drafting", "submitted", "awarded", "rejected", "abandoned"})
# Valid Workspace.citation_style values (mirrors ck_workspaces_citation_style).
_CITATION_STYLES = CITATION_STYLES
# Upper bound on a workspace title — bounds the form field so a multi-MB
# title can't be buffered into the DB. Generous for human-entered titles.
_MAX_WORKSPACE_TITLE_LEN = 500
# Upper bound on the workspaces index list (most-recently-updated first).
_WORKSPACE_LIST_LIMIT = 200


def _parse_deadline_form(deadline: str | None):
    """Parse an optional ISO-8601 deadline from a form field.

    Returns a datetime or None. Raises HTTPException(400) on a malformed
    string instead of letting datetime.fromisoformat's ValueError bubble up
    as an unhandled 500 (the global handler only catches HTTPException).
    """
    from datetime import datetime

    if not deadline:
        return None
    try:
        return datetime.fromisoformat(deadline)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="invalid deadline (expected ISO-8601)") from e


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


def _collect_reference_ids(turns) -> set[str]:
    """Walk an iterable of ChatTurn rows and return the set of stringified
    reference_ids embedded in their content blocks.

    Slice 4 residuals: the chat_turn partial needs a {str(uuid): Reference}
    map to render reference chips with a human-friendly title. The view
    builds the map by collecting every reference_id mentioned by any
    turn's content, then querying once — N+1 avoidance for the chat
    history loop.
    """
    ids: set[str] = set()
    for t in turns:
        c = t.content
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "reference":
                    rid = b.get("reference_id")
                    if rid:
                        ids.add(str(rid))
    return ids


def _load_reference_map(
    session: Session,
    workspace_id: uuid.UUID,
    reference_ids: set[str],
) -> dict[str, Reference]:
    """Resolve a set of stringified UUIDs to Reference rows, scoped to
    one workspace (defense-in-depth IDOR check — a stale or hostile
    reference_id from another workspace silently renders as "unknown"
    rather than leaking a cross-workspace title).
    """
    if not reference_ids:
        return {}
    uuid_ids: list[uuid.UUID] = []
    for s in reference_ids:
        try:
            uuid_ids.append(uuid.UUID(s))
        except (ValueError, TypeError):
            continue
    if not uuid_ids:
        return {}
    rows = session.scalars(
        select(Reference).where(
            Reference.workspace_id == workspace_id,
            Reference.id.in_(uuid_ids),
        )
    )
    return {str(r.id): r for r in rows}


def _render_chat_turn_html(
    turn: ChatTurn,
    *,
    templates: Jinja2Templates | None = None,
    chat_reference_map: dict[str, Reference] | None = None,
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

    Slice 4 residuals: ``chat_reference_map`` resolves reference blocks
    to a human-friendly title chip. Missing entries render as
    "(unknown)" — keeps the chat view from crashing if a reference id
    becomes stale.
    """
    engine = templates if templates is not None else _default_templates()
    rendered = engine.env.get_template(_CHAT_TURN_PARTIAL).render(
        turn=turn,
        chat_reference_map=chat_reference_map or {},
    )
    # SSE `data:` lines cannot contain raw newlines (the protocol
    # uses LF as a record separator). Collapse them — the partial uses
    # ``{%- ... -%}`` to keep this no-op for normal content. A bare CR
    # is ALSO a valid SSE line terminator, so user-submitted turn text
    # containing ``\r`` could otherwise inject extra SSE records; strip
    # both.
    return rendered.replace("\r", "").replace("\n", "")


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
            # Slice 4 residuals: resolve reference-block IDs for THIS
            # batch in one query so per-turn render can show the chip's
            # title without an N+1 lookup.
            ref_ids = _collect_reference_ids(new_rows)
            ref_map = _load_reference_map(s, workspace_id, ref_ids)
            for turn in new_rows:
                rendered = _render_chat_turn_html(
                    turn,
                    templates=templates,
                    chat_reference_map=ref_map,
                )
                yield f"data: {rendered}\n\n"
                last_seen_position = turn.position
            tj = s.get(TurnJob, turn_job_id)
            if tj is None or tj.status in TERMINAL_STATUSES:
                yield f"event: done\ndata: {tj.status if tj is not None else 'missing'}\n\n"
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
    title = title.strip()
    if not title or len(title) > _MAX_WORKSPACE_TITLE_LEN:
        raise HTTPException(status_code=400, detail="invalid title")
    parsed_deadline = _parse_deadline_form(deadline)

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
    # Allowlist the filter: an arbitrary ?status is harmless to the DB
    # (parameterized) but matches nothing useful, so reject unknown values with
    # a 400 rather than silently returning an empty list and hiding the typo.
    if status is not None and status not in _WORKSPACE_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status filter")
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
    # Bound the result set. A user with many workspaces (owned + viewer-of)
    # would otherwise get an unbounded SELECT; the index view shows the most
    # recently-updated _WORKSPACE_LIST_LIMIT.
    q = q.limit(_WORKSPACE_LIST_LIMIT)
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

    # Slice 4 residuals: lookup map for any reference-block content
    # embedded in the chat history. Built in ONE query (set-of-ids ->
    # in_ select) so the chat history loop's reference chips render with
    # a human-friendly title without N+1 lookups. Empty when no turn
    # references anything.
    chat_reference_map = _load_reference_map(
        session,
        workspace_id,
        _collect_reference_ids(chat_turns),
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

    # Slice 4: list of done-references for the composer picker. Filtered
    # server-side to ``text_status='done'`` because submit_user_turn
    # rejects anything else — keeping the template dumb means the picker
    # never shows a checkbox that would fail validation. Pending / failed
    # refs still appear on the /references panel (where the user manages
    # them); only the composer picker hides them.
    workspace_references = list(
        session.scalars(
            select(Reference)
            .where(
                Reference.workspace_id == workspace_id,
                Reference.text_status == "done",
            )
            .order_by(Reference.created_at.desc())
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

    # Section-editing slice: the artifact pane renders per-##-section
    # fragments (section_view.html) so each one can be swapped for an
    # edit form independently. Empty sections (e.g. a zero-byte preamble)
    # are skipped — nothing to render or edit. Dicts (not Section objects)
    # so the template context matches what web/sections.py passes for a
    # single-fragment render.
    artifact_sections = [
        {
            "index": s.index,
            "heading": s.heading,
            "sha": s.sha,
            "rendered": _render_proposal_markdown(s.text),
        }
        for s in split_sections(raw)
        if s.text
    ]

    # Phase-0 gate: while in setup, the left pane renders the setup-status
    # card (partials/setup_status.html) instead of the composer. Query the
    # job only when it's actually needed.
    if ws.current_phase == "setup":
        setup_job = session.scalar(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws.id)
        )
        setup_status = setup_job.status if setup_job else "queued"
        setup_last_error = setup_job.last_error if setup_job else None
    else:
        setup_status, setup_last_error = None, None

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
            "artifact_sections": artifact_sections,
            "annotations_enabled": True,
            "needs_disclosure": needs_disclosure,
            "workspace_attachments": workspace_attachments,
            "workspace_references": workspace_references,
            "chat_reference_map": chat_reference_map,
            "setup_status": setup_status,
            "setup_last_error": setup_last_error,
        },
    )


@router.get("/{workspace_id}/setup-status", response_class=HTMLResponse)
def get_setup_status(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    """Render ONLY the setup-status card partial (htmx poll target).

    The card on the main view polls this every 3s while Phase 0 runs;
    terminal states (for the viewing role) render without the hx-trigger,
    so the final swap stops the polling. Access mirrors the main view.
    """
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    job = session.scalar(select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws.id))
    return request.app.state.templates.TemplateResponse(
        request,
        "proposal_assist/partials/setup_status.html",
        {
            "workspace": ws,
            "setup_status": job.status if job else "queued",
            "setup_last_error": job.last_error if job else None,
            "is_owner": ws.owner_id == user.id,
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


@router.post("/{workspace_id}/turns")
async def post_turn(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
    content: str = Form(...),
    attachment_ids: list[uuid.UUID] = Form(default_factory=list),  # noqa: B008
    reference_ids: list[uuid.UUID] = Form(default_factory=list),  # noqa: B008
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
    # Phase gate: during Phase-0 setup the workspace git repo doesn't exist
    # yet, so a turn would run the agent against nothing and fail silently.
    # The composer is suppressed during setup (workspace_main.html); this
    # guards stale tabs and hand-crafted POSTs. The JSON detail is shown
    # inline by app.js's global htmx:responseError handler.
    if ws.current_phase == "setup":
        raise HTTPException(
            status_code=409,
            detail="Workspace is still setting up — review the rubric to start drafting.",
        )
    settings = request.app.state.settings
    # Auth-first: owner check above runs before this kill-switch gate
    # so a non-owner probing /turns on a workspace they don't own gets
    # 403 (not 503 leaking the kill-switch state). The state isn't
    # sensitive but the inverted order broke the auth-first pattern
    # used elsewhere in this router.
    if not settings.drafting_enabled:
        return HTMLResponse(
            "<p>Drafting is currently disabled by an administrator. Please try again later.</p>",
            status_code=503,
        )

    # Spec 2026-06-10 §9: enforce the cloud-LLM data-disclosure acknowledgement
    # server-side, not just via the UI modal. A direct POST (curl / replayed
    # HTMX) that skips the modal must NOT reach the cloud LLM. Per-actor: the
    # acting user must have a prior `disclosure_acknowledged` event on this
    # workspace. Mirrors the GET-page `needs_disclosure` flag, but as a hard gate.
    prior_ack = session.scalar(
        select(WorkspaceEvent.id)
        .where(
            WorkspaceEvent.workspace_id == ws.id,
            WorkspaceEvent.kind == "disclosure_acknowledged",
            WorkspaceEvent.actor_id == user.id,
        )
        .limit(1)
    )
    if prior_ack is None:
        return HTMLResponse(
            "<p>Please acknowledge the data-disclosure notice before "
            "sending a message to the drafting assistant.</p>",
            status_code=403,
        )

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
    # Slice 4 residuals: early cap-check BEFORE the upload loop. The
    # post-upload check below still runs (defense-in-depth + correct
    # behavior if a file silently drops), but this short-circuit avoids
    # decoding + writing N files to disk when the request was clearly
    # over the cap from the start. No rollback needed — nothing has
    # been written yet.
    early_inclusions = len(real_files) + len(attachment_ids) + len(reference_ids)
    if early_inclusions > MAX_TURN_INCLUSIONS:
        return HTMLResponse(
            f"<p>Too many inclusions: {early_inclusions} > {MAX_TURN_INCLUSIONS} max per turn.</p>",
            status_code=400,
        )
    new_upload_ids: list[uuid.UUID] = []
    for upload in real_files:
        content_type = (upload.content_type or "").lower()
        if content_type not in _ALLOWED_MIMES:
            session.rollback()
            _rollback_created_uploads()
            return HTMLResponse(
                f"<p>Unsupported content_type: {_html.escape(repr(content_type))}</p>",
                status_code=400,
            )
        try:
            data = await _read_capped(upload, _MAX_BYTES)
        except HTTPException as e:
            session.rollback()
            _rollback_created_uploads()
            return HTMLResponse(f"<p>{_html.escape(str(e.detail))}</p>", status_code=e.status_code)
        if not data:
            session.rollback()
            _rollback_created_uploads()
            return HTMLResponse("<p>empty upload</p>", status_code=400)
        try:
            fmt, width_px, height_px = _probe_image_format(data)
        except HTTPException as e:
            session.rollback()
            _rollback_created_uploads()
            return HTMLResponse(f"<p>{_html.escape(str(e.detail))}</p>", status_code=e.status_code)
        detected_mime = _PIL_FORMAT_TO_MIME[fmt]
        if detected_mime != content_type:
            session.rollback()
            _rollback_created_uploads()
            return HTMLResponse(
                f"<p>content_type {_html.escape(repr(content_type))} does not "
                f"match the actual image format {_html.escape(repr(detected_mime))}</p>",
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

    # Combine: new uploads + explicit attachment refs. Order preserved
    # so the composer's display order matches what we render in the
    # chat row.
    all_ids: list[uuid.UUID] = new_upload_ids + list(attachment_ids)
    # Slice 4: combined inclusion cap covers attachments + references.
    # Pre-check here saves a wasted submit_user_turn DB round-trip and
    # gives a single error format for both kinds.
    all_inclusions = len(all_ids) + len(reference_ids)
    if all_inclusions > MAX_TURN_INCLUSIONS:
        session.rollback()
        _rollback_created_uploads()
        return HTMLResponse(
            f"<p>Too many inclusions: {all_inclusions} > {MAX_TURN_INCLUSIONS} max per turn.</p>",
            status_code=400,
        )

    try:
        job = submit_user_turn(
            session=session,
            workspace_id=ws.id,
            content_text=content,
            attachment_ids=all_ids or None,
            reference_ids=list(reference_ids) or None,
            submitted_by_user_id=user.id,
            settings=settings,
            workspaces_dir=Path(settings.workspaces_dir),
        )
        session.commit()
    except CommitMutexTimeout as e:
        session.rollback()
        _rollback_created_uploads()
        raise HTTPException(
            status_code=503,
            detail=BUSY_ERROR_MESSAGE,
        ) from e
    except GlobalTokenCapExceeded as e:
        session.rollback()
        _rollback_created_uploads()
        return HTMLResponse(
            f"<p>Global org-wide daily token cap exceeded: {_html.escape(e.reason)}. "
            f"Used {e.used_in} input / {e.used_out} output today; "
            f"cap is {e.cap_in} / {e.cap_out}. Try again after midnight "
            f"Europe/Stockholm or ask an admin to raise the cap.</p>",
            status_code=429,
            headers={"X-Token-Cap": "global"},
        )
    except TokenCapExceeded as e:
        session.rollback()
        _rollback_created_uploads()
        return HTMLResponse(
            f"<p>Workspace daily token cap exceeded: {_html.escape(e.reason)}. "
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
    # persisted: list of {text} + {image} + {reference} blocks when any
    # inclusions are present, single text dict otherwise. Jinja's auto-
    # escape inside the partial handles HTML in `content`.
    #
    # Slice 4 residuals fix: the prior condition (`if all_ids`) treated
    # a refs-only turn as text-only, so the user got no visible chip
    # in the echo until the SSE re-render landed. Including
    # `reference_ids` in the trigger condition + emitting reference
    # blocks in the echo restores immediate feedback.
    if all_ids or reference_ids:
        echo_content: object = (
            [{"type": "text", "text": content}]
            + [{"type": "image", "attachment_id": str(aid)} for aid in all_ids]
            + [{"type": "reference", "reference_id": str(rid)} for rid in reference_ids]
        )
    else:
        echo_content = {"type": "text", "text": content}
    echo_turn = ChatTurn(
        workspace_id=ws.id,
        role="user",
        content=echo_content,  # type: ignore[arg-type]
    )
    # Load the just-attached references for the echo partial so the
    # chip shows a title/filename (not the "(unknown)" fallback). Scoped
    # to this workspace — same query the history-loop path uses.
    echo_ref_map = _load_reference_map(
        session,
        ws.id,
        {str(rid) for rid in reference_ids},
    )
    user_turn_html = request.app.state.templates.env.get_template(
        _CHAT_TURN_PARTIAL,
    ).render(turn=echo_turn, chat_reference_map=echo_ref_map)
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
        citation_service=getattr(request.app.state, "citation_service", None),
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
    # Generic error: a per-email "no user with email X" message is a
    # user-enumeration oracle (lets an owner probe which addresses have
    # accounts). Keep the failure mode indistinguishable.
    if target is None:
        raise HTTPException(status_code=400, detail="could not add viewer")
    # The owner already has full access; a viewer row for them would be a
    # redundant (and confusing) grant. Skip it silently — same redirect.
    if target.id != ws.owner_id:
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
    citation_style: str = Form("vancouver"),
):
    ws = _require_owner(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    # Allowlist the status before assignment. The DB CHECK constraint would
    # also reject a bogus value, but as an IntegrityError that escapes to the
    # global handler as a 500 — validate up front for a clean 400 instead.
    if status not in _WORKSPACE_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status")
    # Same pattern: allowlist citation_style before assignment.
    if citation_style not in _CITATION_STYLES:
        raise HTTPException(status_code=400, detail="invalid citation_style")
    title = title.strip()
    if not title or len(title) > _MAX_WORKSPACE_TITLE_LEN:
        raise HTTPException(status_code=400, detail="invalid title")
    ws.title = title
    ws.status = status
    ws.deadline = _parse_deadline_form(deadline)
    ws.citation_style = citation_style
    session.commit()
    return RedirectResponse(url=f"/workspaces/{ws.id}", status_code=303)


@router.post("/{workspace_id}/delete")
def soft_delete(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
):
    """Soft-delete a workspace and scrub attachment bytes from disk.

    Order (Codex-finding fix): DB commit FIRST (so the user-visible
    "deleted" state is durable), THEN best-effort FS cleanup. If the
    FS cleanup fails, the workspace is still soft-deleted; the orphan
    files are harmless (no app code reads them, and the row-source-of-
    truth lookup gates serving on a live DB row) and a re-invocation
    of this handler OR a future sweep job will collect them.

    Attachment DB rows are intentionally preserved for the audit
    trail; only the bytes on disk are removed.
    """
    from datetime import UTC, datetime

    ws = _require_owner(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    ws.soft_deleted_at = datetime.now(UTC)
    # Cancel any QUEUED turns for this workspace so a deletion-race turn can't
    # be dispatched and start spending tokens / editing the repo. Running turns
    # are left to finish (bounded by turn_max_seconds + the reaper); the drain
    # query also excludes soft-deleted workspaces, so no NEW turn is dispatched.
    session.execute(
        update(TurnJob)
        .where(TurnJob.workspace_id == workspace_id, TurnJob.status == "queued")
        .values(status="failed", error="workspace soft-deleted", completed_at=datetime.now(UTC))
    )
    session.commit()
    # B7: scrub attachment bytes from disk. DB rows remain (compliance —
    # audit trail preserved); only the bytes are removed. Errors here
    # are non-fatal: the workspace is already soft-deleted at the DB
    # level, which is the durable user-visible state.
    workspaces_dir = Path(request.app.state.settings.workspaces_dir)
    storage = AttachmentStorage(root=workspaces_dir)
    try:
        storage.delete_workspace(workspace_id=workspace_id)
    except OSError:
        logger.warning(
            "soft_delete: attachment cleanup failed for workspace %s; "
            "files will be collected by future sweep",
            workspace_id,
            exc_info=True,
        )
    # Also scrub reference bytes (original files + extracted text.txt).
    # Same compliance promise as attachments — DB rows remain for the
    # audit trail, only the on-disk bytes are removed. Best-effort; an
    # error here is non-fatal (the workspace is already soft-deleted).
    ref_storage = ReferenceStorage(root=workspaces_dir)
    try:
        ref_storage.delete_workspace(workspace_id=workspace_id)
    except OSError:
        logger.warning(
            "soft_delete: reference cleanup failed for workspace %s; "
            "files will be collected by future sweep",
            workspace_id,
            exc_info=True,
        )
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
    # Stream the file (FileResponse sets Content-Length + streams in
    # chunks) rather than reading the whole proposal into memory.
    return FileResponse(
        proposal,
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

    Audit note: the cloud_turn_cancelled actor_id is the turn's
    submitted_by_user_id (via the helper), which equals the canceller
    while _require_owner enforces owner-only access and there is no
    ownership-transfer / admin-on-behalf cancel. If such a path is added,
    pass the acting user's id to the helper explicitly.
    """
    ws = _require_owner(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    did = record_terminal_outcome(
        session,
        turn_job_id=turn_job_id,
        workspace_id=ws.id,
        outcome="cancelled",
        expected_status="queued",
        raw_error=None,
        emit_chat=True,
    )
    session.commit()
    if did:
        return HTMLResponse("<p>cancelled</p>")
    return HTMLResponse("<p>too late — already started or finished</p>")
