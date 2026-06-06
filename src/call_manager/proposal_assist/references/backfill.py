"""Best-effort backfill of references.original_sha256 for rows ingested before
migration 0036 (which added the column). Idempotent: only fills NULL rows whose
original bytes are present on disk; rows with a missing file are left NULL (the
freeze anchor simply remains unavailable for them)."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import Reference

log = logging.getLogger(__name__)


def backfill_original_sha256(session: Session, references_root: Path | str) -> int:
    """Fill original_sha256 for references where it IS NULL and the stored
    original file exists under references_root.  Returns the count updated.
    Commits in reasonable batches.  Never raises on a single missing/unreadable
    file (skips it).

    ``references_root`` must be the same root that ``ReferenceStorage`` uses —
    i.e. the configured ``WORKSPACES_DIR`` (default ``var/workspaces``).
    ``storage_path`` is stored relative to that root as
    ``<workspace_id>/references/<reference_id>/original<suffix>``, so::

        abs_path = Path(references_root) / ref.storage_path
    """
    root = Path(references_root)
    stmt = select(Reference).where(Reference.original_sha256.is_(None))
    rows = session.scalars(stmt).all()

    count = 0
    for ref in rows:
        path = root / ref.storage_path
        if not path.is_file():
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception:
            log.warning(
                "backfill: could not read %s for reference %s — skipping",
                path,
                ref.id,
            )
            continue
        ref.original_sha256 = digest
        count += 1

    return count
