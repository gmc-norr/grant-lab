"""Add summary + applicant_notes columns to call_rankings

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-18

When the cosine pre-filter is high enough that the worker escalates to
Sonnet for a deeper analysis, the prompt also asks Sonnet for a 2-3
sentence summary of the call + a short list of `applicant_notes` (odd
requirements, tight deadlines, mandatory co-funding, IP/data clauses,
etc.). Both fields are optional — Gemma-ranked rows keep them NULL and
the UI hides the analysis section there.

Schema deltas:
- call_rankings.summary: TEXT NULL
- call_rankings.applicant_notes: JSONB NULL — list of {category, text}
  objects. Application-level shape is enforced in
  ranking/prompt.py::_sanitize_applicant_notes; the column itself is
  permissive so legacy schemas don't reject a partial-write rollback.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "call_rankings",
        sa.Column("summary", sa.Text(), nullable=True),
    )
    op.add_column(
        "call_rankings",
        sa.Column("applicant_notes", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("call_rankings", "applicant_notes")
    op.drop_column("call_rankings", "summary")
