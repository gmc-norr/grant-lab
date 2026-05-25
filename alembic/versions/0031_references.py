"""references and reference_chunks

Revision ID: 0031
Revises: 0030
Create Date: 2026-05-25

Two new tables (references, reference_chunks) and ALTER turn_jobs ADD fetch_count.
See docs/superpowers/specs/2026-05-25-references-corpus-design.md for details.
"""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "references",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("bytes_total", sa.Integer(), nullable=True),
        # Stage 1 — text extract + chunk + embed status
        sa.Column("text_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("text_error", sa.Text(), nullable=True),
        sa.Column("text_indexed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("text_chars_total", sa.Integer(), nullable=True),
        # v1.1 scaffolding — PageIndex outline (always 'skipped' in v1)
        sa.Column("outline_status", sa.Text(), nullable=False, server_default="skipped"),
        sa.Column("outline_error", sa.Text(), nullable=True),
        sa.Column("outline_indexed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "uploaded_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('paper','prior_proposal','analysis','data')",
            name="ck_references_kind",
        ),
        sa.CheckConstraint(
            "source_type IN ('uploaded','crossref','pubmed','arxiv','web')",
            name="ck_references_source_type",
        ),
        sa.CheckConstraint(
            "text_status IN ('pending','running','done','failed','skipped')",
            name="ck_references_text_status",
        ),
        sa.CheckConstraint(
            "outline_status IN ('pending','running','done','failed','skipped')",
            name="ck_references_outline_status",
        ),
    )
    op.create_index("ix_references_ws", "references", ["workspace_id"])
    op.create_index(
        "ix_references_indexing_backlog",
        "references",
        ["workspace_id", "created_at"],
        postgresql_where=sa.text(
            "text_status IN ('pending','running') "
            "OR outline_status IN ('pending','running')"
        ),
    )

    op.create_table(
        "reference_chunks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "reference_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("references.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("section_path", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "reference_id", "chunk_index", name="uq_reference_chunks_ref_idx"
        ),
    )
    # HNSW index for semantic search; pgvector already loaded by earlier migration.
    op.execute(
        "CREATE INDEX ix_reference_chunks_ws_embedding "
        "ON reference_chunks USING hnsw (embedding vector_cosine_ops)"
    )

    op.add_column(
        "turn_jobs",
        sa.Column(
            "fetch_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("turn_jobs", "fetch_count")
    op.execute("DROP INDEX IF EXISTS ix_reference_chunks_ws_embedding")
    op.drop_table("reference_chunks")
    op.drop_index("ix_references_indexing_backlog", table_name="references")
    op.drop_index("ix_references_ws", table_name="references")
    op.drop_table("references")
