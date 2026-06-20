"""SP2a-3 cockpit JSON surface: the React source editor's read + save endpoints.

Mounted under /api/workspaces/{id}. Reuses the merged save_section write path
and the proposal_assist permission guards + sectionizer + citation readers.
The runtime error envelope is produced by the global HTTPException handler
(handlers just raise HTTPException); ApiError is the OpenAPI schema mirror.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import (
    ArtifactVersion,
    Reference,
    ReferenceChunk,
    SpecLockEvent,
    User,
    WorkspaceEvent,
)
from call_manager.proposal_assist.artifacts import SPEC_FILENAME, SPEC_SKELETON, Artifact
from call_manager.proposal_assist.citations import reader as citation_reader
from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    current_head,
    write_and_commit_files,
)
from call_manager.proposal_assist.citations.human_cite import (
    CiteInvalid,
    CiteRefNotFound,
    CiteStale,
    insert_citation_for_user,
)
from call_manager.proposal_assist.citations.ledger import find_by_id, load_ledger
from call_manager.proposal_assist.citations.markers import find_markers
from call_manager.proposal_assist.drafting.locking import (
    BUSY_ERROR_MESSAGE,
    CommitMutexTimeout,
    acquire_commit_mutex,
)
from call_manager.proposal_assist.editing.save import (
    CITATIONS_FILENAME,
    PROPOSAL_FILENAME,
    SaveResult,
    SectionStructureError,
    SpecLockedError,
    StaleSectionError,
    save_section,
)
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.references.storage import ReferenceStorage
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


class CitationSourceOut(BaseModel):
    evidence_id: str
    reference_id: str | None = None
    source_line: str
    claim: str | None = None
    stance: str | None = None
    confidence: str | None = None
    index: int
    total: int
    prev_ev: str | None = None
    next_ev: str | None = None
    tombstoned: bool = False
    no_text: bool = False
    note: str | None = None
    text: str
    highlight: list[int] | None = None


class DraftOut(BaseModel):
    head: str
    can_edit: bool
    sections: list[SectionOut]
    # ev_id -> citation display fields, computed over the WHOLE proposal.
    citations: dict[str, CitationOut]


class SpecOut(BaseModel):
    head: str
    can_edit: bool
    sections: list[SectionOut]  # no citations: the spec is citation-free


class SpecLockOut(BaseModel):
    locked: bool
    locked_at: datetime | None = None
    locked_by: str | None = None


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


class AddCitationIn(BaseModel):
    section_index: int
    base_commit_sha: str
    base_section_sha: str
    anchor: str = Field(min_length=1)
    reference_id: uuid.UUID
    chunk_index: int
    evidence_quote: str = Field(min_length=1)
    quote_start: int = Field(ge=0)
    quote_end: int = Field(ge=0)
    stance: str = "supports"


class AddCitationOut(BaseModel):
    ev_id: str
    citekey: str
    commit_sha: str


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


def _sections_out(raw: str) -> list[SectionOut]:
    """Split a raw artifact document into the wire SectionOut list. Shared by
    get_draft and get_spec so both produce identical section semantics."""
    return [
        SectionOut(index=s.index, heading=s.heading, text=s.text, sha=s.sha)
        for s in split_sections(raw)
    ]


def _locker_email(session, user_id: int | None) -> str | None:
    """Display string for a spec locker. user_id is non-null only while the row
    is set, and the FK is SET NULL on user delete, so the lookup never dangles."""
    if user_id is None:
        return None
    u = session.get(User, user_id)
    return u.email if u is not None else None


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
        sections = _sections_out(raw)
        citations = _citation_map(session, settings, workspace_id, raw)
    return DraftOut(head=head, can_edit=can_edit, sections=sections, citations=citations)


@router.get("/workspaces/{workspace_id}/spec", response_model=SpecOut)
def get_spec(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SpecOut:
    """Read the Spec artifact: outline + section bytes, no citations. Member-gated."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        can_edit = ws.owner_id == user.id
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        spec = repo_dir / SPEC_FILENAME
        if not spec.exists():
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet")
        try:
            head = current_head(repo_dir)
        except GitCommitError as e:
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet") from e
        sections = _sections_out(spec.read_text())
    return SpecOut(head=head, can_edit=can_edit, sections=sections)


