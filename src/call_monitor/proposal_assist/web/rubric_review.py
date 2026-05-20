"""Owner rubric-review endpoint (Phase 0 confirmation gate).

Workspace owners review the auto-extracted rubric (or edit it) and click
Confirm. POST handler (Task 18) advances the workspace from current_phase
'setup' to 'spec' and marks the workspace_setup_jobs row 'done'.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from call_monitor.auth.dependencies import get_current_user
from call_monitor.db.models import Workspace, WorkspaceEvent, WorkspaceSetupJob
from call_monitor.db.session import get_db_dependency

router = APIRouter(prefix="/workspaces", tags=["proposal-assist"])


def _require_owner(
    *,
    workspace_id: uuid.UUID,
    user,
    session: Session,
) -> Workspace:
    ws = session.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if ws.owner_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="only the workspace owner may view or edit the rubric",
        )
    return ws


def _session_dep(request: Request):
    """Resolve the per-request session from the app's factory.
    `get_db_dependency` is a factory that returns the actual dependency
    callable; we wire it via app.state to avoid a global at import time."""
    factory = request.app.state.session_factory
    yield from get_db_dependency(factory)()


@router.get(
    "/{workspace_id}/rubric-review",
    response_class=HTMLResponse,
)
def get_rubric_review(
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
    return request.app.state.templates.TemplateResponse(
        request,
        "proposal_assist/rubric_review.html",
        {
            "workspace": ws,
            "criteria": ws.rubric_criteria,
            "funder_notes": ws.rubric_funder_notes or "",
        },
    )


def _parse_form_criteria(form: dict[str, str]) -> list[dict[str, Any]]:
    """Parse criteria[N][field] form keys into a list[dict].

    FastAPI's Form() decoding gives flat key/value pairs; we re-nest by index.
    """
    pattern = re.compile(r"^criteria\[(\d+)\]\[(\w+)\]$")
    rows: dict[int, dict[str, Any]] = {}
    for key, value in form.items():
        m = pattern.match(key)
        if not m:
            continue
        idx, field = int(m.group(1)), m.group(2)
        rows.setdefault(idx, {})[field] = value

    out: list[dict[str, Any]] = []
    for idx in sorted(rows.keys()):
        crit = rows[idx]
        # Cast numeric fields.
        try:
            crit["weight"] = float(crit.get("weight", 0))
            crit["scale_max"] = int(crit.get("scale_max", 5))
        except (TypeError, ValueError) as e:
            raise HTTPException(
                400, f"criterion {idx}: bad numeric value ({e})"
            ) from e
        for required in ("name", "description", "weight", "scale_max"):
            if required not in crit:
                raise HTTPException(400, f"criterion {idx}: missing {required}")
        out.append(crit)
    if not out:
        raise HTTPException(400, "rubric must have at least one criterion")
    return out


@router.post(
    "/{workspace_id}/rubric-review",
    response_class=HTMLResponse,
)
async def post_rubric_review(
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

    form = dict(await request.form())
    new_criteria = _parse_form_criteria(form)
    new_funder_notes = form.get("funder_notes", "")

    old_criteria = ws.rubric_criteria
    old_funder_notes = ws.rubric_funder_notes or ""

    # Only write a rubric_edited event when content actually changed.
    content_changed = new_criteria != old_criteria or new_funder_notes != old_funder_notes

    ws.rubric_criteria = new_criteria
    ws.rubric_funder_notes = new_funder_notes
    if content_changed:
        ws.rubric_updated_at = datetime.now(UTC)
        session.add(
            WorkspaceEvent(
                workspace_id=ws.id,
                kind="rubric_edited",
                payload={
                    "old_criteria": old_criteria,
                    "new_criteria": new_criteria,
                    "old_funder_notes": old_funder_notes,
                    "new_funder_notes": new_funder_notes,
                    "edited_by_user_id": user.id,
                },
            )
        )

    # Advance Phase 0 if this is the first review beat (job exists and is
    # waiting_for_rubric_review).
    job = (
        session.query(WorkspaceSetupJob)
        .filter(WorkspaceSetupJob.workspace_id == ws.id)
        .one_or_none()
    )
    if job is not None and job.status == "waiting_for_rubric_review":
        job.status = "done"
        job.completed_at = datetime.now(UTC)
        ws.current_phase = "spec"

    session.commit()

    # After advancing, redirect the browser to the workspace's main view.
    # HTMX recognizes HX-Redirect; non-HTMX gets a normal 303.
    if ws.current_phase == "spec":
        target = f"/workspaces/{ws.id}"
        if "HX-Request" in request.headers:
            return Response(
                status_code=204,
                headers={"HX-Redirect": target},
            )
        return Response(status_code=303, headers={"Location": target})

    # Otherwise re-render the form with the updated content (HTMX swap target).
    return request.app.state.templates.TemplateResponse(
        request,
        "proposal_assist/rubric_review.html",
        {
            "workspace": ws,
            "criteria": ws.rubric_criteria,
            "funder_notes": ws.rubric_funder_notes or "",
        },
    )
