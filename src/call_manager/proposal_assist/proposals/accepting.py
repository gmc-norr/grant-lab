"""Accept a pending proposed_change by REPLAYING the existing mutation
function as the named approver, under the commit mutex (via save_section).
The caller owns the transaction."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import ProposedChange, User, Workspace
from call_manager.proposal_assist.artifacts import Artifact
from call_manager.proposal_assist.citations.core import (
    _GIT_COMMIT_FAILED_ERROR,
    _insert_citation_locked,
)
from call_manager.proposal_assist.citations.gitio import GitCommitError
from call_manager.proposal_assist.drafting.locking import BUSY_ERROR_MESSAGE
from call_manager.proposal_assist.editing.save import (
    StaleSectionError,
    save_section,
)
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.ports.stamp import FenceStamp
from call_manager.proposal_assist.tools.registry import ToolContext


class ProposedChangeNotFound(Exception):
    pass


class ProposedChangeNotPending(Exception):
    def __init__(self, status: str) -> None:
        super().__init__(f"proposed_change is {status!r}, not pending")
        self.status = status


def _load_scoped(
    session: Session, proposed_change_id: uuid.UUID, workspace_id: uuid.UUID
) -> ProposedChange:
    """Fetch by BOTH id AND workspace_id, so a member of workspace A can never
    act on a proposed_change id belonging to workspace B (finding #2).

    Locks the row ``FOR UPDATE`` (held until the caller commits/rolls back) so
    the pending->terminal transition is serialized: a second concurrent resolve
    of the same row (double-click, client retry, two members) blocks here until
    the first commits, then re-reads the now-terminal status and is rejected by
    the ``status != "pending"`` gate — instead of last-writer-wins corrupting the
    audit row (e.g. a row reading "rejected"/"stale" while its content is live).
    """
    pc = session.scalar(
        select(ProposedChange)
        .where(
            ProposedChange.id == proposed_change_id,
            ProposedChange.workspace_id == workspace_id,
        )
        .with_for_update()
    )
    if pc is None:
        raise ProposedChangeNotFound(str(proposed_change_id))
    return pc


def accept_proposed_change(
    *,
    session: Session,
    proposed_change_id: uuid.UUID,
    workspace_id: uuid.UUID,
    approver: User,
    workspaces_dir: Path | str,
    stamp: FenceStamp,
) -> ProposedChange:
    pc = _load_scoped(session, proposed_change_id, workspace_id)
    if pc.status != "pending":
        raise ProposedChangeNotPending(pc.status)

    if pc.target_artifact == "spec":
        if pc.kind != "prose_edit":
            # The spec is citation-free; only prose proposals target it. A
            # DB-permitted-but-unsupported kind is a clean NotImplementedError
            # (the endpoint maps it to 400), never a silent 500.
            raise NotImplementedError(f"spec accept of kind {pc.kind!r} is not supported")
        # ws cannot be None: workspace_id is a NOT NULL FK and _load_scoped just
        # proved a row with this workspace_id exists.
        ws = session.get(Workspace, pc.workspace_id)
        try:
            result = save_section(
                session=session,
                workspace=ws,
                stamp=stamp,
                user_id=approver.id,
                section_index=pc.section_index,
                new_text=pc.payload["new_text"],
                base_commit_sha=pc.base_commit_sha,
                base_section_sha=pc.base_section_sha,
                workspaces_dir=workspaces_dir,
                artifact=Artifact.SPEC,
            )
        except StaleSectionError:
            # A human spec edit landed between the proposer's read and this accept.
            pc.status = "stale"
            pc.resolved_by = approver.id
            pc.resolved_at = datetime.now(UTC)
            session.flush()
            return pc
        # SpecLockedError is NOT caught here: it propagates to the endpoint, which
        # maps it to 409 and leaves the row pending (retryable after unlock).
        pc.status = "accepted"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        pc.resulting_commit_sha = result.commit_sha
        session.flush()
        return pc

    if pc.kind == "prose_edit":
        # ws cannot be None here: workspace_id is a NOT NULL FK (ON DELETE CASCADE)
        # and _load_scoped just proved a row with this workspace_id exists.
        ws = session.get(Workspace, pc.workspace_id)
        try:
            result = save_section(
                session=session,
                workspace=ws,
                stamp=stamp,
                user_id=approver.id,
                section_index=pc.section_index,
                new_text=pc.payload["new_text"],
                base_commit_sha=pc.base_commit_sha,
                base_section_sha=pc.base_section_sha,
                workspaces_dir=workspaces_dir,
            )
        except StaleSectionError:
            # A human edit landed between the proposer's read and this accept.
            # Stamp the resolver so a stale-on-accept-attempt is dated and
            # distinguishable from a passive refresh_staleness sweep.
            pc.status = "stale"
            pc.resolved_by = approver.id
            pc.resolved_at = datetime.now(UTC)
            session.flush()
            return pc

        # A no-op proposal makes save_section short-circuit (changed=False),
        # returning the current HEAD — still legitimately "accepted".
        pc.status = "accepted"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        pc.resulting_commit_sha = result.commit_sha
        session.flush()
        return pc

    if pc.kind == "citation_insert":
        return _accept_citation_insert(
            session=session,
            pc=pc,
            approver=approver,
            workspaces_dir=workspaces_dir,
            stamp=stamp,
        )

    raise NotImplementedError(f"accept of kind {pc.kind!r} is not supported")


class CitationAcceptBusy(Exception):
    """The commit mutex was held during a citation accept replay — retryable.
    Distinct from a terminal verification failure (which flips the row stale)."""


class UnsupportedKernelKind(Exception):
    """A ProposedChange.kind the kernel has no accept path for (citation_remove /
    citation_update — DB-allowed but no producers). Raised at dispatch BEFORE any room
    mutation, so it is non-evicting; the route maps it to 422."""


def _accept_citation_insert(
    *,
    session: Session,
    pc: ProposedChange,
    approver: User,
    workspaces_dir: Path | str,
    stamp: FenceStamp,
) -> ProposedChange:
    """Replay the citation insert as the named approver, under the commit mutex.

    Uses the SYNC, session-owned _insert_citation_locked on the CALLER's session
    (it acquires the commit mutex itself — accept-time authoritative re-verify).
    Passed owns_txn=False so the replay flushes (never commits) its citekey-cache
    write: the caller's FOR UPDATE pc-row lock + advisory commit mutex stay held
    through the status flip, and the endpoint's single session.commit persists the
    git/ledger write AND the status flip in ONE transaction. (Atomicity is NOT
    automatic — owns_txn=False is what makes it single-transaction.)
    Result mapping:
      - success / idempotent -> accepted, resulting_commit_sha = result["commit_sha"]
      - verification failure (anchor/quote moved, chunk re-chunked, tombstoned)
        -> stale, resolver stamped (parallels prose StaleSectionError->stale)
      - busy (_BUSY_ERROR) -> CitationAcceptBusy raised; endpoint maps to 409,
        status UNCHANGED (retryable, not terminal).
    """
    repo_dir = workspace_repo_dir(workspaces_dir, pc.workspace_id)
    ctx = ToolContext(
        workspace_id=pc.workspace_id,
        repo_dir=repo_dir,
        session_factory=None,  # unused on this path
        settings=None,  # type: ignore[arg-type]  — _insert_citation_locked reads off disk
    )
    payload = pc.payload
    # follow-up: the citation git commit message ("cite: add {citekey} [{ev_id}]")
    # carries NO approver — write_and_commit_files takes no author param. We record
    # the approver on pc.resolved_by only; git-commit-author parity with prose's
    # "user edit: ... (user:{id})" is deferred (would need an author param on
    # _insert_citation_locked / write_and_commit_files).
    result = _insert_citation_locked(
        ctx,
        session,
        stamp=stamp,
        ref_uuid=uuid.UUID(str(payload["reference_id"])),
        anchor=payload["anchor"],
        chunk_index=payload["chunk_index"],
        evidence_quote=payload["evidence_quote"],
        stance=payload.get("stance", "supports"),
        confidence=payload.get("confidence"),
        # owns_txn=False: keep the caller's FOR UPDATE pc-row lock + advisory commit
        # mutex held through the status flip so the endpoint's single session.commit
        # persists the git/ledger write AND the status flip in ONE transaction.
        owns_txn=False,
    )

    if "error" in result:
        if result["error"] == BUSY_ERROR_MESSAGE:
            # Retryable: leave status pending; the endpoint surfaces 409.
            raise CitationAcceptBusy(BUSY_ERROR_MESSAGE)
        if result["error"] == _GIT_COMMIT_FAILED_ERROR:
            # Transient infra failure (git/FS), NOT a verification failure. Mirror
            # the prose GitCommitError path: surface a retryable error and leave
            # the row pending (the endpoint rolls back -> 500). Do NOT burn the
            # proposal to terminal 'stale'.
            raise GitCommitError("citation accept replay failed to commit")
        # Terminal verification failure -> stale, stamped (mirrors prose).
        pc.status = "stale"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        session.flush()
        return pc

    pc.status = "accepted"
    pc.resolved_by = approver.id
    pc.resolved_at = datetime.now(UTC)
    # NB dict key result["commit_sha"], unlike prose's result.commit_sha attribute.
    pc.resulting_commit_sha = result["commit_sha"]
    session.flush()
    return pc


def reject_proposed_change(
    *,
    session: Session,
    proposed_change_id: uuid.UUID,
    workspace_id: uuid.UUID,
    approver: User,
) -> ProposedChange:
    pc = _load_scoped(session, proposed_change_id, workspace_id)
    if pc.status != "pending":
        raise ProposedChangeNotPending(pc.status)
    pc.status = "rejected"
    pc.resolved_by = approver.id
    pc.resolved_at = datetime.now(UTC)
    session.flush()
    return pc
