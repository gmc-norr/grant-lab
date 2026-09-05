"""JSON /api Markdown export (Cockpit Slice F). Member-gated. Reuses the
proposal_assist.export service. The Jinja /workspaces/{id}/export*.md routes are
deleted in the same slice's teardown."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response

from grant_lab.auth.dependencies import get_current_user
from grant_lab.db.models import User
from grant_lab.proposal_assist.citations.bibliography import BuildBibliographyBlocked
from grant_lab.proposal_assist.export import (
    get_composed_export,
    get_markdown_export,
    safe_title,
)
from grant_lab.proposal_assist.web._perms import require_member
from grant_lab.web.api.errors import api_error
from grant_lab.web.http_headers import content_disposition

router = APIRouter()


@router.get("/workspaces/{workspace_id}/export.md")
def export_markdown(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
):
    settings = request.app.state.settings
    with request.app.state.session_factory() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        data = get_markdown_export(ws, settings.workspaces_dir, session=session)
        title = safe_title(ws.title)
    if data is None:
        return api_error(404, "not_found", "no draft to export yet")
    return Response(
        content=data,
        media_type="text/markdown",
        headers={"Content-Disposition": content_disposition("attachment", f"{title}.md")},
    )


@router.get("/workspaces/{workspace_id}/export-composed.md")
def export_composed(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
):
    settings = request.app.state.settings
    with request.app.state.session_factory() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        title = safe_title(ws.title)
        try:
            composed = get_composed_export(
                workspace=ws, workspaces_dir=settings.workspaces_dir, session=session
            )
        except ValueError as exc:
            return api_error(
                409, "citation_ledger_invalid", f"citations.json could not be read: {exc}"
            )
        except BuildBibliographyBlocked as exc:
            blocked = [
                {"citekey": str(b.get("citekey")), "reason": str(b.get("reason"))}
                for b in exc.blocked
            ]
            return api_error(
                409,
                "export_blocked",
                "Some cited references are not citable yet"
                " — approve their metadata (or waive it) and retry.",
                detail={"blocked": blocked},
            )
    if composed is None:
        return api_error(404, "not_found", "no draft to export yet")
    return Response(
        content=composed,
        media_type="text/markdown",
        headers={"Content-Disposition": content_disposition("attachment", f"{title}-composed.md")},
    )
