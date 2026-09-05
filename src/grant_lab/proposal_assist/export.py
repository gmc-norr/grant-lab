"""Export orchestration for raw and citation-composed Markdown.

Thin wrappers over the citation helpers, consumed by ``web/api/export.py``.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from grant_lab.db.models import Workspace
from grant_lab.proposal_assist.artifacts import PROPOSAL_FILENAME
from grant_lab.proposal_assist.citations.bibliography import (
    BuildBibliographyBlocked,  # noqa: F401  (re-exported for the route's except clause)
    build_bibliography,
    compose_export,
)
from grant_lab.proposal_assist.citations.ledger import load_ledger
from grant_lab.proposal_assist.collab import state as collab_state
from grant_lab.proposal_assist.collab import store as collab_store
from grant_lab.proposal_assist.collab.canonical import canonical_artifact_markdown
from grant_lab.proposal_assist.editing.save import CITATIONS_FILENAME
from grant_lab.proposal_assist.paths import workspace_repo_dir


def _draft_serves_room(session: Session, workspace: Workspace) -> bool:
    """Whether export should serve the LIVE room projection instead of disk: draft
    collab enabled AND the room has diverged from disk (an un-checkpointed tail is
    otherwise invisible to export post-flip). Same one-branch predicate as
    web/api/annotations.get_annotations — has_authored_updates keeps a seed-only
    (untouched) room on the disk read, so disabled/untouched stays byte-for-byte."""
    return collab_state.is_collab_enabled(
        session, workspace_id=workspace.id, artifact="draft"
    ) and collab_store.has_authored_updates(
        session, workspace_id=workspace.id, artifact="draft"
    )


def safe_title(title: str) -> str:
    """Sanitize a workspace title for a Content-Disposition filename."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in title)[:80] or "proposal"


def get_markdown_export(
    workspace: Workspace, workspaces_dir, *, session: Session
) -> bytes | None:
    """Raw proposal.md bytes; None if there is no draft yet. When draft collab is
    enabled and the room has authored edits, serves the canonical room projection
    (UTF-8) so an un-checkpointed tail is not omitted (byte-for-byte disk read
    when disabled/untouched)."""
    repo_dir = workspace_repo_dir(workspaces_dir, workspace.id)
    path = repo_dir / PROPOSAL_FILENAME
    if not path.exists():
        return None
    if _draft_serves_room(session, workspace):
        return canonical_artifact_markdown(
            session, workspace_id=workspace.id, artifact="draft", repo_dir=repo_dir
        ).encode("utf-8")
    return path.read_bytes()


def get_composed_export(*, workspace: Workspace, workspaces_dir, session: Session) -> str | None:
    """Markdown with inline citations rendered + bibliography appended. None if no
    draft. Raises ValueError on corrupt citations.json; raises
    BuildBibliographyBlocked if any cited reference is not citable yet. Composes
    from the same canonical draft text as get_markdown_export (live room when
    collab is enabled + authored; the ledger is always the on-disk citations.json,
    which is not part of the room)."""
    repo_dir = workspace_repo_dir(workspaces_dir, workspace.id)
    proposal_path = repo_dir / PROPOSAL_FILENAME
    if not proposal_path.exists():
        return None
    citations_path = repo_dir / CITATIONS_FILENAME
    if _draft_serves_room(session, workspace):
        proposal_text = canonical_artifact_markdown(
            session, workspace_id=workspace.id, artifact="draft", repo_dir=repo_dir
        )
    else:
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
