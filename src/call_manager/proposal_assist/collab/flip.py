"""Enable-on-touch flip seam (3b-5d.D4): the ONLY prod caller of enable_collab.

maybe_auto_enable is invoked from the draft/spec read models — the first read
of an artifact (kill-switch off, no pipe tables at HEAD) enables collab for it,
stickily and per-artifact. New workspaces converge on their first cockpit load;
existing ones on their next open. Remove the hold-back once
scripts/collab_table_audit.py reports no affected artifacts."""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from call_manager.proposal_assist.collab import state
from call_manager.proposal_assist.drafting.locking import (
    CommitMutexTimeout,
    acquire_commit_mutex,
)

logger = logging.getLogger(__name__)

# The flip is a read-path side effect and must not stall an interactive GET on a
# wedged writer. It acquires the commit mutex ONCE per artifact lifetime (the
# is_collab_enabled short-circuit fires forever after), so a bounded, modest
# timeout is safe: on contention we defer the flip and the NEXT read retries.
_FLIP_MUTEX_TIMEOUT_SECONDS = 5

# A pipe-table-shaped row: a line with >=2 pipes separating cell-ish content
# (bordered header/body rows, with or without a leading pipe). Does NOT match
# borderless 2-column rows (single interior pipe) — those are caught by
# _DELIMITER_ROW below. Conservative ON PURPOSE: a false positive only DELAYS
# enablement (the artifact stays on the CM6 editor); a false negative flattens
# a table on the first room round-trip.
_TABLE_ROW = re.compile(r"^\s*\|?[^|\n]*\|[^|\n]*\|")

# A GFM delimiter row (`---- | -----`, `:--- | ---:`, `|---|---|`): the whole
# line is dash-cells (>=3 dashes, optional alignment colons) joined by >=1 pipe.
# This is the unambiguous table signature that catches BORDERLESS 2-column
# tables, whose header/body rows have only a single interior pipe (post-flip
# hardening; D4 final-review Minor 3). Prose-safe: real prose lines never
# consist solely of dashes and pipes.
_DELIMITER_ROW = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")


def has_pipe_tables(md: str) -> bool:
    in_fence = False
    for line in md.splitlines():
        stripped = line.lstrip()
        # NOTE: generic fence toggle — the closing fence's character/length is
        # not matched to the opener (CommonMark would). Mismatches err toward
        # treating more lines as fenced, i.e. false NEGATIVES are possible only
        # for a table following an unclosed pseudo-fence; accepted (D4
        # final-review Minor 6) — indented-code pseudo-fences are exotic in
        # proposal prose.
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _TABLE_ROW.match(line) or _DELIMITER_ROW.match(line):
            return True
    return False


def _log_held_back(workspace_id: uuid.UUID, artifact: str) -> None:
    logger.info(
        "collab.flip_held_back",
        extra={
            "event": "collab_flip_held_back",
            "workspace_id": str(workspace_id),
            "artifact": artifact,
        },
    )


def maybe_auto_enable(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    artifact: str,
    head_md: str,
    artifact_path: Path,
) -> bool:
    """Sticky lazy flip. Returns the artifact's EFFECTIVE enabled state.

    Enables iff: kill-switch off AND not already enabled AND the artifact has no
    pipe tables (hold-back — the CommonMark bridge would flatten them).

    The flip is SERIALIZED against in-flight git-first writers (Item 2a): a stale
    CM6 tab's save may be blocked on the commit mutex right now, about to commit a
    table-adding edit around which we must NOT flip. The cheap pre-checks run
    first (kill-switch → already-enabled → ``has_pipe_tables`` on the caller's
    already-read ``head_md`` — so a held-back GET never takes the mutex); only a
    table-free candidate acquires ``acquire_commit_mutex`` and RE-READS the
    artifact from ``artifact_path`` under the lock, re-running the hold-back on
    the FRESH bytes before committing the enable. If the mutex can't be acquired
    (a writer holds it), we defer: return False and stay legacy — the next read
    retries. COMMITS the enable itself (read models otherwise never write; the
    session factory is expire_on_commit=False so callers' loaded rows survive).
    The concurrent-first-read insert race resolves via IntegrityError → the other
    request won → report enabled."""
    if state.kill_switch_engaged():
        return False
    if state.is_collab_enabled(session, workspace_id=workspace_id, artifact=artifact):
        return True
    if has_pipe_tables(head_md):
        _log_held_back(workspace_id, artifact)
        return False
    # Table-free candidate: serialize the flip against writers. On timeout the
    # xact is aborted — rollback clears it and we defer the flip this read.
    try:
        acquire_commit_mutex(session, workspace_id, timeout_seconds=_FLIP_MUTEX_TIMEOUT_SECONDS)
    except CommitMutexTimeout:
        session.rollback()
        return False
    # Re-read fresh disk under the mutex: an in-flight save that just committed
    # may have added a table (absent file -> fall back to the pre-mutex read).
    fresh_md = artifact_path.read_text() if artifact_path.exists() else head_md
    if has_pipe_tables(fresh_md):
        session.rollback()  # release the mutex without enabling
        _log_held_back(workspace_id, artifact)
        return False
    state.enable_collab(session, workspace_id=workspace_id, artifact=artifact)
    try:
        session.commit()  # releases the mutex
    except IntegrityError:
        session.rollback()  # concurrent first read won the insert — same outcome
    logger.info(
        "collab.flip_enabled",
        extra={
            "event": "collab_flip_enabled",
            "workspace_id": str(workspace_id),
            "artifact": artifact,
        },
    )
    return True
