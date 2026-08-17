"""Admin retention surface: read-only access to soft-deleted workspaces.

Retention Slice B (docs/superpowers/specs/2026-08-16-admin-retention-surface-design.md).
This module REMOVES NOTHING. Purge is deferred to Slice E and requires the
filesystem-activity barrier of Slice D first.

Both routes are deliberately separate from the owner-facing workspace routes.
Those gate on a LIVE row; widening them to serve deleted workspaces would put
the deleted case on the same code path as the live case, where a later edit
could leak one into the other.
"""

import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select

from call_manager.auth.dependencies import require_admin
from call_manager.db.models import User, Workspace, WorkspaceEvent
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    blob_sha,
    current_head,
    list_commits,
    read_blob,
)
from call_manager.proposal_assist.drafting.locking import (
    BUSY_ERROR_MESSAGE,
    CommitMutexTimeout,
    acquire_commit_mutex,
)
from call_manager.proposal_assist.paths import workspace_repo_dir

# NO prefix: web/api/__init__.py's router adds /api and the shared error-envelope
# responses. Paths below are written relative to that.
router = APIRouter(tags=["admin-retention"])

_LIST_LIMIT_DEFAULT = 50
_LIST_LIMIT_MAX = 200


class DeletedWorkspaceRow(BaseModel):
    id: uuid.UUID
    title: str
    owner_email: str | None
    soft_deleted_at: datetime


class DeletedWorkspaceListOut(BaseModel):
    items: list[DeletedWorkspaceRow]
    total: int


