"""User section save: the single write path for owner edits.

Order inside the per-workspace commit mutex (spec §1):
stale-form check → structure guard → no-change
short-circuit → git commit → ArtifactVersion('user') →
WorkspaceEvent('user_edit') → Tier-1 deterministic validation → Tier-2
job enqueue. The last two steps run for the DRAFT artifact only; the spec
(proposal-spec.md) is citation-free and skips them. The mutex is
transaction-scoped; the caller's session.commit()
releases it. A DB failure AFTER the git commit is recovered by
reconcile_workspace_git_drift + the analysis reconcile tick (spec §6).
CommitMutexTimeout propagates to the caller (web maps it to 503).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from call_manager.db.models import ArtifactVersion, TurnJob, Workspace, WorkspaceEvent
from call_manager.proposal_assist.analysis.queue import enqueue_section_job
from call_manager.proposal_assist.artifacts import (
    PROPOSAL_FILENAME,  # noqa: F401  (re-export)
    Artifact,
    filename_for,
)
from call_manager.proposal_assist.citations.audit import validate_citations_deterministic
from call_manager.proposal_assist.citations.gitio import current_head, write_and_commit_files
from call_manager.proposal_assist.citations.ledger import empty_ledger, load_ledger
from call_manager.proposal_assist.citations.markers import find_bare_markers
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex
from call_manager.proposal_assist.editing.sectionizer import is_h2_heading_line, split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir

logger = logging.getLogger(__name__)

CITATIONS_FILENAME = "citations.json"

# User-facing message for the turn-in-flight 409 (shared with insert.py and
# sections.py so all three raise the identical string).
TURN_IN_FLIGHT_MESSAGE = "assistant is working — try again when the turn finishes"

# Statuses meaning an agent turn owns (or is about to own) the repo.
_TURN_IN_FLIGHT_STATUSES = ("queued", "running")


class StaleSectionError(RuntimeError):
    """HEAD moved since the edit form was rendered AND the edited section's
    bytes changed underneath the user — saving would silently clobber."""


class SectionStructureError(RuntimeError):
    """The edit removed or invalidated the section's structural boundary
    (its ## heading), which would silently merge it into a neighbor.
    Removing/merging/reordering sections is chat-only in this slice."""


class SpecLockedError(RuntimeError):
    """The spec is locked (workspace.spec_locked_at set); a spec write is
    refused until it is unlocked. Web routes map this to 409."""


@dataclass
class SaveResult:
    commit_sha: str
    head: str
    section_sha: str
    issues: list[dict] = field(default_factory=list)
    job_enqueued: bool = False
    changed: bool = True


def _turn_in_flight(session: Session, workspace_id) -> bool:
    """True if an agent turn is queued or running for this workspace.

    save_section no longer consults this (its turn-freeze hard guard was
    removed). Current callers: analysis/insert.py (hard guard — a citation
    insert refuses with InsertConflict while a turn owns the repo) and the
    section-edit web route (soft pre-check so the edit form isn't even offered
    mid-turn). Save-time concurrency now rests on the commit mutex + staleness.
    """
    return (
        session.query(TurnJob.id)
        .filter(
            TurnJob.workspace_id == workspace_id,
            TurnJob.status.in_(_TURN_IN_FLIGHT_STATUSES),
        )
        .first()
        is not None
    )


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
        logger.error("corrupt citations.json in workspace %s: %s", workspace_id, exc)
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
                "evidence_id": None,
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
    artifact: Artifact = Artifact.DRAFT,
) -> SaveResult:
    """Save one user-edited section of the workspace's draft or spec artifact.

    ``artifact`` selects the file (``proposal.md`` for the draft,
    ``proposal-spec.md`` for the spec). The mutex, staleness check, structure
    guard, git commit, and provenance rows are shared; the Tier-1 citation
    validation and Tier-2 claim-analysis enqueue run for the DRAFT only (the
    spec is citation-free), so a spec save always returns ``issues == []`` and
    ``job_enqueued is False``.

    Caller owns the transaction: rows are added but NOT committed here, and
    the commit mutex is released by the caller's COMMIT/ROLLBACK. Raises
    CommitMutexTimeout, StaleSectionError, SectionStructureError
    (pre-commit — no git side-effect), IndexError (section_index out of
    range), GitCommitError, SpecLockedError (pre-commit — the spec
    artifact is locked).

    SectionStructureError is raised when the first line of ``new_text`` is
    not an H2 heading for a non-preamble section, or IS an H2 heading for
    the preamble (section_index == 0). Adding EXTRA ``##`` headings anywhere
    inside a section is allowed — save_section splits the result and enqueues
    the first resulting chunk; the reconcile tick covers any remainder.
    """
    # 1. Serialize against every other git mutation of this workspace.
    acquire_commit_mutex(session, workspace.id)

    artifact = Artifact(artifact)  # normalize a stringly-typed caller

    # Spec-lock guard: a lock may have committed while we waited on the mutex,
    # so re-read the row under the lock before deciding. Gated on SPEC so the
    # draft path is unchanged (no extra query, no behavior change).
    if artifact is Artifact.SPEC:
        session.refresh(workspace, ["spec_locked_at"])
        if workspace.spec_locked_at is not None:
            raise SpecLockedError(str(workspace.id))

    # NOTE: save_section's workspace-wide turn-freeze hard guard was removed. In
    # SP2a the drafting agent's direct-commit tools are STILL LIVE (mechanism B's
    # allowlist swap lands in a later plan), so anti-clobber does NOT yet rest on
    # "the agent only proposes". It rests on this commit mutex serializing every
    # git writer + each writer re-checking HEAD/base_section_sha before it writes
    # (here, and in tools/proposal.py). Do not drop a writer's HEAD/base-sha check
    # on the assumption that (B) prevents concurrent agent commits.

    # 3. Read the live document under the mutex.
    repo_dir = workspace_repo_dir(workspaces_dir, workspace.id)
    head = current_head(repo_dir)
    fname = filename_for(artifact)
    doc = (repo_dir / fname).read_text()
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

    # 5b. Structure guard: enforce the section boundary before any git I/O.
    #     For non-preamble sections the first line must be an H2 heading
    #     (rename allowed; deleting it would silently merge into a neighbor).
    #     For the preamble (index 0) the first line must NOT be an H2 heading
    #     (a preamble beginning with "## " ceases to be the preamble).
    #     Empty preamble text is allowed.
    first_line = new_text.split("\n", 1)[0] if new_text else ""
    if section_index > 0:
        if not is_h2_heading_line(first_line):
            raise SectionStructureError(
                f"section {section_index} edit must start with an H2 heading (## …); "
                "got: " + repr(first_line)
            )
    else:
        if new_text and is_h2_heading_line(first_line):
            raise SectionStructureError(
                "preamble (section 0) edit must not start with an H2 heading; "
                "got: " + repr(first_line)
            )

    # 6. No-change short-circuit: write_and_commit_files would no-op anyway;
    #    skip provenance rows + enqueue but still report Tier-1 issues.
    if new_text == live.text:
        issues = (
            _tier1_issues(doc=doc, repo_dir=repo_dir, session=session, workspace_id=workspace.id)
            if artifact is Artifact.DRAFT
            else []
        )
        return SaveResult(
            commit_sha=head,
            head=head,
            section_sha=live.sha,
            issues=issues,
            job_enqueued=False,
            changed=False,
        )

    # 7. Reassemble and commit (join invariant: section texts concatenate
    #    byte-identically to the document).
    new_doc = "".join(new_text if s.index == section_index else s.text for s in sections)
    heading = live.heading or "preamble"
    commit_sha = write_and_commit_files(
        repo_dir,
        {fname: new_doc},
        f"user {artifact} edit: {heading} (user:{user_id})",
    )

    # 8. Provenance rows (committed by the caller, atomically with the mutex
    #    release; a DB failure from here on is repaired by reconciliation).
    session.add(
        ArtifactVersion(
            workspace_id=workspace.id,
            commit_sha=commit_sha,
            created_by="user",
            message=f"user {artifact} edit: {heading}",
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
                "byte_delta": len(new_doc.encode()) - len(doc.encode()),
                "artifact": str(artifact),
            },
        )
    )

    saved = split_sections(new_doc)[section_index]
    if artifact is Artifact.DRAFT:
        # 9. Tier-1 deterministic validation of the NEW document.
        issues = _tier1_issues(
            doc=new_doc, repo_dir=repo_dir, session=session, workspace_id=workspace.id
        )
        # 10. Tier-2: enqueue claim analysis for the saved section's new content.
        job_enqueued = enqueue_section_job(
            session,
            workspace_id=workspace.id,
            section_index=saved.index,
            section_heading=saved.heading,
            section_sha=saved.sha,
            commit_sha=commit_sha,
        )
    else:
        # Spec: no citation validation, no claim analysis (spec §3).
        issues = []
        job_enqueued = False
    return SaveResult(
        commit_sha=commit_sha,
        head=commit_sha,
        section_sha=saved.sha,
        issues=issues,
        job_enqueued=job_enqueued,
        changed=True,
    )
