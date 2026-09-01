"""Shared cloud-LLM trigger gate for the manual background-job endpoints
(coherence, evaluator, review). Extracted from the previously-duplicated
per-endpoint copies; the ONLY per-caller difference is the disclosure_action
phrase woven into the 403 message. Same order + status codes as the interactive
turns route (web/workspaces.py): setup -> 409, drafting disabled -> 503, missing
per-actor disclosure ack -> 403.

The citation/reference surface is a SECOND cloud-egress family with a different
shape — it has no setup phase to respect, and it egresses only when
`CITATION_LLM_PROVIDER` is a cloud provider — so it composes the pieces below
rather than calling `gate_cloud_llm`. There is exactly one disclosure lookup
(`has_disclosure_ack`) and one predicate for whether citation work leaves the
building (`citation_llm_egresses`); a second copy of either is how the two
families drift apart."""

from __future__ import annotations

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import Workspace, WorkspaceEvent


def citation_llm_egresses(settings) -> bool:
    """True when citation/reference work leaves the building.

    The default provider is on-prem Ollama, so this is False in the default
    deployment and every caller below is a no-op there. `build_citation_llm`
    already refuses to construct a cloud client without
    `CITATION_LLM_ALLOW_CLOUD`, so reaching here with True means an operator
    opted in — which covers the machine, not the individual user.
    """
    return settings.citation_llm_provider.lower() == "anthropic"


def has_disclosure_ack(*, session: Session, workspace_id, user_id) -> bool:
    """Whether this user has acknowledged the data-disclosure notice for this
    workspace. The single source of truth for that question."""
    return (
        session.scalar(
            select(WorkspaceEvent.id)
            .where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.kind == "disclosure_acknowledged",
                WorkspaceEvent.actor_id == user_id,
            )
            .limit(1)
        )
        is not None
    )


def citation_cloud_permitted(*, request: Request, session: Session, workspace_id, user) -> bool:
    """Whether THIS user's request may send citation/reference text off-prem.

    False also when the admin kill-switch is off: `DRAFTING_ENABLED=false` is an
    administrator saying "stop calling the hosted model", and the citation
    surface calls one. Callers degrade rather than raise where the LLM is an
    enhancement to a non-AI action (reference upload) — see their call sites.
    """
    if not citation_llm_egresses(request.app.state.settings):
        return True
    if not request.app.state.settings.drafting_enabled:
        return False
    return has_disclosure_ack(session=session, workspace_id=workspace_id, user_id=user.id)


def gate_cloud_llm(
    *, ws: Workspace, request: Request, session: Session, user, disclosure_action: str
) -> None:
    if ws.current_phase == "setup":
        raise HTTPException(
            status_code=409,
            detail="Workspace is still setting up — review the rubric to start drafting.",
        )
    if not request.app.state.settings.drafting_enabled:
        raise HTTPException(
            status_code=503,
            detail="Drafting is currently disabled by an administrator. Please try again later.",
        )
    if not has_disclosure_ack(session=session, workspace_id=ws.id, user_id=user.id):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "disclosure_required",
                "message": (
                    "Please acknowledge the data-disclosure notice before "
                    f"{disclosure_action}."
                ),
            },
        )
