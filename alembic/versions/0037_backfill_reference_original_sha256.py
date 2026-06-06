"""Backfill references.original_sha256 for rows ingested before migration 0036.

Revision ID: 0037
Revises: 0036
Create Date: 2026-06-06

Best-effort data migration: for every reference whose original_sha256 is NULL,
resolve the stored file from disk and write its SHA-256 hex digest.  Rows whose
file is absent or unreadable are skipped silently — the freeze anchor simply
remains unavailable for them.

This migration NEVER hard-fails: if the filesystem root is absent, the
application is running in an environment without file storage (CI, minimal
setups), or any other unexpected error occurs, the upgrade is logged and
completes with a no-op.
"""

from __future__ import annotations

import logging
import os

from alembic import op

# revision identifiers, used by Alembic.
revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)


def upgrade() -> None:
    try:
        # Lazy import to avoid coupling at module load time.
        from pathlib import Path

        from sqlalchemy import orm

        from call_manager.proposal_assist.references.backfill import backfill_original_sha256

        # Resolve the workspaces root using the same env var the app reads.
        references_root = Path(os.environ.get("WORKSPACES_DIR", "var/workspaces"))

        bind = op.get_bind()
        # Alembic owns the surrounding transaction and commits when the
        # migration's transaction block exits; we only need the session to
        # flush its mutations (autoflush handles that). Use a context manager
        # so the session is always closed even on the best-effort error path.
        with orm.Session(bind=bind) as session:
            count = backfill_original_sha256(session, references_root)
            session.flush()
        log.info("0037 backfill: updated original_sha256 for %d reference(s)", count)
    except Exception:
        log.warning(
            "0037 backfill: best-effort data migration failed — schema is unaffected",
            exc_info=True,
        )


def downgrade() -> None:
    # Deliberate no-op: we do not null-out hashes that were successfully computed.
    pass