@router.post("/workspaces/{workspace_id}/spec/sections/{index}", response_model=SaveResultOut)
def post_save_spec_section(
    workspace_id: uuid.UUID,
    index: int,
    body: SaveSectionIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SaveResultOut:
    """Save one Spec section via the shared write path (artifact=spec). Owner-gated.

    Same body + status mapping as post_save_section; the spec path skips
    Tier-1/Tier-2 (no citation lint, no audit job)."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        # Authorize BEFORE touching repo state (mirrors post_save_section): a
        # non-owner must not be able to distinguish "no proposal-spec.md yet" (404)
        # from "owner only" (403) for any workspace UUID they can name.
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        if not (repo_dir / SPEC_FILENAME).exists():
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet")
        try:
            current_head(repo_dir)
        except GitCommitError as e:
            logger.warning("workspace %s: git HEAD unavailable (%s)", workspace_id, e)
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet") from e
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
                artifact=Artifact.SPEC,
            )
            session.commit()
        except StaleSectionError as e:
            session.rollback()
            raise HTTPException(
                status_code=409,
                detail="this section changed underneath you — reopen it",
            ) from e
        except SpecLockedError as e:
            session.rollback()
            raise HTTPException(
                status_code=409,
                detail="spec is locked — unlock to edit",
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
            raise HTTPException(
                status_code=429,
                detail=BUSY_ERROR_MESSAGE,
                headers={"Retry-After": "2"},
            ) from e
        except GitCommitError as e:
            session.rollback()
            logger.exception("spec save failed for workspace %s section %s", workspace_id, index)
            raise HTTPException(status_code=500, detail="save failed — nothing was written") from e
        except IndexError as e:
            session.rollback()
            raise HTTPException(status_code=404, detail="no such section") from e
    return SaveResultOut.from_result(result)


@router.post("/workspaces/{workspace_id}/spec", response_model=SpecOut)
def post_init_spec(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SpecOut:
    """Idempotent Spec initialization (owner-gated). If proposal-spec.md is
    whitespace-only, seed SPEC_SKELETON as ONE whole-doc commit (sidestepping the
    preamble structure guard); otherwise no-op. Returns the SpecOut either way."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        spec = repo_dir / SPEC_FILENAME
        if not spec.exists():
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet")
        try:
            acquire_commit_mutex(session, ws.id)
            # post_init_spec bypasses save_section, so it needs its OWN lock
            # guard. Check under the mutex (like the lock/unlock routes) BEFORE
            # the whitespace/no-op branch, so a locked spec is 409 regardless of
            # file content.
            session.refresh(ws, ["spec_locked_at"])
            if ws.spec_locked_at is not None:
                raise SpecLockedError(str(ws.id))
            raw = spec.read_text()  # read UNDER the mutex so a concurrent init serializes
            if raw.strip() == "":
                commit_sha = write_and_commit_files(
                    repo_dir, {SPEC_FILENAME: SPEC_SKELETON}, "user spec init"
                )
                session.add(
                    ArtifactVersion(
                        workspace_id=ws.id,
                        commit_sha=commit_sha,
                        created_by="user",
                        message="user spec init",
                    )
                )
                session.add(
                    WorkspaceEvent(
                        workspace_id=ws.id,
                        kind="user_edit",
                        actor_id=user.id,
                        payload={"artifact": "spec", "commit_sha": commit_sha},
                    )
                )
                # Snapshot the response WHILE STILL HOLDING the mutex: we just
                # committed exactly SPEC_SKELETON at commit_sha.
                head, sections = commit_sha, _sections_out(SPEC_SKELETON)
            else:
                # No-op: capture head + sections UNDER the lock too. Reading them
                # after releasing the mutex (Codex Medium) would let a writer that
                # grabs the lock the instant we release expose an uncommitted /
                # rolled-back write, or a head/section snapshot from two moments.
                head, sections = current_head(repo_dir), _sections_out(raw)
            session.commit()  # releases the mutex; persists rows iff we seeded
        except SpecLockedError as e:
            session.rollback()
            raise HTTPException(
                status_code=409,
                detail="spec is locked — unlock to edit",
            ) from e
        except CommitMutexTimeout as e:
            session.rollback()
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        except GitCommitError as e:
            session.rollback()
            logger.exception("spec init failed for workspace %s", workspace_id)
            raise HTTPException(
                status_code=500, detail="spec init failed — nothing was written"
            ) from e
    return SpecOut(head=head, can_edit=True, sections=sections)


@router.post("/workspaces/{workspace_id}/spec/lock", response_model=SpecLockOut)
def post_lock_spec(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SpecLockOut:
    """Lock the spec (owner-gated). Refuses an empty spec (409). Idempotent: a
    re-lock returns the current state and writes no new audit row."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        spec_path = repo_dir / SPEC_FILENAME
        if not spec_path.exists():
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet")
        try:
            acquire_commit_mutex(session, ws.id)
            # Re-read under the lock: a concurrent toggle may have committed
            # while we waited on the mutex.
            session.refresh(ws, ["spec_locked_at", "spec_locked_by"])
            if ws.spec_locked_at is None:
                if spec_path.read_text().strip() == "":
                    raise HTTPException(status_code=409, detail="cannot lock an empty spec")
                ws.spec_locked_at = datetime.now(UTC)
                ws.spec_locked_by = user.id
                session.add(
                    SpecLockEvent(
                        workspace_id=ws.id,
                        action="lock",
                        actor_id=user.id,
                        artifact_commit_sha=current_head(repo_dir),
                    )
                )
            locked_at, locked_by_id = ws.spec_locked_at, ws.spec_locked_by
            session.commit()
        except CommitMutexTimeout as e:
            session.rollback()
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        except GitCommitError as e:
            # Spec file present but no committable HEAD (unborn branch): mirror
            # the /spec read/save routes' 404 rather than a sanitized 500.
            session.rollback()
            logger.warning("workspace %s: git HEAD unavailable (%s)", workspace_id, e)
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet") from e
        locked_by = _locker_email(session, locked_by_id)
    return SpecLockOut(locked=True, locked_at=locked_at, locked_by=locked_by)


@router.post("/workspaces/{workspace_id}/spec/unlock", response_model=SpecLockOut)
def post_unlock_spec(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SpecLockOut:
    """Unlock the spec (owner-gated). Idempotent: unlocking an unlocked spec is
    a no-op that writes no audit row."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        if not (repo_dir / SPEC_FILENAME).exists():
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet")
        try:
            acquire_commit_mutex(session, ws.id)
            session.refresh(ws, ["spec_locked_at", "spec_locked_by"])
            if ws.spec_locked_at is not None:
                ws.spec_locked_at = None
                ws.spec_locked_by = None
                session.add(
                    SpecLockEvent(
                        workspace_id=ws.id,
                        action="unlock",
                        actor_id=user.id,
                        artifact_commit_sha=current_head(repo_dir),
                    )
                )
            session.commit()
        except CommitMutexTimeout as e:
            session.rollback()
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        except GitCommitError as e:
            session.rollback()
            logger.warning("workspace %s: git HEAD unavailable (%s)", workspace_id, e)
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet") from e
    return SpecLockOut(locked=False, locked_at=None, locked_by=None)


@router.get(
    "/workspaces/{workspace_id}/citations/{evidence_id}/source",
    response_model=CitationSourceOut,
)
def get_citation_source(
    workspace_id: uuid.UUID,
    evidence_id: str,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> CitationSourceOut:
    """The cited source for ONE evidence id, for the cockpit citation peek.

    Member-gated, read-only. JSON analogue of the Jinja citation_reader_fragment
    (proposal_assist/web/citations.py): resolve the ledger row -> reference (
    ownership-guarded) -> chunk + extracted text, then return the (windowed) text
    plus a client-side [start,end) highlight. An unknown evidence id or a
    missing/cross-workspace reference is a 404; a not-yet-indexed source returns
    200 with no_text=True (so the peek shows a notice, not an error)."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        citations_path = repo_dir / CITATIONS_FILENAME
        try:
            ledger = load_ledger(citations_path.read_text() if citations_path.exists() else None)
        except (ValueError, OSError) as e:
            raise HTTPException(status_code=404, detail="citations are unavailable") from e
        ev = find_by_id(ledger, evidence_id)
        if ev is None:
            raise HTTPException(status_code=404, detail="no such evidence id")
        # Resolve + ownership-guard the reference (never leak a cross-workspace ref).
        ref = None
        try:
            ref_uuid = uuid.UUID(str(ev.get("reference_id")))
        except (ValueError, TypeError):
            ref_uuid = None
        if ref_uuid is not None:
            ref = session.get(Reference, ref_uuid)
        if ref is None or ref.workspace_id != workspace_id:
            raise HTTPException(status_code=404, detail="the cited reference is unavailable")
        # Resolve the cited chunk (if the ledger row names one). A hand-edited
        # ledger may carry a non-dict locator or a non-int chunk_index; coerce
        # defensively so a malformed-but-valid-JSON row degrades, never 500s.
        loc = ev.get("locator")
        locator = loc if isinstance(loc, dict) else {}
        chunk = None
        ci = locator.get("chunk_index")
        if isinstance(ci, int) and not isinstance(ci, bool):
            chunk = session.scalar(
                select(ReferenceChunk).where(
                    ReferenceChunk.reference_id == ref.id,
                    ReferenceChunk.chunk_index == ci,
                    ReferenceChunk.workspace_id == workspace_id,
                )
            )
        # Extracted text only when indexing is complete (mirrors the Jinja route).
        storage = ReferenceStorage(settings.workspaces_dir)
        text_path = storage.text_path_for(workspace_id=workspace_id, reference_id=ref.id)
        text_indexed = ref.text_status == "done"
        full_text = (
            text_path.read_text(encoding="utf-8") if text_indexed and text_path.exists() else None
        )
        # Position among this reference's live cites (document order) for "i of N".
        proposal_path = repo_dir / PROPOSAL_FILENAME
        proposal_text = proposal_path.read_text() if proposal_path.exists() else ""
        order = citation_reader.evidence_order_for_reference(
            proposal_text, ledger, ev["reference_id"]
        )
        total = len(order)
        pos = order.index(evidence_id) if evidence_id in order else -1
        idx = pos + 1 if pos >= 0 else 0
        prev_ev = order[pos - 1] if pos > 0 else None
        next_ev = order[pos + 1] if 0 <= pos < total - 1 else None
        no_text = not text_indexed
        if no_text:
            text, highlight, note = "", None, None
        else:
            quote = ev.get("evidence_quote")
            text, highlight, note = citation_reader.build_source_window(
                full_text, chunk.text if chunk else None, quote if isinstance(quote, str) else ""
            )
        csl = ref.csl_json or ref.csl_json_extracted or {}
        csl_title = csl.get("title") if isinstance(csl, dict) else None
        title = ref.title or (csl_title if isinstance(csl_title, str) else None) or ref.filename
        bits = [ref.citekey] if ref.citekey else []
        bits.append(str(title))
        source_line = " · ".join(b for b in bits if b)
        # Coerce the remaining ledger-sourced fields that flow into the typed
        # response — a non-str value (malformed-but-valid-JSON row) would 500 the
        # endpoint at Pydantic validation otherwise.
        cc = ev.get("claim_context")
        claim = cc.get("text") if isinstance(cc, dict) else None
        stance = ev.get("stance")
        confidence = ev.get("confidence")
        return CitationSourceOut(
            evidence_id=evidence_id,
            reference_id=str(ref_uuid),
            source_line=source_line,
            claim=claim if isinstance(claim, str) else None,
            stance=stance if isinstance(stance, str) else None,
            confidence=confidence if isinstance(confidence, str) else None,
            index=idx,
            total=total,
            prev_ev=prev_ev,
            next_ev=next_ev,
            tombstoned=ref.tombstoned_at is not None,
            no_text=no_text,
            note=note,
            text=text,
            highlight=list(highlight) if highlight is not None else None,
        )


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
    sf = request.app.state.session_factory
    with sf() as session:
        # Authorize BEFORE touching any repo/proposal state, mirroring the HTMX
        # sibling (sections.py:233 require_owner, then :240 read). Doing the
        # pre-read probe first would let a non-owner distinguish "no proposal.md
        # yet" (404) from "owner only" (403) and leak the draft-lifecycle state of
        # any workspace UUID they can name — so require_owner runs first.
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
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
            # On the JSON path a 503 would be sanitized to a hard "internal_error"
            # by the global >=500 handler (main.py:335), discarding BUSY_ERROR_MESSAGE
            # so the SPA can't tell "busy" from a real 500. Map to a retriable 429
            # (too_many_requests) with Retry-After — the 4xx branch (main.py:344)
            # echoes the busy message verbatim. Mirrors references.py:275-276.
            raise HTTPException(
                status_code=429,
                detail=BUSY_ERROR_MESSAGE,
                headers={"Retry-After": "2"},
            ) from e
        except GitCommitError as e:
            session.rollback()
            logger.exception("section save failed for workspace %s section %s", workspace_id, index)
            raise HTTPException(status_code=500, detail="save failed — nothing was written") from e
        except IndexError as e:
            session.rollback()
            raise HTTPException(status_code=404, detail="no such section") from e
    return SaveResultOut.from_result(result)


@router.post("/workspaces/{workspace_id}/citations", response_model=AddCitationOut)
def post_citation(
    workspace_id: uuid.UUID,
    body: AddCitationIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> AddCitationOut:
    """Human select-to-cite: a direct citation commit (owner-gated). Sync handler
    (FastAPI threadpools it) calling the sync insert_citation_for_user core; this
    handler owns the transaction (single commit/rollback, mutex released on it)."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        # 404 precheck (mirror the save route): proposal.md + git HEAD must exist.
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        if not (repo_dir / PROPOSAL_FILENAME).exists():
            raise HTTPException(status_code=404, detail="no proposal.md yet")
        try:
            current_head(repo_dir)
        except GitCommitError as e:
            logger.warning("workspace %s: git HEAD unavailable (%s)", workspace_id, e)
            raise HTTPException(status_code=404, detail="no proposal.md yet") from e
        try:
            result = insert_citation_for_user(
                session=session,
                workspace=ws,
                user_id=user.id,
                section_index=body.section_index,
                base_commit_sha=body.base_commit_sha,
                base_section_sha=body.base_section_sha,
                anchor=body.anchor,
                reference_id=body.reference_id,
                chunk_index=body.chunk_index,
                evidence_quote=body.evidence_quote,
                quote_start=body.quote_start,
                quote_end=body.quote_end,
                stance=body.stance,
                workspaces_dir=settings.workspaces_dir,
            )
            session.commit()
        except CiteStale as e:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(e)) from e
        except CiteRefNotFound as e:
            session.rollback()
            raise HTTPException(status_code=404, detail=str(e)) from e
        except CiteInvalid as e:
            session.rollback()
            raise HTTPException(status_code=422, detail=str(e)) from e
        except CommitMutexTimeout as e:
            session.rollback()
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        except GitCommitError as e:
            session.rollback()
            logger.exception("user citation insert failed for workspace %s", workspace_id)
            raise HTTPException(
                status_code=500, detail="citation failed — nothing was written"
            ) from e
    return AddCitationOut(**result)


class ReferenceListItemOut(BaseModel):
    id: str
    citekey: str | None = None
    title: str
    kind: str
    text_status: str


@router.get("/workspaces/{workspace_id}/references", response_model=list[ReferenceListItemOut])
def list_references(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> list[ReferenceListItemOut]:
    """The workspace's citable references for the select-to-cite picker. Member-gated."""
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        refs = session.scalars(
            select(Reference)
            .where(Reference.workspace_id == workspace_id, Reference.tombstoned_at.is_(None))
            .order_by(Reference.created_at.desc())
        ).all()
        out: list[ReferenceListItemOut] = []
        for ref in refs:
            csl = ref.csl_json or ref.csl_json_extracted or {}
            csl_title = csl.get("title") if isinstance(csl, dict) else None
            title = ref.title or (csl_title if isinstance(csl_title, str) else None) or ref.filename
            out.append(
                ReferenceListItemOut(
                    id=str(ref.id),
                    citekey=ref.citekey,
                    title=str(title),
                    kind=ref.kind,
                    text_status=ref.text_status,
                )
            )
    return out


class ChunkOut(BaseModel):
    chunk_index: int
    section_path: str | None = None
    text: str


class ReferenceChunksOut(BaseModel):
    reference_id: str
    title: str
    chunks: list[ChunkOut]


@router.get(
    "/workspaces/{workspace_id}/references/{reference_id}/chunks",
    response_model=ReferenceChunksOut,
)
def get_reference_chunks(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> ReferenceChunksOut:
    """A reference's chunks (verbatim text), for selecting a supporting span.
    Member-gated; ownership-guarded (cross-workspace reference -> 404)."""
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        ref = session.get(Reference, reference_id)
        if ref is None or ref.workspace_id != workspace_id or ref.tombstoned_at is not None:
            raise HTTPException(status_code=404, detail="reference not found")
        csl = ref.csl_json or ref.csl_json_extracted or {}
        csl_title = csl.get("title") if isinstance(csl, dict) else None
        title = ref.title or (csl_title if isinstance(csl_title, str) else None) or ref.filename
        rows = session.scalars(
            select(ReferenceChunk)
            .where(
                ReferenceChunk.reference_id == ref.id,
                ReferenceChunk.workspace_id == workspace_id,
            )
            .order_by(ReferenceChunk.chunk_index)
        ).all()
        chunks = [
            ChunkOut(chunk_index=c.chunk_index, section_path=c.section_path, text=c.text)
            for c in rows
        ]
    return ReferenceChunksOut(reference_id=str(reference_id), title=str(title), chunks=chunks)
