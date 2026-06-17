"""SP2a-3 cockpit JSON surface: the React source editor's read + save endpoints.

Mounted under /api/workspaces/{id}. Reuses the merged save_section write path
and the proposal_assist permission guards + sectionizer + citation readers.
The runtime error envelope is produced by the global HTTPException handler
(handlers just raise HTTPException); ApiError is the OpenAPI schema mirror.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import Reference, User
from call_manager.proposal_assist.citations import reader as citation_reader
from call_manager.proposal_assist.citations.gitio import GitCommitError, current_head
from call_manager.proposal_assist.citations.ledger import find_by_id, load_ledger
from call_manager.proposal_assist.citations.markers import find_markers
from call_manager.proposal_assist.drafting.locking import BUSY_ERROR_MESSAGE, CommitMutexTimeout
from call_manager.proposal_assist.editing.save import (
    CITATIONS_FILENAME,
    PROPOSAL_FILENAME,
    SaveResult,
    SectionStructureError,
    StaleSectionError,
    save_section,
)
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.web._perms import require_member, require_owner

logger = logging.getLogger(__name__)

router = APIRouter()


class SectionOut(BaseModel):
    index: int
    heading: str
    text: str
    sha: str


class CitationOut(BaseModel):
    citekey: str
    number: int | None = None
    stance: str | None = None
    title: str | None = None


class DraftOut(BaseModel):
    head: str
    can_edit: bool
    sections: list[SectionOut]
    # ev_id -> citation display fields, computed over the WHOLE proposal.
    citations: dict[str, CitationOut]


class SaveSectionIn(BaseModel):
    content: str
    base_commit_sha: str
    base_section_sha: str


class SaveResultOut(BaseModel):
    commit_sha: str
    head: str
    section_sha: str
    issues: list[dict]
    job_enqueued: bool
    changed: bool

    @classmethod
    def from_result(cls, result: SaveResult) -> SaveResultOut:
        return cls(
            commit_sha=result.commit_sha,
            head=result.head,
            section_sha=result.section_sha,
            issues=result.issues,
            job_enqueued=result.job_enqueued,
            changed=result.changed,
        )


def _citation_map(session, settings, workspace_id: uuid.UUID, raw: str) -> dict[str, CitationOut]:
    """Build {ev_id: CitationOut} over the WHOLE proposal `raw`.

    number_by_ev/stance_by_ev are proposal-wide; citekey comes from the marker;
    title is resolved per ev_id via the ledger -> Reference row. A missing or
    corrupt ledger yields an EMPTY map (never raises) — same degrade rule as
    _citation_render_maps_for.
    """
    repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
    citations_path = repo_dir / CITATIONS_FILENAME
    try:
        ledger = load_ledger(citations_path.read_text() if citations_path.exists() else None)
    except (ValueError, OSError):
        return {}
    number_by_ev = citation_reader.build_citation_numbering(raw, ledger)
    stance_by_ev = citation_reader.build_citation_stances(ledger)
    out: dict[str, CitationOut] = {}
    for m in find_markers(raw):
        ev_id = m["ev_id"]
        if ev_id in out:
            continue
        title: str | None = None
        row = find_by_id(ledger, ev_id)
        if row is not None and row.get("reference_id") is not None:
            try:
                ref_uuid = uuid.UUID(str(row["reference_id"]))
            except (ValueError, TypeError):
                ref_uuid = None
            if ref_uuid is not None:
                ref = session.get(Reference, ref_uuid)
                if ref is not None and ref.workspace_id == workspace_id:
                    csl = ref.csl_json or ref.csl_json_extracted or {}
                    csl_title = csl.get("title") if isinstance(csl, dict) else None
                    csl_title = csl_title if isinstance(csl_title, str) else None
                    title = ref.title or csl_title or ref.filename
        out[ev_id] = CitationOut(
            citekey=m["citekey"],
            number=number_by_ev.get(ev_id),
            stance=stance_by_ev.get(ev_id),
            title=title,
        )
    return out


@router.get("/workspaces/{workspace_id}/draft", response_model=DraftOut)
def get_draft(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> DraftOut:
    """One read for the editor + Outline + Full-draft tab + pills. Member-gated."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        can_edit = ws.owner_id == user.id
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        proposal = repo_dir / PROPOSAL_FILENAME
        if not proposal.exists():
            raise HTTPException(status_code=404, detail="no proposal.md yet")
        try:
            head = current_head(repo_dir)
        except GitCommitError as e:
            raise HTTPException(status_code=404, detail="no proposal.md yet") from e
        raw = proposal.read_text()
        sections = [
            SectionOut(index=s.index, heading=s.heading, text=s.text, sha=s.sha)
            for s in split_sections(raw)
        ]
        citations = _citation_map(session, settings, workspace_id, raw)
    return DraftOut(head=head, can_edit=can_edit, sections=sections, citations=citations)


@router.post("/workspaces/{workspace_id}/sections/{index}", response_model=SaveResultOut)
def post_save_section(
    workspace_id: uuid.UUID,
    index: int,
    body: SaveSectionIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SaveResultOut:
    """Save one section via the single write path. Owner-gated. The endpoint
    owns the transaction: save_section adds rows but does NOT commit, and the
    commit mutex is released by this handler's COMMIT / ROLLBACK."""
    settings = request.app.state.settings
    # Mirror the HTMX route's pre-read 404 (sections.py:73-89, used at
    # sections.py:240): before touching save_section, reject a workspace that
    # has no proposal.md yet OR no git HEAD yet with the SAME user-facing 404 as
    # export_markdown / _read_sections. save_section reads HEAD + proposal.md
    # directly (save.py:168-170) and would otherwise surface a raw
    # FileNotFoundError / GitCommitError (500), not a 404. Cover BOTH the same
    # way _read_sections does: .exists() first, then catch GitCommitError.
    repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
    if not (repo_dir / PROPOSAL_FILENAME).exists():
        raise HTTPException(status_code=404, detail="no proposal.md yet")
    try:
        current_head(repo_dir)
    except GitCommitError as e:
        logger.warning("workspace %s: git HEAD unavailable (%s)", workspace_id, e)
        raise HTTPException(status_code=404, detail="no proposal.md yet") from e
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        try:
            result = save_section(
                session=session,
                workspace=ws,
                user_id=user.id,
                section_index=index,
                new_text=body.content,
                base_commit_sha=body.base_commit_sha,
                base_section_sha=body.base_section_sha,
                workspaces_dir=settings.workspaces_dir,
            )
            session.commit()
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
                    "keep the section's ## heading line — removing or "
                    "merging sections is done via chat"
                ),
            ) from e
        except CommitMutexTimeout as e:
            session.rollback()
            raise HTTPException(status_code=503, detail=BUSY_ERROR_MESSAGE) from e
        except GitCommitError as e:
            session.rollback()
            logger.exception("section save failed for workspace %s section %s", workspace_id, index)
            raise HTTPException(status_code=500, detail="save failed — nothing was written") from e
        except IndexError as e:
            session.rollback()
            raise HTTPException(status_code=404, detail="no such section") from e
    return SaveResultOut.from_result(result)
