"""Workspace git ↔ DB reconciliation.

Spec line 248–268. The filesystem (git) and Postgres are not in the
same transaction, so commits can land without their matching
artifact_versions row. Reconciliation backfills.

Triggers:
  - on workspace open (controller.launch invokes this — Task 8).
  - on-demand from /admin (B6/B8 will wire that).

Forward-only and never destructive: git commits are never undone.
Orphan-commit case → backfill an artifact_versions row with the
commit's actual timestamp (Codex C3: `%ct` from git log).
Orphan-row case (sha in DB, no git commit) → impossible under the
write order (git first); if it appears, mark created_by='unrecoverable'
and alert. Out of scope here.
"""

from __future__ import annotations

import logging
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from call_manager.db.models import ArtifactVersion, WorkspaceEvent

logger = logging.getLogger(__name__)


def reconcile_workspace_git_drift(
    session_factory: sessionmaker,
    workspace_id: uuid.UUID,
    repo_dir: Path,
) -> int:
    """Backfill artifact_versions rows for git commits that don't have
    one. Returns the count of rows inserted.

    Codex C3: preserves commit timestamps (%ct = committer Unix time)
    so backfilled rows' created_at matches when the commit actually
    landed — not when reconciliation ran. Spec line 258 mandates this.
    """
    if not (repo_dir / ".git").is_dir():
        return 0
    try:
        proc = subprocess.run(
            ["git", "log", "--pretty=format:%H%x00%ct%x00%s"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        logger.warning(
            "reconcile_workspace_git_drift: git log failed for %s: %s",
            workspace_id,
            e.stderr,
        )
        return 0

    git_lines = [line for line in proc.stdout.strip().split("\n") if line]
    if not git_lines:
        return 0
    # Each line: <sha>\x00<committer_unix_ts>\x00<subject>
    git_commits: list[tuple[str, int, str]] = []
    for line in git_lines:
        sha, ts_str, message = line.split("\x00", 2)
        git_commits.append((sha, int(ts_str), message))

    with session_factory() as s:
        existing_shas = set(
            s.scalars(
                select(ArtifactVersion.commit_sha).where(
                    ArtifactVersion.workspace_id == workspace_id
                )
            ).all()
        )
        backfilled = 0
        for sha, ts, message in git_commits:
            if sha in existing_shas:
                continue
            s.add(
                ArtifactVersion(
                    workspace_id=workspace_id,
                    commit_sha=sha,
                    triggering_turn_id=None,
                    created_by="reconciled",
                    message=message,
                    created_at=datetime.fromtimestamp(ts, tz=UTC),
                )
            )
            backfilled += 1
        if backfilled > 0:
            s.add(
                WorkspaceEvent(
                    workspace_id=workspace_id,
                    kind="git_drift_reconciled",
                    payload={"backfilled_count": backfilled},
                )
            )
        s.commit()
    return backfilled
