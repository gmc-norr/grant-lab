"""artifact_versions: one row per (workspace, commit sha)

Revision ID: 0065
Revises: 0064
"""

from __future__ import annotations

from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Reconciliation's read-then-insert has no DB-level guard, so two
    # reconcilers racing (a workspace open and a future admin-triggered run)
    # can each miss the other's row and both insert. A duplicate is
    # user-visible: a second entry in the version history for one commit.
    #
    # Deduplicate before indexing — a deployment that already drifted would
    # otherwise fail the migration.
    #
    # DO NOT rank by created_at alone. A 'reconciled' row carries the GIT
    # COMMIT's timestamp (reconcile.py:96), while a normal row uses the
    # server's now() default, so the reconciled row is routinely the OLDER
    # one. Ranking by age would delete the provenance-bearing row and keep
    # the derived one. Provenance class first; age only breaks ties within it.
    op.execute(
        """
        DELETE FROM artifact_versions a
        USING artifact_versions b
        WHERE a.workspace_id = b.workspace_id
          AND a.commit_sha   = b.commit_sha
          AND (
                (a.created_by = 'reconciled') > (b.created_by = 'reconciled')
             OR (
                  (a.created_by = 'reconciled') = (b.created_by = 'reconciled')
                  AND (a.created_at, a.id) > (b.created_at, b.id)
                )
              )
        """
    )
    op.create_index(
        "uq_artifact_versions_ws_sha",
        "artifact_versions",
        ["workspace_id", "commit_sha"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_artifact_versions_ws_sha", table_name="artifact_versions")
