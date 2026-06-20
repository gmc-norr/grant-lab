"""Migration 0052: two-mode lifecycle + spec lock.

Asserts (a) the new schema objects exist at head, (b) the new CHECK rejects an
old phase value, and (c) the data migration maps every pre-existing
spec/plan/draft/review workspace to 'active' (unlocked) while leaving 'setup'.

pg_url / migrated_engine are the SHARED session container; the round-trip test
seeds at 0051, upgrades, asserts, then restores head + cleans its rows in a
finally (mirrors tests/test_migration_0035_user_fk_restrict.py)."""

from __future__ import annotations

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command


def _cfg(pg_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_lock_schema_present_at_head(migrated_engine):
    insp = inspect(migrated_engine)
    assert "spec_lock_events" in insp.get_table_names()
    ws_cols = {c["name"] for c in insp.get_columns("workspaces")}
    assert {"spec_locked_at", "spec_locked_by"} <= ws_cols


def test_new_check_rejects_old_phase(pg_url):
    cfg = _cfg(pg_url)
    command.upgrade(cfg, "head")
    eng = create_engine(pg_url)
    try:
        with eng.begin() as c:
            c.execute(text("INSERT INTO users (email) VALUES ('lck@ki.se')"))
            uid = c.execute(text("SELECT id FROM users WHERE email='lck@ki.se'")).scalar_one()
            c.execute(
                text(
                    "INSERT INTO workspaces (id, title, owner_id, status, current_phase) "
                    "VALUES (gen_random_uuid(), 'W', :u, 'drafting', 'setup')"
                ),
                {"u": uid},
            )
        raised = False
        try:
            with eng.begin() as c:
                c.execute(
                    text(
                        "UPDATE workspaces SET current_phase='draft' "
                        "WHERE owner_id IN (SELECT id FROM users WHERE email='lck@ki.se')"
                    )
                )
        except IntegrityError:
            raised = True
        assert raised, "the {setup,active} CHECK must reject a legacy phase value"
    finally:
        with eng.begin() as c:
            c.execute(
                text(
                    "DELETE FROM workspaces WHERE owner_id IN "
                    "(SELECT id FROM users WHERE email='lck@ki.se')"
                )
            )
            c.execute(text("DELETE FROM users WHERE email='lck@ki.se'"))
        eng.dispose()


def test_data_migration_maps_old_phases_to_active(pg_url):
    cfg = _cfg(pg_url)
    eng = create_engine(pg_url)
    try:
        command.downgrade(cfg, "0051")  # before this migration
        with eng.begin() as c:
            c.execute(text("INSERT INTO users (email) VALUES ('mig@ki.se')"))
            uid = c.execute(text("SELECT id FROM users WHERE email='mig@ki.se'")).scalar_one()
            for phase in ("setup", "spec", "plan", "draft", "review"):
                c.execute(
                    text(
                        "INSERT INTO workspaces (id, title, owner_id, status, current_phase) "
                        "VALUES (gen_random_uuid(), :t, :u, 'drafting', :p)"
                    ),
                    {"t": phase, "u": uid, "p": phase},
                )
        command.upgrade(cfg, "head")
        with eng.connect() as c:
            rows = c.execute(
                text(
                    "SELECT title, current_phase, spec_locked_at FROM workspaces "
                    "WHERE owner_id IN (SELECT id FROM users WHERE email='mig@ki.se')"
                )
            ).all()
        by_title = {r.title: r for r in rows}
        assert by_title["setup"].current_phase == "setup"
        for phase in ("spec", "plan", "draft", "review"):
            assert by_title[phase].current_phase == "active", phase
        assert all(r.spec_locked_at is None for r in rows)
    finally:
        with eng.begin() as c:
            c.execute(
                text(
                    "DELETE FROM workspaces WHERE owner_id IN "
                    "(SELECT id FROM users WHERE email='mig@ki.se')"
                )
            )
            c.execute(text("DELETE FROM users WHERE email='mig@ki.se'"))
        command.upgrade(cfg, "head")  # restore head for later tests
        eng.dispose()
