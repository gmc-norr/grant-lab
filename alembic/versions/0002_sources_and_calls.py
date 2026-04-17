"""sources and calls

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-17
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("slug", sa.String(length=50), nullable=False),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "config",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("slug", name="uq_sources_slug"),
    )

    op.create_table(
        "calls",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("full_text", sa.Text(), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("budget_min", sa.BigInteger(), nullable=True),
        sa.Column("budget_max", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("programme", sa.String(length=200), nullable=True),
        sa.Column(
            "keywords",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "raw",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], ondelete="RESTRICT", name="fk_calls_source_id"
        ),
        sa.UniqueConstraint(
            "source_id", "external_id", name="uq_calls_source_external"
        ),
    )

    # GIN supports the array overlap operator (&&) used by the keyword filter.
    op.create_index(
        "ix_calls_keywords_gin",
        "calls",
        ["keywords"],
        postgresql_using="gin",
    )
    # B-tree indices support feed sorts on the dashboard.
    op.create_index("ix_calls_deadline", "calls", ["deadline"])
    op.create_index("ix_calls_published_at", "calls", ["published_at"])

    # Seed the two v1 sources. Idempotent in spirit: the migration runs once.
    # Slugs are stable so adapters key on them.
    op.bulk_insert(
        sa.table(
            "sources",
            sa.column("name", sa.String),
            sa.column("slug", sa.String),
        ),
        [
            {"name": "EU Funding & Tenders Portal", "slug": "eu_portal"},
            {"name": "Vinnova", "slug": "vinnova"},
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_calls_published_at", table_name="calls")
    op.drop_index("ix_calls_deadline", table_name="calls")
    op.drop_index("ix_calls_keywords_gin", table_name="calls")
    op.drop_table("calls")
    op.drop_table("sources")
