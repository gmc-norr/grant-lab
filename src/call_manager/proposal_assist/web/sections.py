"""Section-level editing routes (HTMX fragments). Spec 2026-06-10 §1.

Three routes per section:

* ``GET  /{workspace_id}/sections/{n}``      — re-rendered view fragment
  (owner OR viewer; used by the Cancel button).
* ``GET  /{workspace_id}/sections/{n}/edit`` — edit-form fragment (owner
  only; 409 notice if an agent turn is queued/running).
* ``POST /{workspace_id}/sections/{n}``      — save via
  :func:`save_section`, then return the re-rendered view fragment with
  any Tier-1 issues.

Mirrors the workspaces.py router pattern: same ``_session_dep``, same
``_require_owner`` / ``_require_owner_or_viewer`` auth deps (NOT the
unauthorized ``_load_workspace_or_404`` — that would be an IDOR), and
templates resolved via ``request.app.state.templates``.

CSRF: the app uses Origin/Referer middleware (auth/csrf.py), not form
tokens — the section_edit form needs no hidden CSRF field (matches the
composer form in workspace_main.html).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import Workspace
from call_manager.proposal_assist.citations.gitio import GitCommitError, current_head
from call_manager.proposal_assist.drafting.locking import BUSY_ERROR_MESSAGE, CommitMutexTimeout
from call_manager.proposal_assist.editing.save import (
    PROPOSAL_FILENAME,
    SectionStructureError,
    StaleSectionError,
    TurnInFlightError,
    _turn_in_flight,
    save_section,
)
from call_manager.proposal_assist.editing.sectionizer import Section, split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.web.workspaces import (
    _render_proposal_markdown,
    _require_owner,
    _require_owner_or_viewer,
    _session_dep,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["sections"])

_VIEW_PARTIAL = "proposal_assist/partials/section_view.html"
_EDIT_PARTIAL = "proposal_assist/partials/section_edit.html"


def _read_sections(request: Request, workspace_id: uuid.UUID) -> tuple[str, list[Section]]:
    """Read the workspace repo's proposal.md and return (HEAD sha, sections).

    404 if proposal.md (or the repo itself) doesn't exist yet — same
    user-facing failure as export_markdown's "no proposal.md yet".
    """
    repo_dir = workspace_repo_dir(request.app.state.settings.workspaces_dir, workspace_id)
    proposal = repo_dir / PROPOSAL_FILENAME
    if not proposal.exists():
        raise HTTPException(status_code=404, detail="no proposal.md yet")
    try:
        head = current_head(repo_dir)
    except GitCommitError as e:
        logger.warning("workspace %s: git HEAD unavailable (%s)", workspace_id, e)
        raise HTTPException(status_code=404, detail="no proposal.md yet") from e
    return head, split_sections(proposal.read_text())


def _section_or_404(sections: list[Section], n: int) -> Section:
    if not 0 <= n < len(sections):
        raise HTTPException(status_code=404, detail="no such section")
    return sections[n]


def _section_ctx(section: Section) -> dict:
    """Template-facing dict for one section (Jinja getattr→getitem
    fallback makes ``{{ s.index }}`` etc. work on plain dicts)."""
    return {
        "index": section.index,
        "heading": section.heading,
        "sha": section.sha,
        "rendered": _render_proposal_markdown(section.text),
    }


def _view_fragment(
    request: Request,
    workspace: Workspace,
    section: Section,
    *,
    is_owner: bool,
    issues: list[dict] | None = None,
) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        _VIEW_PARTIAL,
        {
            "workspace": workspace,
            "s": _section_ctx(section),
            "is_owner": is_owner,
            "issues": issues or [],
            # Task 11: the view partial lazy-loads the annotation strip for
            # this section's CURRENT sha (a fresh save → job_pending poll).
            "annotations_enabled": True,
        },
    )


@router.get("/{workspace_id}/sections/{n}", response_class=HTMLResponse)
def get_section_view(
    workspace_id: uuid.UUID,
    n: int,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    """Single-section view fragment (Cancel target). Owner or viewer."""
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    _, sections = _read_sections(request, workspace_id)
    section = _section_or_404(sections, n)
    return _view_fragment(request, ws, section, is_owner=ws.owner_id == user.id)


@router.get("/{workspace_id}/sections/{n}/edit", response_class=HTMLResponse)
def get_section_edit(
    workspace_id: uuid.UUID,
    n: int,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    """Edit-form fragment for one section. Owner only.

    Soft pre-check: 409 notice if an agent turn is queued/running — the
    hard guard remains save_section's turn-lock check under the commit
    mutex; this just avoids offering a form that's doomed to 409 on save.
    """
    ws = _require_owner(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    if _turn_in_flight(session, ws.id):
        raise HTTPException(
            status_code=409,
            detail="assistant is working — try again when the turn finishes",
        )
    head, sections = _read_sections(request, workspace_id)
    section = _section_or_404(sections, n)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        _EDIT_PARTIAL,
        {
            "workspace": ws,
            "s": {**_section_ctx(section), "raw": section.text},
            "base_commit_sha": head,
        },
    )


@router.post("/{workspace_id}/sections/{n}", response_class=HTMLResponse)
def post_section_save(
    workspace_id: uuid.UUID,
    n: int,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
    content: str = Form(...),
    base_commit_sha: str = Form(...),
    base_section_sha: str = Form(...),
) -> HTMLResponse:
    """Save one section via the single write path (save_section), commit,
    and return the re-rendered view fragment with Tier-1 issues."""
    ws = _require_owner(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    settings = request.app.state.settings
    # Capture pre-save section count so we can detect splits/merges later.
    _, sections_before = _read_sections(request, workspace_id)
    n_before = len(sections_before)
    try:
        result = save_section(
            session=session,
            workspace=ws,
            user_id=user.id,
            section_index=n,
            new_text=content,
            base_commit_sha=base_commit_sha,
            base_section_sha=base_section_sha,
            workspaces_dir=settings.workspaces_dir,
        )
        session.commit()
        # Any failure beyond this point must not be reported as a failed save
        # — the save is committed.
    except TurnInFlightError as e:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="assistant is working — try again when the turn finishes",
        ) from e
    except StaleSectionError as e:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="this section changed underneath you — reopen it",
        ) from e
    except SectionStructureError as e:
        session.rollback()
        raise HTTPException(
            status_code=400,
            detail=(
                "keep the section's ## heading line — removing or merging sections is done via chat"
            ),
        ) from e
    except CommitMutexTimeout as e:
        session.rollback()
        raise HTTPException(
            status_code=503,
            detail=BUSY_ERROR_MESSAGE,
        ) from e
    except GitCommitError as e:
        session.rollback()
        logger.exception(
            "section save failed for workspace %s section %s",
            workspace_id,
            n,
        )
        raise HTTPException(
            status_code=500,
            detail="save failed — nothing was written",
        ) from e
    except IndexError as e:
        session.rollback()
        raise HTTPException(status_code=404, detail="no such section") from e

    # Re-read the committed document so the fragment reflects exactly what
    # was saved (newline glue, extra-heading splits).
    _, sections_after = _read_sections(request, workspace_id)
    n_after = len(sections_after)

    if n_after != n_before:
        # Section count changed (e.g. a new ## heading was added): re-render
        # the entire artifact pane so all section divs are in sync.
        artifact_sections = [_section_ctx(s) for s in sections_after if s.text]
        templates = request.app.state.templates
        resp = templates.TemplateResponse(
            request,
            "proposal_assist/partials/artifact_sections.html",
            {
                "workspace": ws,
                "is_owner": True,
                "annotations_enabled": True,
                "artifact_sections": artifact_sections,
                "save_issues_for_index": n,
                "save_issues": result.issues,
            },
        )
        resp.headers["HX-Retarget"] = "#artifact-rendered"
        resp.headers["HX-Reswap"] = "innerHTML"
        return resp

    # Fast path: count unchanged — return only the saved section's fragment.
    section = _section_or_404(sections_after, n)
    return _view_fragment(request, ws, section, is_owner=True, issues=result.issues)