# Path is RELATIVE. web/api/__init__.py's router supplies the /api prefix along
# with the shared error-envelope responses; an absolute path mounted straight on
# the app would skip those and advertise the wrong 422 shape to the TS codegen.
@router.get("/admin/workspaces/deleted", response_model=DeletedWorkspaceListOut)
def list_deleted_workspaces(
    request: Request,
    limit: int = Query(_LIST_LIMIT_DEFAULT, ge=1, le=_LIST_LIMIT_MAX),
    offset: int = Query(0, ge=0),
    admin: User = Depends(require_admin),  # noqa: B008
) -> DeletedWorkspaceListOut:
    """List soft-deleted workspaces, newest deletion first.

    Paginated because this grows without bound on a long-lived instance: every
    workspace ever deleted stays here, since nothing removes them.
    """
    sf = request.app.state.session_factory
    with sf() as session:
        total = session.execute(
            select(func.count())
            .select_from(Workspace)
            .where(Workspace.soft_deleted_at.is_not(None))
        ).scalar_one()
        rows = session.execute(
            select(Workspace, User.email)
            .outerjoin(User, User.id == Workspace.owner_id)
            .where(Workspace.soft_deleted_at.is_not(None))
            # id as tiebreak: two workspaces deleted in the same instant would
            # otherwise have an undefined relative order, and limit/offset paging
            # over an unstable sort can repeat or skip a row across pages.
            .order_by(Workspace.soft_deleted_at.desc(), Workspace.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        items = [
            DeletedWorkspaceRow(
                id=ws.id,
                title=ws.title,
                owner_email=email,
                soft_deleted_at=ws.soft_deleted_at,
            )
            for ws, email in rows
        ]
    return DeletedWorkspaceListOut(items=items, total=total)


_HISTORY_LIMIT = 200


class CommitOut(BaseModel):
    sha: str
    authored_at: datetime
    subject: str


class DeletedWorkspaceDetailOut(BaseModel):
    id: uuid.UUID
    title: str
    owner_email: str | None
    soft_deleted_at: datetime
    # True when the repository yielded something: a readable commit history, or
    # a readable proposal.md at HEAD. NOT a filesystem predicate — a directory
    # that exists with an unborn HEAD reports False, because nothing in it can
    # be shown.
    repository_available: bool
    # Whether `commits` is an ANSWER or an ABSENCE. Without it, a history that
    # could not be read is indistinguishable from a repository with no commits:
    # both are `commits: []`. Those are not the same fact, and the difference is
    # visible — a readable draft at HEAD proves at least one commit exists, so
    # `repository_available: true` beside an empty history is a contradiction
    # rather than a state.
    history_available: bool
    proposal_markdown: str | None
    commits: list[CommitOut]
    history_truncated: bool
    # The server's cap, sent so the UI can state it instead of hardcoding a
    # number that silently becomes a lie the moment _HISTORY_LIMIT changes.
    # Always present, not only when truncated: a client that renders the cap
    # should not have to know which responses carry it.
    history_limit: int


def _record_inspection(
    session, *, workspace_id: uuid.UUID, admin_id: int, served: bool,
    commit_count: int, truncated: bool,
) -> None:
    """Write and COMMIT the inspection audit event.

    Separate function so the ordering is visible at the call site and so a test
    can make it fail: nothing that reveals proposal content may be returned
    before this has committed.
    """
    session.add(
        WorkspaceEvent(
            workspace_id=workspace_id,
            kind="admin_deleted_workspace_inspected",
            actor_id=admin_id,
            # Spec 3.3 fixes these five keys. workspace_id and admin_user_id are
            # redundant with the columns ON PURPOSE: the payload is what an
            # exported or forwarded audit trail carries, and a row that says
            # only "content_served: true" names neither the draft read nor who
            # read it. str() on the UUID because the column is JSONB.
            payload={
                "workspace_id": str(workspace_id),
                "admin_user_id": admin_id,
                "content_served": served,
                "commit_count": commit_count,
                "history_truncated": truncated,
            },
        )
    )
    session.commit()


@router.get(
    "/admin/workspaces/deleted/{workspace_id}",
    response_model=DeletedWorkspaceDetailOut,
)
def inspect_deleted_workspace(
    workspace_id: uuid.UUID,
    request: Request,
    response: Response,
    admin: User = Depends(require_admin),  # noqa: B008
) -> DeletedWorkspaceDetailOut:
    """Read a soft-deleted workspace's surviving proposal text and history.

    Liveness is checked TWICE, deliberately:

    1. Pre-lock: a missing or live workspace 404s immediately, WITHOUT taking
       its commit mutex. Taking the mutex first would contend with that
       workspace's real writers (turn commits, collab checkpoints,
       editing/save.py) for a workspace this route is about to refuse anyway,
       and — if the mutex happened to be held — would pin a FastAPI worker for
       the full `acquire_commit_mutex` default of `timeout_seconds=10`
       (drafting/locking.py:206) just to answer a 404.
    2. Post-lock, with a fresh scalar SELECT (never the identity-mapped row
       from step 1 — same pattern as `acquire_commit_mutex_live`,
       drafting/locking.py:288-292): the workspace can be restored in the
       window between step 1 and winning the mutex, and the git read below
       must never proceed against a workspace that is live again.

    Holds the per-workspace commit mutex for the git read. That is the plain
    `acquire_commit_mutex`, NOT `acquire_commit_mutex_live` — the live variant
    re-reads liveness and raises WorkspaceDeleted, which would refuse every
    workspace this endpoint exists to serve.

    Refuses with 403 when RETENTION_INSPECTION_ENABLED is false (the default).

    The audit event is committed BEFORE any content is returned.
    """
    # Capability gate, FIRST: before the workspace lookup, before the commit
    # mutex, before the audit row. A refused request must cost nothing and leave
    # nothing — taking the mutex would contend with a live workspace's real
    # writers on behalf of a request that is going to be refused anyway.
    #
    # 403, not 404. The workspace is genuinely there and the admin can already
    # see it in the list beside this call; answering "not found" would
    # contradict that list and send an operator hunting for a missing row
    # instead of an unset env var. Nothing is concealed by lying here — the
    # list endpoint is deliberately ungated.
    #
    # No audit row on this path, matching the existing 404s: the audit trail
    # records CONTENT SERVED, and nothing was. A record of refused attempts is a
    # different feature (access-attempt logging) and is not built here.
    if not request.app.state.settings.retention_inspection_enabled:
        raise HTTPException(
            status_code=403,
            detail=(
                "Reading deleted workspace content is disabled on this "
                "deployment (RETENTION_INSPECTION_ENABLED)."
            ),
        )
    # Every read of this endpoint writes an audit row, and that is only true of
    # reads that actually reach this handler. A browser or intermediary cache
    # replaying the response would show an admin someone's deleted draft with no
    # trace — the same hole the client-side query cache had, one layer down and
    # outside our control. no-store forbids writing it to any cache at all;
    # private additionally forbids shared caches from holding it.
    response.headers["Cache-Control"] = "private, no-store"

    sf = request.app.state.session_factory
    workspaces_dir = Path(request.app.state.settings.workspaces_dir)

    with sf() as session:
        row = session.execute(
            select(Workspace, User.email)
            .outerjoin(User, User.id == Workspace.owner_id)
            .where(Workspace.id == workspace_id)
        ).one_or_none()
        if row is None or row[0].soft_deleted_at is None:
            raise HTTPException(status_code=404, detail="not found")
        ws, owner_email = row

        # NOT a context manager. acquire_commit_mutex returns None
        # (drafting/locking.py:205); the advisory lock is held for the rest of
        # the transaction and released by COMMIT or ROLLBACK. `with acquire_...`
        # raises AttributeError on every request.
        #
        # The plain variant, not acquire_commit_mutex_live: the _live variant
        # re-reads liveness and raises WorkspaceDeleted, which would refuse every
        # workspace this endpoint exists to serve.
        try:
            acquire_commit_mutex(session, workspace_id)
        except CommitMutexTimeout as e:
            session.rollback()
            # Same mapping as every other mutex caller (web/api/workspaces.py:444).
            raise HTTPException(
                status_code=429,
                detail=BUSY_ERROR_MESSAGE,
                headers={"Retry-After": "2"},
            ) from e

        # Fresh re-read under the lock. `ws.soft_deleted_at` above was read
        # before the mutex and can be stale: a restore (Slice C) could have
        # landed in the window between that read and winning the lock. A
        # scalar SELECT always round-trips to Postgres; session.get() could
        # instead hand back the identity-mapped instance loaded in step 1.
        still_deleted = session.execute(
            select(Workspace.soft_deleted_at).where(Workspace.id == workspace_id)
        ).scalar_one_or_none()
        if still_deleted is None:
            raise HTTPException(status_code=404, detail="not found")

        repo = workspace_repo_dir(workspaces_dir, workspace_id)

        # Two SEPARATE try blocks. A workspace can have a real, readable commit
        # history but no proposal.md at HEAD (e.g. a crash between `git init`
        # and the first commit — phases/setup.py's own _init_repo docstring
        # names this as a recovery scenario). One shared try would let a
        # failure reading the draft blob discard commit history git can still
        # produce, and the audit would then record a false commit_count too.
        try:
            commits, truncated = list_commits(repo, limit=_HISTORY_LIMIT)
            history_available = True
        except (OSError, GitCommitError):
            # Legitimate: the workspace can be deleted before setup ever
            # created a repository, or the repository exists but has no
            # commits yet. Report it as a state, not a crash.
            commits, truncated, history_available = [], False, False

        try:
            # At HEAD, not the working tree. Spec 3.2 says "proposal markdown at
            # HEAD", and a workspace can be deleted with uncommitted bytes on
            # disk from a collab session that never checkpointed. read_text()
            # would serve content the commit list beside it does not explain.
            head = current_head(repo)
            markdown = read_blob(repo, blob_sha(repo, head, PROPOSAL_FILENAME))
        except (OSError, GitCommitError):
            # Legitimate: no repository, no commits yet, or a commit exists
            # that never touched proposal.md.
            markdown = None

        # Splitting the try blocks made one new combination reachable that the
        # shared block could not produce: git log fails (a timeout, or the
        # malformed-output branch) while rev-parse and cat-file still work. That
        # would answer "there is no repository" while handing the admin that
        # repository's draft. Serving content is itself proof the repository is
        # there, so say so — and `history_available` above then carries why the
        # commit list is empty, which `commits: []` alone cannot: a readable
        # blob at HEAD PROVES at least one commit exists, so an empty history
        # next to a served draft is never "this workspace has no commits".
        available = history_available or markdown is not None

        # "content_served" means the admin actually saw a draft — an empty
        # proposal.md at HEAD is readable but is not a draft.
        served = markdown is not None and markdown != ""

        _record_inspection(
            session,
            workspace_id=workspace_id,
            admin_id=admin.id,
            served=served,
            commit_count=len(commits),
            truncated=truncated,
        )

        return DeletedWorkspaceDetailOut(
            id=ws.id,
            title=ws.title,
            owner_email=owner_email,
            soft_deleted_at=ws.soft_deleted_at,
            repository_available=available,
            history_available=history_available,
            proposal_markdown=markdown,
            commits=[
                CommitOut(sha=c.sha, authored_at=c.authored_at, subject=c.subject)
                for c in commits
            ],
            history_limit=_HISTORY_LIMIT,
            history_truncated=truncated,
        )
