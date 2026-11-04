"""Backfill references.original_sha256 for rows ingested before migration 0036.

Revision ID: 0037
Revises: 0036
Create Date: 2026-06-06

Best-effort data migration: for every reference whose original_sha256 is NULL,
resolve the stored file from disk and write its SHA-256 hex digest.  Rows whose
file is absent or unreadable are skipped silently — the freeze anchor simply
remains unavailable for them.

The data pass NEVER hard-fails the upgrade: if the filesystem root is absent, the
application is running in an environment without file storage (CI, minimal
setups), or any other unexpected error occurs in the pass, it is logged and the
upgrade completes with the hashes it managed. Only the data pass is wrapped.

FROZEN COPY. `_backfill_original_sha256` below is
`grant_lab.proposal_assist.references.backfill.backfill_original_sha256` as it
stood at 388494c8, copied in on 2026-11-03 when the proposal workspace left master
(docs/superpowers/specs/2026-11-03-workspace-removal-design.md §3). The original
is on the `workspace` branch and the `workspace-v1` tag. A migration must keep
running after the code it once imported is gone, so this file imports nothing
from `grant_lab`. The one change: it takes the migration's Connection instead of
an ORM Session wrapped around that same Connection. The SQL is the same.
Tests: tests/test_migration_0037_backfill.py.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

# revision identifiers, used by Alembic.
revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)

# A lightweight Core table naming only the columns this backfill touches, never
# an ORM model: selecting a full entity would emit `SELECT <every model column>`
# and fail with "column does not exist" on a database at 0037 the moment the
# model grew a later column (e.g. 0038's tombstoned_at).
_references = sa.table(
    "references",
    sa.column("id"),
    sa.column("storage_path"),
    sa.column("original_sha256"),
)


def _backfill_original_sha256(conn: Connection, references_root: Path | str) -> int:
    """Fill original_sha256 for references where it IS NULL and the stored
    original file exists under references_root. Returns the count updated.
    Does NOT commit. Never raises on a single missing/unreadable file (skips it).

    ``references_root`` is the app's ``WORKSPACES_DIR`` (default
    ``var/workspaces``). ``storage_path`` is stored relative to it as
    ``<workspace_id>/references/<reference_id>/original<suffix>``. A path that
    resolves outside the root is skipped, never read.
    """
    root_resolved = Path(references_root).resolve()
    stmt = (
        _references.select()
        .with_only_columns(_references.c.id, _references.c.storage_path)
        .where(_references.c.original_sha256.is_(None))
    )
    # Materialises all NULL rows at once — acceptable for a one-time backfill.
    rows = conn.execute(stmt).all()

    count = 0
    for ref_id, storage_path in rows:
        path = (root_resolved / storage_path).resolve()
        if not path.is_relative_to(root_resolved):
            log.warning(
                "backfill: skipping reference %s: storage_path escapes references_root",
                ref_id,
            )
            continue
        if not path.is_file():
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception:
            log.warning(
                "backfill: could not read %s for reference %s — skipping",
                path,
                ref_id,
            )
            continue
        conn.execute(
            sa.update(_references).where(_references.c.id == ref_id).values(original_sha256=digest)
        )
        count += 1

    return count


def upgrade() -> None:
    # Resolve the workspaces root using the same env var the app read.
    references_root = Path(os.environ.get("WORKSPACES_DIR", "var/workspaces"))
    # Best-effort, and only the data pass is: nothing outside this block can fail
    # on an import any more, because the code it runs is in this file.
    try:
        count = _backfill_original_sha256(op.get_bind(), references_root)
    except Exception:
        log.warning(
            "0037 backfill: best-effort data migration failed — schema is unaffected",
            exc_info=True,
        )
        return
    log.info("0037 backfill: updated original_sha256 for %d reference(s)", count)


def downgrade() -> None:
    # Deliberate no-op: we do not null-out hashes that were successfully computed.
    pass
