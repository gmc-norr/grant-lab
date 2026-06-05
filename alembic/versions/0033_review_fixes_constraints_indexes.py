"""Review fixes: FK + indexes + Stockholm digest week + session epoch

Revision ID: 0033
Revises: 0032
Create Date: 2026-06-05

Bundles the schema-level findings from the 2026-06-05 code review:

1. digest_runs.week_start is regenerated with `date_trunc('week', sent_at,
   'Europe/Stockholm')` instead of UTC, so the DB idempotency constraint
   agrees with the app's Stockholm-week skip check (digest.send.week_start).
   A UTC truncation disagreed in the Sun-22:00..Mon-00:00-UTC seam, which
   could miss or duplicate a weekly digest.
2. reference_chunks.workspace_id gains a FK to workspaces(id) ON DELETE
   CASCADE — it was a free-floating denormalized UUID with no referential
   guarantee.
3. ix_references_ws is renamed to ix_references_workspace_id so the deployed
   index name matches the ORM's `index=True` auto-name (no duplicate index
   on autogenerate).
4. Missing indexes on FK columns whose parent rows can be deleted/updated
   (workspaces.call_id/profile_id, turn_jobs.user_turn_id/submitted_by_user_id,
   artifact_versions.triggering_turn_id, attachments.uploaded_by,
   review_runs.waived_from_review_run_id) — each previously forced a child-
   table scan on parent delete.
5. users.session_epoch — monotonic counter embedded in the signed session
   cookie so logout / admin revocation can invalidate outstanding cookies.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


# (table, column, index name) for the plain FK indexes added below.
_FK_INDEXES = [
    ("workspaces", "call_id", "ix_workspaces_call_id"),
    ("workspaces", "profile_id", "ix_workspaces_profile_id"),
    ("turn_jobs", "user_turn_id", "ix_turn_jobs_user_turn_id"),
    ("turn_jobs", "submitted_by_user_id", "ix_turn_jobs_submitted_by_user_id"),
    ("artifact_versions", "triggering_turn_id", "ix_artifact_versions_triggering_turn_id"),
    ("attachments", "uploaded_by", "ix_attachments_uploaded_by"),
    ("review_runs", "waived_from_review_run_id", "ix_review_runs_waived_from_review_run_id"),
]


def upgrade() -> None:
    # 1. Digest week boundary: UTC -> Europe/Stockholm. The unique index
    #    depends on the generated column, so drop it first, regenerate the
    #    column, then recreate the index. Generated columns cannot be ALTERed
    #    in place — drop + re-add is the only path.
    op.drop_index("uq_digest_runs_user_week", table_name="digest_runs")
    op.drop_column("digest_runs", "week_start")
    op.add_column(
        "digest_runs",
        sa.Column(
            "week_start",
            sa.DateTime(timezone=True),
            sa.Computed("date_trunc('week', sent_at, 'Europe/Stockholm')", persisted=True),
            nullable=True,
        ),
    )
    op.create_index(
        "uq_digest_runs_user_week",
        "digest_runs",
        ["user_id", "week_start"],
        unique=True,
    )

    # 2. reference_chunks.workspace_id -> workspaces(id) FK.
    op.create_foreign_key(
        "fk_reference_chunks_workspace_id",
        "reference_chunks",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 3. Rename references workspace index to match the ORM auto-name.
    op.execute("ALTER INDEX ix_references_ws RENAME TO ix_references_workspace_id")

    # 4. FK-column indexes.
    for table, column, name in _FK_INDEXES:
        op.create_index(name, table, [column])

    # 5. users.session_epoch (cookie-invalidation counter).
    op.add_column(
        "users",
        sa.Column("session_epoch", sa.Integer(), nullable=False, server_default="0"),
    )

    # 6. Trigram GIN indexes so the digest must-include `ILIKE '%term%'` scans
    #    (title/summary/full_text) become index lookups instead of per-term
    #    sequential scans of the calls table on every weekly run.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for column in ("title", "summary", "full_text"):
        op.create_index(
            f"ix_calls_{column}_trgm",
            "calls",
            [column],
            postgresql_using="gin",
            postgresql_ops={column: "gin_trgm_ops"},
        )


def downgrade() -> None:
    for column in ("title", "summary", "full_text"):
        op.drop_index(f"ix_calls_{column}_trgm", table_name="calls")
    # Leave the pg_trgm extension installed — other objects may use it.

    op.drop_column("users", "session_epoch")

    for table, _column, name in _FK_INDEXES:
        op.drop_index(name, table_name=table)

    op.execute("ALTER INDEX ix_references_workspace_id RENAME TO ix_references_ws")

    op.drop_constraint("fk_reference_chunks_workspace_id", "reference_chunks", type_="foreignkey")

    # Restore the UTC-truncated week column + unique index.
    op.drop_index("uq_digest_runs_user_week", table_name="digest_runs")
    op.drop_column("digest_runs", "week_start")
    op.add_column(
        "digest_runs",
        sa.Column(
            "week_start",
            sa.DateTime(timezone=True),
            sa.Computed("date_trunc('week', sent_at, 'UTC')", persisted=True),
            nullable=True,
        ),
    )
    op.create_index(
        "uq_digest_runs_user_week",
        "digest_runs",
        ["user_id", "week_start"],
        unique=True,
    )
