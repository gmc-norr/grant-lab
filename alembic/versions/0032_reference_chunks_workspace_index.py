"""reference_chunks: composite (workspace_id, embedding_model) index

Slice 2's search_references query filters by workspace_id AND
embedding_model BEFORE the HNSW order-by. Without a btree on these
columns, Postgres has to scan all chunks across workspaces to find
candidates before sorting by distance — the HNSW index loses its
benefit at scale. Composite index keeps the candidate set small.

Per post-Slice-2 code review (Important #3).

Revision ID: 0032
Revises: 0031_references

"""

from alembic import op


revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_reference_chunks_ws_model",
        "reference_chunks",
        ["workspace_id", "embedding_model"],
    )


def downgrade() -> None:
    op.drop_index("ix_reference_chunks_ws_model", table_name="reference_chunks")
