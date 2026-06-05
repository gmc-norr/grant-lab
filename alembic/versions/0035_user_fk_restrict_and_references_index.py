"""Explicit ON DELETE RESTRICT on user-referencing FKs + references.uploaded_by index

Revision ID: 0035
Revises: 0034
Create Date: 2026-06-05

Code-review follow-ups to the user-FK delete policy:

1. references.uploaded_by gains an index (ix_references_uploaded_by). It is a
   FK to users(id) whose parent rows can be deleted, and the column was
   previously unindexed — forcing a full child-table scan on every user delete
   and slowing FK validation. This is the higher-value change.

2. Several user-referencing FKs were created inline (unnamed) and therefore
   defaulted to ON DELETE NO ACTION, while sibling FKs to users.id use CASCADE
   / SET NULL. To make the (already effective) RESTRICT-style semantics
   explicit, the following FKs are re-created with an explicit ON DELETE
   RESTRICT:

       workspaces.owner_id
       attachments.uploaded_by
       phase_approvals.approving_user_id
       turn_jobs.submitted_by_user_id
       references.uploaded_by

   NO ACTION and RESTRICT both forbid deleting a referenced user (the only
   difference is deferral timing, which none of these constraints use), so this
   is a clarity-only change with no behavioral effect on existing data.

Constraint names: these FKs were all created unnamed in their original
migrations (0014, 0021, 0024, 0018, 0031), so Postgres assigned the standard
`<table>_<column>_fkey` names. Rather than hardcode and risk a mismatch, both
upgrade and downgrade look the constraint name up from the catalog
(information_schema) by (table, column) and drop/recreate it dynamically — so
the migration is correct regardless of the exact auto-generated name.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


# (table, column) pairs whose FK to users.id we make explicit.
_USER_FKS = [
    ("workspaces", "owner_id"),
    ("attachments", "uploaded_by"),
    ("phase_approvals", "approving_user_id"),
    ("turn_jobs", "submitted_by_user_id"),
    ("references", "uploaded_by"),
]


def _find_user_fk_name(conn, table: str, column: str) -> str | None:
    """Return the name of the FK on (table, column) that references users.id,
    or None if no such constraint currently exists."""
    row = conn.execute(
        sa.text(
            """
            SELECT tc.constraint_name
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage AS ccu
              ON tc.constraint_name = ccu.constraint_name
             AND tc.table_schema = ccu.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_name = :table
              AND kcu.column_name = :column
              AND ccu.table_name = 'users'
              AND ccu.column_name = 'id'
            LIMIT 1
            """
        ),
        {"table": table, "column": column},
    ).scalar()
    return row


def _recreate_user_fk(conn, table: str, column: str, ondelete: str | None) -> None:
    name = _find_user_fk_name(conn, table, column)
    if name is None:
        return
    op.drop_constraint(name, table, type_="foreignkey")
    op.create_foreign_key(
        name,
        table,
        "users",
        [column],
        ["id"],
        ondelete=ondelete,
    )


def upgrade() -> None:
    conn = op.get_bind()

    op.create_index("ix_references_uploaded_by", "references", ["uploaded_by"])

    # Re-create each user FK with explicit ON DELETE RESTRICT, preserving the
    # original (auto-generated) constraint name.
    for table, column in _USER_FKS:
        _recreate_user_fk(conn, table, column, ondelete="RESTRICT")


def downgrade() -> None:
    conn = op.get_bind()

    # Restore the original implicit (NO ACTION) policy by re-creating the FKs
    # without an ondelete clause, keeping the same constraint name.
    for table, column in _USER_FKS:
        _recreate_user_fk(conn, table, column, ondelete=None)

    op.drop_index("ix_references_uploaded_by", table_name="references")
