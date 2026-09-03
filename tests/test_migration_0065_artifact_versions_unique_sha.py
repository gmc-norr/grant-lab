"""Migration 0065: one artifact_versions row per (workspace_id, commit_sha).

Asserts the unique index exists at head with the right columns, that the
pre-existing non-unique ix_artifact_versions_ws_created is untouched, that the
migration round-trips, and — the point that matters — that the pre-index dedup
keeps the PROVENANCE-bearing row and drops the derived 'reconciled' one, even
when the reconciled row is OLDER (the realistic drift case).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command


def _cfg(pg_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def _index(insp, table: str, name: str):
    for ix in insp.get_indexes(table):
        if ix["name"] == name:
            return ix
    return None


def test_unique_index_present_at_head(migrated_engine):
    """At head, artifact_versions has uq_artifact_versions_ws_sha over
    (workspace_id, commit_sha) and it is UNIQUE."""
    insp = inspect(migrated_engine)
    ix = _index(insp, "artifact_versions", "uq_artifact_versions_ws_sha")
    assert ix is not None, "uq_artifact_versions_ws_sha missing at head"
    assert ix["column_names"] == ["workspace_id", "commit_sha"], (
        f"unexpected columns for the unique index: {ix['column_names']!r}"
    )
    assert ix["unique"] is True, "uq_artifact_versions_ws_sha must be UNIQUE"


def test_preexisting_ws_created_index_untouched(migrated_engine):
    """0065 must not disturb the non-unique ix_artifact_versions_ws_created."""
    insp = inspect(migrated_engine)
    ix = _index(insp, "artifact_versions", "ix_artifact_versions_ws_created")
    assert ix is not None, "ix_artifact_versions_ws_created disappeared"
    assert ix["unique"] is False, (
        "ix_artifact_versions_ws_created must stay NON-unique — 0065 only adds a "
        "second index, it must not alter this one"
    )
    assert ix["column_names"][0] == "workspace_id", (
        f"ix_artifact_versions_ws_created leads on the wrong column: "
        f"{ix['column_names']!r}"
    )


def test_migration_0065_round_trips(pg_url, pg_session_factory):
    """upgrade head -> downgrade 0064 -> upgrade head is clean, and after the
    downgrade the unique index is ABSENT.

    `pg_session_factory` is requested SOLELY for its teardown TRUNCATE, and the
    upgrade is wrapped in try/finally so the shared session-scoped container is
    restored to head even on failure (same precedent as
    test_migration_0064_claimed_at_generation.py)."""
    cfg = _cfg(pg_url)
    eng = create_engine(pg_url)
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0064")
        try:
            insp = inspect(eng)
            assert (
                _index(insp, "artifact_versions", "uq_artifact_versions_ws_sha")
                is None
            ), "downgrade left uq_artifact_versions_ws_sha in place"
        finally:
            command.upgrade(cfg, "head")

        insp = inspect(eng)
        ix = _index(insp, "artifact_versions", "uq_artifact_versions_ws_sha")
        assert ix is not None, "re-upgrade did not restore the unique index"
        assert ix["unique"] is True
    finally:
        eng.dispose()


def test_dedup_keeps_the_provenance_row_not_the_reconciled_one(pg_url, pg_session_factory):
    """The pre-index dedup must keep the PROVENANCE row and drop the derived
    'reconciled' one — even though the reconciled row is OLDER.

    This is the realistic drift pair: a 'reconciled' row stamped with the git
    commit's own (older) time, and a 'user' row at the newer server time that
    carries a real triggering_turn_id. Ranking by age alone would keep the
    reconciled row and destroy the turn id; this asserts it does NOT.

    Seed at revision 0064 (before the index exists) via raw SQL, satisfying the
    real FK chain: user -> workspace -> chat_turn -> the two artifact_versions.
    `pg_session_factory` is requested solely for its teardown TRUNCATE.
    """
    cfg = _cfg(pg_url)
    eng = create_engine(pg_url)
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0064")
        try:
            older = datetime.now(UTC) - timedelta(days=2)
            newer = datetime.now(UTC)
            with eng.begin() as c:
                c.execute(text("INSERT INTO users (email) VALUES ('mig65@umu.se')"))
                uid = c.execute(
                    text("SELECT id FROM users WHERE email='mig65@umu.se'")
                ).scalar_one()
                c.execute(
                    text(
                        "INSERT INTO workspaces (id, title, owner_id, status, "
                        "current_phase) VALUES (gen_random_uuid(), 'W65', :u, "
                        "'drafting', 'active')"
                    ),
                    {"u": uid},
                )
                ws_id = c.execute(
                    text("SELECT id FROM workspaces WHERE title='W65'")
                ).scalar_one()
                # A real chat_turn so P's triggering_turn_id references a live row
                # (the FK is nullable, but a non-NULL value must resolve).
                c.execute(
                    text(
                        "INSERT INTO chat_turns (id, workspace_id, role, content) "
                        "VALUES (gen_random_uuid(), :w, 'user', '{}'::jsonb)"
                    ),
                    {"w": ws_id},
                )
                turn_id = c.execute(
                    text("SELECT id FROM chat_turns WHERE workspace_id = :w"),
                    {"w": ws_id},
                ).scalar_one()
                # Row R: the derived 'reconciled' row, OLDER, no turn id.
                c.execute(
                    text(
                        "INSERT INTO artifact_versions (id, workspace_id, commit_sha, "
                        "triggering_turn_id, created_by, message, created_at) VALUES "
                        "(gen_random_uuid(), :w, 'cafef00d', NULL, 'reconciled', "
                        "'derived', :ts)"
                    ),
                    {"w": ws_id, "ts": older},
                )
                # Row P: the provenance row, NEWER, carries the turn id.
                c.execute(
                    text(
                        "INSERT INTO artifact_versions (id, workspace_id, commit_sha, "
                        "triggering_turn_id, created_by, message, created_at) VALUES "
                        "(gen_random_uuid(), :w, 'cafef00d', :t, 'user', 'authored', "
                        ":ts)"
                    ),
                    {"w": ws_id, "t": turn_id, "ts": newer},
                )
        finally:
            command.upgrade(cfg, "head")

        # After 0065's dedup: exactly one row survives, it is P, and its
        # triggering_turn_id is still set.
        with eng.begin() as c:
            rows = c.execute(
                text(
                    "SELECT created_by, triggering_turn_id, message "
                    "FROM artifact_versions WHERE workspace_id = :w "
                    "AND commit_sha = 'cafef00d'"
                ),
                {"w": ws_id},
            ).all()
        assert len(rows) == 1, (
            f"dedup must leave exactly one row per (workspace, sha), got {len(rows)}"
        )
        created_by, surviving_turn_id, message = rows[0]
        assert created_by == "user", (
            f"dedup kept the wrong row — it must keep the provenance ('user') row, "
            f"not the derived one, got created_by={created_by!r}"
        )
        assert surviving_turn_id is not None, (
            "the surviving row's triggering_turn_id was destroyed — the dedup "
            "must preserve provenance, not the git-timestamp-derived row"
        )
        assert str(surviving_turn_id) == str(turn_id)
        assert message == "authored"
    finally:
        eng.dispose()
