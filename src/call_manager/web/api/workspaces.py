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

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from starlette.concurrency import run_in_threadpool

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import (
    ArtifactVersion,
    Call,
    Reference,
    ReferenceChunk,
    SpecLockEvent,
    User,
    Workspace,
    WorkspaceEvent,
    WorkspaceSetupJob,
    WorkspaceViewer,
)
from call_manager.proposal_assist import row_locks
from call_manager.proposal_assist.artifacts import SPEC_FILENAME, SPEC_SKELETON, Artifact
from call_manager.proposal_assist.citations import reader as citation_reader
from call_manager.proposal_assist.citations.collab_cite import (
    direct_cite_via_kernel,
    init_spec_via_kernel,
)
from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    current_head,
    read_branch_file,
    read_file_at_commit,
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
from call_manager.proposal_assist.collab import state as collab_state
from call_manager.proposal_assist.collab.canonical import canonical_artifact_markdown
from call_manager.proposal_assist.collab.checkpoint import checkpoint_artifact
from call_manager.proposal_assist.collab.flip import maybe_auto_enable
from call_manager.proposal_assist.collab.permissions import signal_from_request
from call_manager.proposal_assist.collab.ws_auth import resolve_access
from call_manager.proposal_assist.drafting.locking import (
    BUSY_ERROR_MESSAGE,
    CommitMutexTimeout,
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
from call_manager.proposal_assist.fencing import acquire_commit_mutex_at_generation
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.ports.stamp import FenceStamp, stamp_for_workspace
from call_manager.proposal_assist.proposals.kernel_accept import RoomEvicted
from call_manager.proposal_assist.references.storage import ReferenceStorage
from call_manager.proposal_assist.retirement import signal_workspace_retirement
from call_manager.proposal_assist.web._perms import require_member, require_owner
from call_manager.web.profile_access import can_read_profile
from call_manager.workspaces import (
    create_workspace,
    soft_delete_workspace_db,
)

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
    branch: str
    can_edit: bool
    sections: list[SectionOut]
    # ev_id -> citation display fields, computed over the WHOLE proposal.
    citations: dict[str, CitationOut]
    collab_enabled: bool
    can_write: bool


class SpecOut(BaseModel):
    head: str
    branch: str
    can_edit: bool
    sections: list[SectionOut]  # no citations: the spec is citation-free
    # Lock fields are required-nullable: every handler that builds a SpecOut
    # passes them explicitly, so the OpenAPI contract should mark them present
    # (and nullable), not optional.
    locked: bool
    locked_at: datetime | None
    locked_by: str | None
    can_manage_lock: bool
    collab_enabled: bool
    can_write: bool


class SpecLockOut(BaseModel):
    locked: bool
    locked_at: datetime | None
    locked_by: str | None


class SaveSectionIn(BaseModel):
    content: str
    base_commit_sha: str
    base_section_sha: str
    # REQUIRED, not `str | None = None`: an optional branch is a silently
    # skipped check, and this field exists precisely because the other two
    # cannot tell a branch switch from an ordinary head move. The SPA ships with
    # the server, so a 422 on a client that omits it is the correct answer.
    base_branch: str


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
    # None on an idempotent re-cite of a citekey-less/corrupt ledger row —
    # kernel.py carries str | None end-to-end (3b-5a deferral, fixed at the flip).
    citekey: str | None
    commit_sha: str


def _citation_map(
    session, workspace_id: uuid.UUID, raw: str, ledger_text: str | None
) -> dict[str, CitationOut]:
    """Build {ev_id: CitationOut} over the WHOLE proposal `raw`.

    number_by_ev/stance_by_ev are proposal-wide; citekey comes from the marker;
    title is resolved per ev_id via the ledger -> Reference row. A missing or
    corrupt ledger yields an EMPTY map (never raises) — same degrade rule as
    _citation_render_maps_for.
    """
    try:
        ledger = load_ledger(ledger_text)
    except ValueError:
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


def _lock_artifact_read_snapshot(session, workspace: Workspace) -> None:
    try:
        with row_locks.bounded_lock_waits(
            session,
            timeout_seconds=row_locks.WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS,
            message="timed out waiting for a stable artifact snapshot",
        ):
            session.refresh(workspace, with_for_update={"read": True})
    except row_locks.RowLockTimeout as exc:
        raise HTTPException(
            status_code=429,
            detail=BUSY_ERROR_MESSAGE,
            headers={"Retry-After": "2"},
        ) from exc


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
        # Pair the authoritative branch with its collab log in one committed
        # workspace state. A switch either waits before its row update or this
        # refresh waits until the checkout + log handover commits.
        _lock_artifact_read_snapshot(session, ws)
        stamp = stamp_for_workspace(ws)  # minted at the access check (endgame Task 5)
        branch = ws.current_branch
        can_edit = ws.owner_id == user.id
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        try:
            head, raw_disk = read_branch_file(repo_dir, branch, PROPOSAL_FILENAME)
        except GitCommitError as e:
            raise HTTPException(status_code=404, detail="no proposal.md yet") from e
        try:
            ledger_text = read_file_at_commit(repo_dir, head, CITATIONS_FILENAME)
        except GitCommitError:
            ledger_text = None
        # 3b-5d.D4 enable-on-touch: the first read of an artifact flips collab
        # on (sticky, per-artifact, kill-switch-suppressed) and THIS response
        # already reports collab_enabled=true.
        collab_enabled = maybe_auto_enable(
            session,
            stamp=stamp,
            workspace_id=workspace_id,
            artifact="draft",
        )
        if collab_enabled:
            raw = canonical_artifact_markdown(
                session,
                workspace_id=workspace_id,
                artifact="draft",
                repo_dir=repo_dir,
                head_markdown=raw_disk,
            )
        else:
            raw = raw_disk
        sections = _sections_out(raw)
        citations = _citation_map(session, workspace_id, raw, ledger_text)
        _, can_write = resolve_access(
            session, user=user, workspace_id=workspace_id, artifact="draft"
        )
    return DraftOut(
        head=head,
        branch=branch,
        can_edit=can_edit,
        sections=sections,
        citations=citations,
        collab_enabled=collab_enabled,
        can_write=can_write,
    )


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
        _lock_artifact_read_snapshot(session, ws)
        stamp = stamp_for_workspace(ws)  # minted at the access check (endgame Task 5)
        branch = ws.current_branch
        is_owner = ws.owner_id == user.id
        locked = ws.spec_locked_at is not None
        can_edit = is_owner and not locked
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        try:
            head, raw_disk = read_branch_file(repo_dir, branch, SPEC_FILENAME)
        except GitCommitError as e:
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet") from e
        collab_enabled = maybe_auto_enable(
            session,
            stamp=stamp,
            workspace_id=workspace_id,
            artifact="spec",
        )
        raw = (
            canonical_artifact_markdown(
                session,
                workspace_id=workspace_id,
                artifact="spec",
                repo_dir=repo_dir,
                head_markdown=raw_disk,
            )
            if collab_enabled
            else raw_disk
        )
        sections = _sections_out(raw)
        # Resolve the lock display fields WHILE the session is open: _locker_email
        # queries it, so this must run before the `with` block closes.
        locked_by = _locker_email(session, ws.spec_locked_by)
        locked_at = ws.spec_locked_at
        _, can_write = resolve_access(
            session, user=user, workspace_id=workspace_id, artifact="spec"
        )
    return SpecOut(
        head=head,
        branch=branch,
        can_edit=can_edit,
        sections=sections,
        locked=locked,
        locked_at=locked_at,
        locked_by=locked_by,
        can_manage_lock=is_owner,
        collab_enabled=collab_enabled,
        can_write=can_write,
    )


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
        stamp = stamp_for_workspace(ws)  # minted at the access check (endgame Task 6)
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        if not (repo_dir / SPEC_FILENAME).exists():
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet")
        try:
            current_head(repo_dir)
        except GitCommitError as e:
            logger.warning("workspace %s: git HEAD unavailable (%s)", workspace_id, e)
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet") from e
        # 3b-5d.D4 severance: a collab-enabled artifact is Tiptap-only. A stale
        # CM6 tab (or direct API call) must not git-first-write around the live
        # room. 409 tells the client to reload (it will mount the collab editor).
        # Under the kill-switch the flag reads False -> git-first saves resume.
        if collab_state.is_collab_enabled(session, workspace_id=workspace_id, artifact="spec"):
            raise HTTPException(
                status_code=409,
                detail=collab_state.COLLAB_SEVERED_DETAIL,
            )
        try:
            result = save_section(
                session=session,
                workspace=ws,
                stamp=stamp,
                user_id=user.id,
                section_index=index,
                new_text=body.content,
                base_commit_sha=body.base_commit_sha,
                base_section_sha=body.base_section_sha,
                base_branch=body.base_branch,
                workspaces_dir=settings.workspaces_dir,
                artifact=Artifact.SPEC,
            )
            session.commit()
        except collab_state.CollabSeveranceConflict as e:
            # enable-on-touch flipped spec collab on AFTER the pre-mutex guard but
            # while save_section held the mutex — same 409 as that guard.
            session.rollback()
            raise HTTPException(
                status_code=409, detail=collab_state.COLLAB_SEVERED_DETAIL
            ) from e
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
async def post_init_spec(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SpecOut:
    """Idempotent Spec initialization (owner-gated). Flag-on: the skeleton seeds
    through the live room (kernel) — provenance comes from the collab checkpoint
    (ArtifactVersion "collab checkpoint: spec" / WorkspaceEvent kind=
    "collab_checkpoint"), NOT a user_edit row (3b-5d.D4 §3.3 deliberate deviation
    — do not duplicate provenance rows on the kernel path). Flag-off: the legacy
    whole-doc git commit, byte-for-byte."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory

    def _route() -> bool:
        with sf() as session:
            return collab_state.is_collab_enabled(
                session, workspace_id=workspace_id, artifact="spec"
            )

    enabled = await run_in_threadpool(_route)

    if enabled:
        def _gate() -> tuple[FenceStamp, str]:
            with sf() as session:
                ws = require_owner(workspace_id=workspace_id, user=user, session=session)
                # Mint the fence stamp at the owner access check (endgame Task 5):
                # kernel_init_spec re-validates THIS generation under the commit
                # mutex, in a separate session, never a read at the write.
                stamp = stamp_for_workspace(ws)
                branch = ws.current_branch
            if not (
                workspace_repo_dir(settings.workspaces_dir, workspace_id) / SPEC_FILENAME
            ).exists():
                raise HTTPException(status_code=404, detail="no proposal-spec.md yet")
            return stamp, branch
        stamp, branch = await run_in_threadpool(_gate)
        try:
            head, md = await init_spec_via_kernel(
                session_factory=sf, workspaces_dir=settings.workspaces_dir,
                stamp=stamp, workspace_id=workspace_id, actor_id=user.id,
            )
        except SpecLockedError as e:
            raise HTTPException(
                status_code=409, detail="spec is locked — unlock to edit"
            ) from e
        except CommitMutexTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        except RoomEvicted as e:
            raise HTTPException(
                status_code=409, detail="the live session was reset — retry"
            ) from e
        except GitCommitError as e:
            logger.exception("collab spec init failed for workspace %s", workspace_id)
            raise HTTPException(
                status_code=500, detail="spec init failed — nothing was written"
            ) from e
        return SpecOut(
            head=head,
            branch=branch,
            can_edit=True,
            sections=_sections_out(md),
            locked=False,
            locked_at=None,
            locked_by=None,
            can_manage_lock=True,
            collab_enabled=True,
            can_write=True,
        )

    def _git() -> SpecOut:
        with sf() as session:
            ws = require_owner(workspace_id=workspace_id, user=user, session=session)
            stamp = stamp_for_workspace(ws)  # minted at the access check (endgame Task 4)
            branch = ws.current_branch
            repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
            spec = repo_dir / SPEC_FILENAME
            if not spec.exists():
                raise HTTPException(status_code=404, detail="no proposal-spec.md yet")
            try:
                # Flag-off-only path: with collab enabled the severance recheck immediately below
                # raises CollabSeveranceConflict, which the route maps to 409, so this path is
                # retiring rather than maintained. Slice C3 bounds its tail anyway — the 2s cap now
                # left in force by acquire_commit_mutex applies at every site, including this one,
                # with nothing needed here.
                acquire_commit_mutex_at_generation(
                    session, stamp.workspace_id, generation=stamp.generation
                )
                # Severance recheck UNDER the mutex (3b-5d.D4 Item 2b): the _route()
                # fork read the flag as False, but enable-on-touch may have flipped
                # spec collab on while we blocked on the mutex. Abort writing NOTHING
                # (init_spec bypasses save_section, so it needs its OWN recheck).
                collab_state.raise_if_collab_severed(
                    session, workspace_id=workspace_id, artifact="spec"
                )
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
                # Read collab_enabled WHILE the session is open (the return is outside
                # the `with` block). can_write is unconditionally True here: require_owner
                # above + a successful init only runs on an UNLOCKED spec.
                collab_enabled = collab_state.is_collab_enabled(
                    session, workspace_id=workspace_id, artifact="spec"
                )
            except collab_state.CollabSeveranceConflict as e:
                # enable-on-touch flipped spec collab on while we held the mutex —
                # abort the git-first init, same 409 as the save routes' guard.
                session.rollback()
                raise HTTPException(
                    status_code=409, detail=collab_state.COLLAB_SEVERED_DETAIL
                ) from e
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
        # A successful init only runs on an UNLOCKED spec (locked -> SpecLockedError
        # -> 409 above), so the lock fields are always unset here. can_manage_lock=True
        # because require_owner above guarantees the caller owns the workspace.
        return SpecOut(
            head=head,
            branch=branch,
            can_edit=True,
            sections=sections,
            locked=False,
            locked_at=None,
            locked_by=None,
            can_manage_lock=True,
            collab_enabled=collab_enabled,
            can_write=True,
        )

    return await run_in_threadpool(_git)


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
        stamp = stamp_for_workspace(ws)  # minted at the access check (endgame Task 4)
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        spec_path = repo_dir / SPEC_FILENAME
        if not spec_path.exists():
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet")
        try:
            acquire_commit_mutex_at_generation(
                session, stamp.workspace_id, generation=stamp.generation
            )
            # Fresh flag read UNDER the mutex (Codex): a pre-mutex read can go
            # stale while we block — enable-on-touch commits the enable while
            # HOLDING this mutex, so an under-mutex read is authoritative. If it
            # reads False the flip is queued behind us (no room can exist until we
            # release: the WS gate reads the flag), so stamping git HEAD is
            # correct; if True the checkpoint branch runs with the fresh state.
            # This route is room-aware — the remedy is the fresh read feeding the
            # existing branch, NOT the git-first writers' severance 409. First
            # CollabArtifactState load in this session (no stale identity-map hit).
            is_collab = collab_state.is_collab_enabled(
                session, workspace_id=ws.id, artifact="spec"
            )
            if is_collab:
                cp = checkpoint_artifact(
                    session,
                    stamp=stamp,
                    workspace_id=ws.id,
                    artifact="spec",
                    workspaces_dir=settings.workspaces_dir,
                )
                frozen_sha = cp.commit_sha
                if not frozen_sha:
                    # No committable HEAD (unborn / repo-less): checkpoint_artifact
                    # returns commit_sha="" WITHOUT raising.  Mirror the flag-off
                    # current_head -> GitCommitError -> 404 mapping rather than
                    # stamping an empty SHA and locking anyway (Codex Medium).
                    session.rollback()
                    raise HTTPException(status_code=404, detail="no proposal-spec.md yet")
            # Re-read under the lock: a concurrent toggle may have committed
            # while we waited on the mutex.
            session.refresh(ws, ["spec_locked_at", "spec_locked_by"])
            if ws.spec_locked_at is None:
                if spec_path.read_text().strip() == "":
                    raise HTTPException(status_code=409, detail="cannot lock an empty spec")
                ws.spec_locked_at = datetime.now(UTC)
                ws.spec_locked_by = user.id
                # Compute the SHA conditionally AT THE SpecLockEvent SITE so that on
                # the flag-off path current_head is still evaluated lazily inside the
                # transition (after the empty-check), EXACTLY as before.
                sha = frozen_sha if is_collab else current_head(repo_dir)
                session.add(
                    SpecLockEvent(
                        workspace_id=ws.id,
                        action="lock",
                        actor_id=user.id,
                        artifact_commit_sha=sha,
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
            # collab checkpoint write failure is a 500 (mirror collab_checkpoint.py),
            # NOT the flag-off "no committable HEAD" 404.  Distinguish by is_collab.
            session.rollback()
            if is_collab:
                raise HTTPException(
                    status_code=500, detail="lock failed — try again"
                ) from e
            # Spec file present but no committable HEAD (unborn branch): mirror
            # the /spec read/save routes' 404 rather than a sanitized 500.
            logger.warning("workspace %s: git HEAD unavailable (%s)", workspace_id, e)
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet") from e
        locked_by = _locker_email(session, locked_by_id)
        signal_from_request(request, workspace_id=workspace_id, artifacts=["spec"])
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
        stamp = stamp_for_workspace(ws)  # minted at the access check (endgame Task 4)
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        if not (repo_dir / SPEC_FILENAME).exists():
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet")
        try:
            acquire_commit_mutex_at_generation(
                session, stamp.workspace_id, generation=stamp.generation
            )
            # Fresh flag read UNDER the mutex (Codex) — see post_lock_spec for the
            # soundness argument; unlock shares the moved-read shape exactly.
            is_collab = collab_state.is_collab_enabled(
                session, workspace_id=ws.id, artifact="spec"
            )
            if is_collab:
                cp = checkpoint_artifact(
                    session,
                    stamp=stamp,
                    workspace_id=ws.id,
                    artifact="spec",
                    workspaces_dir=settings.workspaces_dir,
                )
                frozen_sha = cp.commit_sha
                if not frozen_sha:
                    # No committable HEAD (unborn / repo-less): same 404 guard as lock.
                    session.rollback()
                    raise HTTPException(status_code=404, detail="no proposal-spec.md yet")
            session.refresh(ws, ["spec_locked_at", "spec_locked_by"])
            if ws.spec_locked_at is not None:
                ws.spec_locked_at = None
                ws.spec_locked_by = None
                # Compute the SHA conditionally AT THE SpecLockEvent SITE so that on
                # the flag-off path current_head is still evaluated lazily inside the
                # transition, EXACTLY as before.
                sha = frozen_sha if is_collab else current_head(repo_dir)
                session.add(
                    SpecLockEvent(
                        workspace_id=ws.id,
                        action="unlock",
                        actor_id=user.id,
                        artifact_commit_sha=sha,
                    )
                )
            session.commit()
        except CommitMutexTimeout as e:
            session.rollback()
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        except GitCommitError as e:
            # collab checkpoint write failure is a 500 (mirror collab_checkpoint.py),
            # NOT the flag-off "no committable HEAD" 404.  Distinguish by is_collab.
            session.rollback()
            if is_collab:
                raise HTTPException(
                    status_code=500, detail="unlock failed — try again"
                ) from e
            logger.warning("workspace %s: git HEAD unavailable (%s)", workspace_id, e)
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet") from e
    signal_from_request(request, workspace_id=workspace_id, artifacts=["spec"])
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
        # Authorize BEFORE touching any repo/proposal state. Doing the pre-read
        # probe first would let a non-owner distinguish "no proposal.md yet" (404)
        # from "owner only" (403) and leak the draft-lifecycle state of any
        # workspace UUID they can name — so require_owner runs first.
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        stamp = stamp_for_workspace(ws)  # minted at the access check (endgame Task 6)
        # Before touching save_section, reject a workspace that has no proposal.md
        # or no git HEAD with the same user-facing 404 as the other proposal read
        # paths. save_section reads both directly and would otherwise surface a raw
        # FileNotFoundError or GitCommitError as a 500.
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        if not (repo_dir / PROPOSAL_FILENAME).exists():
            raise HTTPException(status_code=404, detail="no proposal.md yet")
        try:
            current_head(repo_dir)
        except GitCommitError as e:
            logger.warning("workspace %s: git HEAD unavailable (%s)", workspace_id, e)
            raise HTTPException(status_code=404, detail="no proposal.md yet") from e
        # 3b-5d.D4 severance: a collab-enabled artifact is Tiptap-only. A stale
        # CM6 tab (or direct API call) must not git-first-write around the live
        # room. 409 tells the client to reload (it will mount the collab editor).
        # Under the kill-switch the flag reads False -> git-first saves resume.
        if collab_state.is_collab_enabled(session, workspace_id=workspace_id, artifact="draft"):
            raise HTTPException(
                status_code=409,
                detail=collab_state.COLLAB_SEVERED_DETAIL,
            )
        try:
            result = save_section(
                session=session,
                workspace=ws,
                stamp=stamp,
                user_id=user.id,
                section_index=index,
                new_text=body.content,
                base_commit_sha=body.base_commit_sha,
                base_section_sha=body.base_section_sha,
                base_branch=body.base_branch,
                workspaces_dir=settings.workspaces_dir,
            )
            session.commit()
        except collab_state.CollabSeveranceConflict as e:
            # enable-on-touch flipped draft collab on AFTER the pre-mutex guard but
            # while save_section held the mutex — same 409 as that guard.
            session.rollback()
            raise HTTPException(
                status_code=409, detail=collab_state.COLLAB_SEVERED_DETAIL
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
async def post_citation(
    workspace_id: uuid.UUID,
    body: AddCitationIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> AddCitationOut:
    """Human select-to-cite: collab-aware citation commit.

    Flag-on:  any writer, routes to the live Yjs room via direct_cite_via_kernel.
    Flag-off: owner-only, sync git path (threadpooled), byte-for-byte parity.
    """
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)

    # _route decides ONLY the branch — no auth, no prechecks — so the flag-off path
    # keeps its exact owner-first ordering (Codex: a non-owner member must still see
    # 403 before any proposal-404, not the reverse). is_collab_enabled on a missing
    # workspace/row is a harmless bool; each branch below does its own full auth.
    def _route() -> bool:
        with sf() as session:
            return collab_state.is_collab_enabled(
                session, workspace_id=workspace_id, artifact="draft"
            )

    enabled = await run_in_threadpool(_route)

    if enabled:
        def _gate() -> FenceStamp:
            with sf() as session:
                is_member, can_write = resolve_access(
                    session, user=user, workspace_id=workspace_id, artifact="draft")
                if not is_member:
                    raise HTTPException(status_code=404, detail="workspace not found")
                if not can_write:
                    raise HTTPException(status_code=403, detail="not permitted to write this draft")
                # Mint the fence stamp from a bare-name load in the SAME transaction
                # as resolve_access above (this route's access check). apply_citation_
                # to_room re-validates THIS generation under the commit mutex, in a
                # separate session, never a read at the write (endgame Task 5).
                ws = session.get(Workspace, workspace_id)
                if ws is None or ws.soft_deleted_at is not None:
                    raise HTTPException(status_code=404, detail="workspace not found")
                stamp = stamp_for_workspace(ws)
            if not (repo_dir / PROPOSAL_FILENAME).exists():
                raise HTTPException(status_code=404, detail="no proposal.md yet")
            try:
                current_head(repo_dir)
            except GitCommitError as e:
                raise HTTPException(status_code=404, detail="no proposal.md yet") from e
            return stamp
        stamp = await run_in_threadpool(_gate)
        try:
            out = await direct_cite_via_kernel(
                session_factory=sf,
                workspaces_dir=settings.workspaces_dir,
                stamp=stamp,
                workspace_id=workspace_id,
                actor_id=user.id,
                section_index=body.section_index,
                base_section_sha=body.base_section_sha,
                anchor=body.anchor,
                reference_id=body.reference_id,
                chunk_index=body.chunk_index,
                evidence_quote=body.evidence_quote,
                quote_start=body.quote_start,
                quote_end=body.quote_end,
                stance=body.stance,
            )
        except CiteStale as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except CiteRefNotFound as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except CiteInvalid as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except IndexError as e:
            # Out-of-range section_index (e.g. a peer deleted the section mid-cite).
            # Parity with the flag-off path (insert_citation_for_user → CiteStale 409)
            # — a reachable concurrent-edit race must be a 4xx, not a 500.
            raise HTTPException(
                status_code=409, detail="this section changed — reopen and re-cite"
            ) from e
        except RoomEvicted as e:
            raise HTTPException(status_code=409, detail="workspace busy; retry the citation") from e
        except CommitMutexTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        except GitCommitError as e:
            logger.exception("collab citation insert failed for workspace %s", workspace_id)
            raise HTTPException(
                status_code=500, detail="citation failed — nothing was written"
            ) from e
        return AddCitationOut(**out)

    # Flag-off: the CURRENT handler body, byte-for-byte (owner FIRST, then the
    # proposal/head prechecks, then insert). Threadpooled. This ordering is the
    # parity guarantee asserted by test_post_citation_flag_off_still_owner_only.
    def _git() -> AddCitationOut:
        with sf() as session:
            ws = require_owner(workspace_id=workspace_id, user=user, session=session)
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
            except collab_state.CollabSeveranceConflict as e:
                # enable-on-touch flipped draft collab on AFTER _route() read False
                # but while insert_citation_for_user held the mutex — abort writing
                # NOTHING; same 409 as the save routes' severance guard.
                session.rollback()
                raise HTTPException(
                    status_code=409, detail=collab_state.COLLAB_SEVERED_DETAIL
                ) from e
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

    return await run_in_threadpool(_git)


class CockpitMetaOut(BaseModel):
    workspace_title: str
    workspace_deadline: datetime | None
    current_phase: str  # "setup" | "active"
    role: str  # "owner" | "member"
    spec_locked: bool
    member_count: int
    setup_status: str | None  # setup job status if available, None if no job
    setup_last_error: str | None  # last_error from setup job (owner-only)


@router.get("/workspaces/{workspace_id}/cockpit-meta", response_model=CockpitMetaOut)
def get_cockpit_meta(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> CockpitMetaOut:
    """Workspace header read-model for the cockpit status bar + title breadcrumb.
    Member-gated. Reads the workspace-level title/deadline (NOT the optional linked
    Call). member_count = owner + viewers (a non-PII aggregate shown to every member)."""
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        viewer_count = session.scalar(
            select(func.count())
            .select_from(WorkspaceViewer)
            .where(WorkspaceViewer.workspace_id == workspace_id)
        )
        job = session.scalar(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws.id)
        )
        is_owner = ws.owner_id == user.id
        return CockpitMetaOut(
            workspace_title=ws.title,
            workspace_deadline=ws.deadline,
            current_phase=ws.current_phase,
            role="owner" if is_owner else "member",
            spec_locked=ws.spec_locked_at is not None,
            member_count=1 + int(viewer_count or 0),
            setup_status=job.status if job is not None else None,
            setup_last_error=(job.last_error if job is not None else None) if is_owner else None,
        )


class MemberOut(BaseModel):
    user_id: int
    email: str
    role: str  # "owner" | "member"


@router.get("/workspaces/{workspace_id}/members", response_model=list[MemberOut])
def list_members(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> list[MemberOut]:
    """Owner + viewers for the Members sidebar group. OWNER-GATED — mirrors the
    settings viewer list, so member emails stay visible to the owner only (no PII
    expansion; non-owners get only the member_count from cockpit-meta). Owner first."""
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        owner = session.get(User, ws.owner_id)
        viewers = session.scalars(
            select(User)
            .join(WorkspaceViewer, WorkspaceViewer.user_id == User.id)
            .where(WorkspaceViewer.workspace_id == workspace_id)
            .order_by(User.email)
        ).all()
        out: list[MemberOut] = []
        if owner is not None:
            out.append(MemberOut(user_id=owner.id, email=owner.email, role="owner"))
        out.extend(MemberOut(user_id=v.id, email=v.email, role="member") for v in viewers)
        return out


class VersionOut(BaseModel):
    commit_sha: str
    message: str
    created_by: str
    created_at: datetime


@router.get("/workspaces/{workspace_id}/versions", response_model=list[VersionOut])
def list_versions(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> list[VersionOut]:
    """Recent artifact versions (git commits) for the Versions sidebar group.
    Member-gated, newest-first, capped at 50."""
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        rows = session.scalars(
            select(ArtifactVersion)
            .where(ArtifactVersion.workspace_id == workspace_id)
            .order_by(ArtifactVersion.created_at.desc())
            .limit(50)
        ).all()
        return [
            VersionOut(
                commit_sha=v.commit_sha,
                message=v.message,
                created_by=v.created_by,
                created_at=v.created_at,
            )
            for v in rows
        ]


class ReferenceListItemOut(BaseModel):
    id: str
    citekey: str | None = None
    title: str
    kind: str
    text_status: str
    # Slice C (Library) additive fields — sidebar useReferences ignores these.
    metadata_status: str
    metadata_waiver: bool
    text_error: str | None = None
    filename: str


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
                    metadata_status=ref.metadata_status,
                    metadata_waiver=ref.metadata_waiver,
                    text_error=ref.text_error,
                    filename=ref.filename,
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


# --- Slice A: workspace collection (list + create) -------------------------
# Mirrors the ck_workspaces_status CHECK constraint (db/models.py) — the single
# source of truth for the ?status allowlist.
_WORKSPACE_STATUSES = frozenset({"drafting", "submitted", "awarded", "rejected", "abandoned"})
_WORKSPACE_LIST_LIMIT = 200


class WorkspaceListItemOut(BaseModel):
    id: str
    title: str
    status: str  # drafting|submitted|awarded|rejected|abandoned
    current_phase: str  # "setup" | "active"
    deadline: datetime | None
    updated_at: datetime
    linked_call_title: str | None  # None when call_id is null (call deleted)
    is_owner: bool


@router.get("/workspaces", response_model=list[WorkspaceListItemOut])
def list_workspaces_api(
    request: Request,
    status: str | None = None,
    user: User = Depends(get_current_user),  # noqa: B008
) -> list[WorkspaceListItemOut]:
    """Workspaces visible to the user (owned OR viewer-of), not soft-deleted,
    most-recently-updated first, capped. Optional ?status filter (allowlisted).
    Mirrors the Jinja list_workspaces query; powers the React /cockpit list."""
    if status is not None and status not in _WORKSPACE_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status filter")
    sf = request.app.state.session_factory
    with sf() as session:
        viewer_ids = session.scalars(
            select(WorkspaceViewer.workspace_id).where(WorkspaceViewer.user_id == user.id)
        ).all()
        q = (
            select(Workspace)
            .where(
                Workspace.soft_deleted_at.is_(None),
                or_(Workspace.owner_id == user.id, Workspace.id.in_(viewer_ids)),
            )
            .order_by(Workspace.updated_at.desc())
            .limit(_WORKSPACE_LIST_LIMIT)
        )
        if status:
            q = q.where(Workspace.status == status)
        rows = list(session.scalars(q))
        # Batch the linked-call titles in ONE query (no N+1); a row whose call was
        # deleted (call_id SET NULL) simply has no title.
        call_ids = {w.call_id for w in rows if w.call_id is not None}
        titles = (
            dict(session.execute(select(Call.id, Call.title).where(Call.id.in_(call_ids))).all())
            if call_ids
            else {}
        )
        return [
            WorkspaceListItemOut(
                id=str(w.id),
                title=w.title,
                status=w.status,
                current_phase=w.current_phase,
                deadline=w.deadline,
                updated_at=w.updated_at,
                linked_call_title=titles.get(w.call_id),
                is_owner=(w.owner_id == user.id),
            )
            for w in rows
        ]


_MAX_WORKSPACE_TITLE_LEN = 500


class WorkspaceCreateIn(BaseModel):
    call_id: int
    title: str
    deadline: str | None = None  # ISO-8601; parsed + validated server-side
    profile_id: int | None = None  # the dashboard's resolved effective profile


class WorkspaceCreatedOut(BaseModel):
    id: str


def _parse_deadline(raw: str | None) -> datetime | None:
    """Parse an optional ISO-8601 deadline; 400 on a malformed string (mirrors the
    Jinja _parse_deadline_form, without the Form coupling)."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="invalid deadline (expected ISO-8601)") from e


@router.post("/workspaces", response_model=WorkspaceCreatedOut, status_code=201)
def create_workspace_api(
    body: WorkspaceCreateIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> WorkspaceCreatedOut:
    """From-call workspace creation. Validates first (clean 400, not a FK 500),
    then reuses create_workspace (sets current_phase='setup' + enqueues Phase-0).
    Returns the new id so the SPA can navigate to ``/cockpit/{id}``."""
    title = body.title.strip()
    if not title or len(title) > _MAX_WORKSPACE_TITLE_LEN:
        raise HTTPException(status_code=400, detail="invalid title")
    deadline = _parse_deadline(body.deadline)
    sf = request.app.state.session_factory
    with sf() as session:
        if session.get(Call, body.call_id) is None:
            raise HTTPException(status_code=400, detail=f"No such call: {body.call_id}")
        # READABILITY, not existence. This check used to be
        # `session.get(Profile, ...) is None`, which answered 400 for a profile
        # that does not exist and 201 for one the caller may not read -- an
        # existence oracle on every profile id, on a route whose name says
        # nothing about profiles (PD-9). It is not only an oracle: Phase-0 copies
        # the profile's `description` into `ws.profile_desc_snapshot`, which the
        # drafting agent's read-only get_profile tool hands back on a plain ask.
        # can_read_profile() is False for a nonexistent id too, so the unreadable
        # and the missing id keep the IDENTICAL detail string -- one answer, no
        # oracle. Same reasoning as profiles.py's 404-not-403.
        if body.profile_id is not None and not can_read_profile(session, user, body.profile_id):
            raise HTTPException(status_code=400, detail=f"No such profile: {body.profile_id}")
        ws = create_workspace(
            session=session,
            owner_id=user.id,
            title=title,
            call_id=body.call_id,
            profile_id=body.profile_id,
        )
        if deadline:
            ws.deadline = deadline
        session.commit()
        new_id = str(ws.id)
    return WorkspaceCreatedOut(id=new_id)


@router.delete("/workspaces/{workspace_id}", status_code=204)
def soft_delete_workspace_api(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> Response:
    """Owner-only soft-delete. Cascades active jobs + queued turns and retires the
    workspace in-process. Re-delete of an already-soft-deleted workspace → 404
    (require_owner filters soft_deleted_at) — not idempotent, by design.

    Destroys nothing on disk (Slice A): the workspace is frozen, not erased, so
    its contents remain for a later admin purge. Users are locked out by the
    liveness gates and the room eviction, not by byte removal.

    The ORDER is the contract: DB commit → retire → 204."""
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        # DB half commits internally (mutex acquire + soft_deleted_at + job/turn
        # cancellation) — do not add a second commit here.
        try:
            generation = soft_delete_workspace_db(session, workspace_id=ws.id)
        except CommitMutexTimeout as e:
            # Deletion participates in the commit mutex (so live writers can
            # linearize against it). A wedged writer means nothing was committed —
            # the workspace is untouched and the client may retry.
            session.rollback()
            raise HTTPException(
                status_code=429,
                detail=BUSY_ERROR_MESSAGE,
                headers={"Retry-After": "2"},
            ) from e
    # DB soft-delete is durable. Retire in-process (bounded ~5s, never raises):
    # tombstone the turns registry so no new subprocess installs and tear down any
    # live one, and tear down both collab rooms (close editors 1008, no
    # checkpoint). This SUBSUMES the old signal_from_request permission
    # reconciliation — the rooms are gone. THEN 204.
    #
    # Slice A: deletion FREEZES a workspace and destroys nothing on disk. It used
    # to scrub attachment and reference bytes here; that removal is what made a
    # later restore impossible, because the rows survived while their files did
    # not. Byte removal moves to the admin purge (Slice E). Users are already
    # locked out by the liveness gates and the room eviction above — keeping the
    # bytes changes what can be recovered later, not who can reach them now.
    signal_workspace_retirement(request, workspace_id=workspace_id, generation=generation)
    return Response(status_code=204)
