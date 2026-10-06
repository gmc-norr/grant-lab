"""Migration 0083: llm_spend accepts kind='setup' (Phase 0, review 2026-10-06 H2)."""

from __future__ import annotations

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from tests.proposal_assist.conftest import make_workspace


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


_INSERT = text("INSERT INTO llm_spend (workspace_id, kind, status) VALUES (:w, 'setup', 'ok')")


def test_0083_admits_setup_and_its_downgrade_drops_those_rows(
    migrated_engine, pg_url, pg_session_factory
):
    with pg_session_factory() as s:
        ws_id = make_workspace(s).id
        s.commit()
    engine = create_engine(pg_url)
    try:
        with engine.begin() as conn:
            conn.execute(_INSERT, {"w": ws_id})

        command.downgrade(_cfg(pg_url), "0082")
        with engine.connect() as conn:
            left = conn.execute(
                text("SELECT count(*) FROM llm_spend WHERE workspace_id=:w"), {"w": ws_id}
            ).scalar_one()
            assert left == 0, "the old CHECK cannot hold a setup row"
            with pytest.raises(IntegrityError, match="ck_llm_spend_kind"):
                conn.execute(_INSERT, {"w": ws_id})
            conn.rollback()

        command.upgrade(_cfg(pg_url), "head")
        with engine.begin() as conn:
            conn.execute(_INSERT, {"w": ws_id})
    finally:
        try:
            command.upgrade(_cfg(pg_url), "head")
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM llm_spend WHERE workspace_id=:w"), {"w": ws_id})
        finally:
            engine.dispose()
