"""User section save: the single write path for owner edits.

Order inside the per-workspace commit mutex (spec §1):
turn-lock check → stale-form check → no-change short-circuit → git commit →
ArtifactVersion('user') → WorkspaceEvent('user_edit') → Tier-1 deterministic
validation → Tier-2 job enqueue. The mutex is transaction-scoped; the
caller's session.commit() releases it. A DB failure AFTER the git commit is
recovered by reconcile_workspace_git_drift + the analysis reconcile tick
(spec §6). CommitMutexTimeout propagates to the caller (web maps it to 503).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from call_manager.db.models import ArtifactVersion, TurnJob, Workspace, WorkspaceEvent
from call_manager.proposal_assist.analysis.queue import enqueue_section_job
from call_manager.proposal_assist.citations.audit import validate_citations_deterministic
from call_manager.proposal_assist.citations.gitio import current_head, write_and_commit_files
from call_manager.proposal_assist.citations.ledger import empty_ledger, load_ledger
from call_manager.proposal_assist.citations.markers import find_bare_markers
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir

PROPOSAL_FILENAME = "proposal.md"
CITATIONS_FILENAME = "citations.json"

# Statuses meaning an agent turn owns (or is about to own) the repo.
_TURN_IN_FLIGHT_STATUSES = ("queued", "running")


class TurnInFlightError(RuntimeError):
    """An agent turn is queued or running for this workspace — a user edit
    would race the agent's git commits. Save again when the turn finishes."""


class StaleSectionError(RuntimeError):
    """HEAD moved since the edit form was rendered AND the edited section's
    bytes changed underneath the user — saving would silently clobber."""


@dataclass
class SaveResult:
    commit_sha: str
    issues: list[dict] = field(default_factory=list)
    job_enqueued: bool = False
    changed: bool = True


def _tier1_issues(*, doc: str, repo_dir: Path, session: Session, workspace_id) -> list[dict]:
    """Deterministic (Tier-1) validation of *doc*: citation audit against the
    repo's citations.json (absent file → empty ledger, matching the other
    load_ledger call sites) plus a bare-marker scan. Degrades a corrupt ledger
    to an issue instead of raising — Tier-1 runs after the git commit, and an
    exception here would orphan that commit."""
    issues: list[dict] = []
    citations_path = repo_dir / CITATIONS_FILENAME
    try:
        ledger = load_ledger(citations_path.read_text() if citations_path.exists() else None)
    except ValueError as exc:
        ledger = empty_ledger()
        issues.append(
            {
                "type": "ledger_invalid",
                "severity": "error",
                "citekey": None,
                "evidence_id": None,
                "location": CITATIONS_FILENAME,
                "detail": f"citations.json could not be read: {exc}",
            }
        )
    issues.extend(
        validate_citations_deterministic(
            proposal_text=doc,
            ledger=ledger,
            session=session,
            workspace_id=workspace_id,
        )
    )
    for bm in find_bare_markers(doc):
        issues.append(
            {
                "type": "bare_marker",
                "severity": "error",
                "citekey": bm["citekey"],
                "location": f"char {bm['start']}",
                "detail": (
                    "hand-typed citation has no evidence link — "
                    "use the Insert citation button instead"
                ),
            }
        )
    return issues


def save_section(
    *,
    session: Session,
    workspace: Workspace,
    user_id: int,
    section_index: int,
    new_text: str,
    base_commit_sha: str,
    base_section_sha: str,
    workspaces_dir: Path | str,
) -> SaveResult:
    """Save one user-edited section of the workspace's proposal.md.

    Caller owns the transaction: rows are added but NOT committed here, and
    the commit mutex is released by the caller's COMMIT/ROLLBACK. Raises
    CommitMutexTimeout, TurnInFlightError, StaleSectionError, IndexError
    (section_index out of range), GitCommitError.
    """
    # 1. Serialize against every other git mutation of this workspace.
    acquire_commit_mutex(session, workspace.id)

    # 2. Turn-lock: an agent turn in flight owns the repo.
    in_flight = (
        session.query(TurnJob.id)
        .filter(
            TurnJob.workspace_id == workspace.id,
            TurnJob.status.in_(_TURN_IN_FLIGHT_STATUSES),
        )
        .first()
    )
    if in_flight is not None:
        raise TurnInFlightError(
            f"an agent turn is in flight for workspace {workspace.id}; retry after it finishes"
        )

    # 3. Read the live document under the mutex.
    repo_dir = workspace_repo_dir(workspaces_dir, workspace.id)
    head = current_head(repo_dir)
    doc = (repo_dir / PROPOSAL_FILENAME).read_text()
    sections = split_sections(doc)
    if not 0 <= section_index < len(sections):
        raise IndexError(
            f"section_index {section_index} out of range (document has {len(sections)} sections)"
        )
    live = sections[section_index]

    # 4. Stale-form check: HEAD moving is fine as long as THIS section's bytes
    #    are still exactly what the form was rendered from.
    if head != base_commit_sha and live.sha != base_section_sha:
        raise StaleSectionError(
            f"section {section_index} changed since the edit form was loaded; reload and re-apply"
        )

    # 5. Newline glue: a non-final section must end with a newline or the next
    #    "## " heading would fuse onto the user's last line.
    if section_index < len(sections) - 1 and not new_text.endswith("\n"):
        new_text += "\n"

    # 6. No-change short-circuit: write_and_commit_files would no-op anyway;
    #    skip provenance rows + enqueue but still report Tier-1 issues.
    if new_text == live.text:
        issues = _tier1_issues(
            doc=doc, repo_dir=repo_dir, session=session, workspace_id=workspace.id
        )
        return SaveResult(commit_sha=head, issues=issues, job_enqueued=False, changed=False)

    # 7. Reassemble and commit (join invariant: section texts concatenate
    #    byte-identically to the document).
    new_doc = "".join(new_text if s.index == section_index else s.text for s in sections)
    heading = live.heading or "preamble"
    commit_sha = write_and_commit_files(
        repo_dir,
        {PROPOSAL_FILENAME: new_doc},
        f"user edit: {heading} (user:{user_id})",
    )

    # 8. Provenance rows (committed by the caller, atomically with the mutex
    #    release; a DB failure from here on is repaired by reconciliation).
    session.add(
        ArtifactVersion(
            workspace_id=workspace.id,
            commit_sha=commit_sha,
            created_by="user",
            message=f"user edit: {heading}",
        )
    )
    session.add(
        WorkspaceEvent(
            workspace_id=workspace.id,
            kind="user_edit",
            actor_id=user_id,
            payload={
                "section_index": section_index,
                "heading": heading,
                "commit_sha": commit_sha,
                "byte_delta": len(new_doc) - len(doc),
            },
        )
    )

    # 9. Tier-1 deterministic validation of the NEW document.
    issues = _tier1_issues(
        doc=new_doc, repo_dir=repo_dir, session=session, workspace_id=workspace.id
    )

    # 10. Tier-2: enqueue claim analysis for the saved section's new content.
    saved = split_sections(new_doc)[section_index]
    job_enqueued = enqueue_section_job(
        session,
        workspace_id=workspace.id,
        section_index=saved.index,
        section_heading=saved.heading,
        section_sha=saved.sha,
        commit_sha=commit_sha,
    )
    return SaveResult(commit_sha=commit_sha, issues=issues, job_enqueued=job_enqueued, changed=True)
