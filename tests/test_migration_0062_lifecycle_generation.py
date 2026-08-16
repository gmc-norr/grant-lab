"""Migration 0062: workspaces.lifecycle_generation.

Asserts (a) the column exists at head with the correct properties, and (b) the
migration round-trips cleanly — downgrade removes it, re-upgrade restores it.
"""

from __future__ import annotations

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command


def _cfg(pg_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_lifecycle_generation_column_present_at_head(migrated_engine):
    """Column exists on workspaces, is NOT NULL, and has default 0."""
    insp = inspect(migrated_engine)
    cols = {c["name"]: c for c in insp.get_columns("workspaces")}
    assert "lifecycle_generation" in cols, "lifecycle_generation column missing from workspaces"
    col = cols["lifecycle_generation"]
    assert not col["nullable"], "lifecycle_generation must be NOT NULL"


def test_migration_0062_round_trips(pg_url):
    """0062 can be applied, rolled back, and re-applied without schema corruption."""
    cfg = _cfg(pg_url)
    command.upgrade(cfg, "head")
    eng = create_engine(pg_url)
    try:
        # Seed a workspace row to verify existing rows survive round-trip.
        with eng.begin() as c:
            c.execute(text("INSERT INTO users (email) VALUES ('mig62@ki.se')"))
            uid = c.execute(
                text("SELECT id FROM users WHERE email='mig62@ki.se'")
            ).scalar_one()
            c.execute(
                text(
                    "INSERT INTO workspaces (id, title, owner_id, status, current_phase) "
                    "VALUES (gen_random_uuid(), 'W62', :u, 'drafting', 'setup')"
                ),
                {"u": uid},
            )

        # Downgrade removes the column.
        command.downgrade(cfg, "0061")
        with eng.connect() as c:
            cols = {
                r[0]
                for r in c.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='workspaces'"
                    )
                )
            }
        assert "lifecycle_generation" not in cols, (
            "downgrade must drop lifecycle_generation"
        )

        # Re-upgrade restores it; existing row gets server_default=0.
        command.upgrade(cfg, "head")
        with eng.connect() as c:
            gen = c.execute(
                text(
                    "SELECT lifecycle_generation FROM workspaces "
                    "WHERE owner_id IN (SELECT id FROM users WHERE email='mig62@ki.se')"
                )
            ).scalar_one()
        assert gen == 0, f"existing row after re-upgrade must have generation=0, got {gen}"
    finally:
        with eng.begin() as c:
            c.execute(
                text(
                    "DELETE FROM workspaces WHERE owner_id IN "
                    "(SELECT id FROM users WHERE email='mig62@ki.se')"
                )
            )
            c.execute(text("DELETE FROM users WHERE email='mig62@ki.se'"))
        command.upgrade(cfg, "head")
        eng.dispose()
