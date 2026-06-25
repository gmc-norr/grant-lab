"""Export orchestration — raw + composed Markdown. Thin wrappers over the
existing citations helpers, kept OUT of the web/ package so the Jinja routes can
be deleted (Slice F teardown) without touching this module. Consumed by
web/api/export.py."""

from __future__ import annotations

from sqlalchemy.orm import Session

from call_manager.db.models import Workspace
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.citations.bibliography import (
    BuildBibliographyBlocked,  # noqa: F401  (re-exported for the route's except clause)
    build_bibliography,
    compose_export,
)
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.editing.save import CITATIONS_FILENAME
from call_manager.proposal_assist.paths import workspace_repo_dir


def safe_title(title: str) -> str:
    """Sanitize a workspace title for a Content-Disposition filename."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in title)[:80] or "proposal"


def get_markdown_export(workspace: Workspace, workspaces_dir) -> bytes | None:
    """Raw proposal.md bytes; None if there is no draft yet."""
    path = workspace_repo_dir(workspaces_dir, workspace.id) / PROPOSAL_FILENAME
    if not path.exists():
        return None
    return path.read_bytes()


def get_composed_export(*, workspace: Workspace, workspaces_dir, session: Session) -> str | None:
    """Markdown with inline citations rendered + bibliography appended. None if no
    draft. Raises ValueError on corrupt citations.json; raises
    BuildBibliographyBlocked if any cited reference is not citable yet."""
    repo_dir = workspace_repo_dir(workspaces_dir, workspace.id)
    proposal_path = repo_dir / PROPOSAL_FILENAME
    if not proposal_path.exists():
        return None
    citations_path = repo_dir / CITATIONS_FILENAME
    proposal_text = proposal_path.read_text()
    ledger = load_ledger(citations_path.read_text() if citations_path.exists() else None)
    style = workspace.citation_style or "vancouver"
    bib = build_bibliography(
        proposal_text=proposal_text,
        ledger=ledger,
        session=session,
        workspace_id=workspace.id,
        style=style,
        repo_dir=repo_dir,
        commit=False,
    )
    return compose_export(
        proposal_text=proposal_text,
        ledger=ledger,
        session=session,
        workspace_id=workspace.id,
        style=style,
        bibliography_text=bib["bibliography_md"] or None,
    )
