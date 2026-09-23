"""Migration 0080: `calls.embedding_fingerprint`, the call half of 0075.

Mirrors `calls.embedding_fingerprint` on `profiles.embedding_fingerprint`
(migration 0075) -- same column, same NULL-means-stale contract, no
grandfathering (spec docs/superpowers/specs/2026-09-08-call-embedding-freshness.md,
D4). Unlike 0075, there is no paired-NULL CHECK asymmetry to pin here: calls
never had a `call`-side fingerprint column before, so there is no prior
constraint a new one could collide with.
"""

from __future__ import annotations

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_column_exists_nullable_text_after_upgrade(migrated_engine):
    insp = inspect(migrated_engine)
    cols = {c["name"]: c for c in insp.get_columns("calls")}
    assert "embedding_fingerprint" in cols
    col = cols["embedding_fingerprint"]
    assert col["nullable"] is True
    assert "TEXT" in str(col["type"]).upper()


def test_0080_round_trips_and_leaves_existing_rows_null(migrated_engine, pg_url):
    """The data gate: seed a call AT 0079, then upgrade, then round-trip the drop.

    `migrated_engine` is requested but unused in the body ON PURPOSE: it forces
    `upgrade head` to have run before this test's own `downgrade 0079` below,
    exactly as test_migration_0075_freshness_fingerprints.py does for the
    profile half. Do not delete it -- without it the shared container's
    revision at entry is whatever the previous test file left behind.

    Proves, in one alembic drive:
    1. At 0079, `embedding_fingerprint` does not exist on `calls`.
    2. A call row inserted before 0080 comes out of `upgrade head` with a
       NULL `embedding_fingerprint` -- the column adds no server_default and
       no backfill, so an existing row is stale by construction (D4).
    3. `downgrade` to 0079 removes the column again (Gate 1's real leg: a
       plain `upgrade` alone would trivially pass; only the round trip
       catches a missing `drop_column`).
    4. `upgrade head` again brings the column back.

    `finally` restores head and deletes only the rows this file seeded --
    the container is shared session-wide across the whole suite.
    """
    engine = create_engine(pg_url)
    try:
        command.downgrade(_cfg(pg_url), "0079")
        with engine.begin() as conn:
            assert "embedding_fingerprint" not in {
                c["name"] for c in inspect(conn).get_columns("calls")
            }
            src = conn.execute(
                text("INSERT INTO sources (name, slug) VALUES ('S0080','s0080') RETURNING id")
            ).scalar_one()
            cid = conn.execute(
                text(
                    "INSERT INTO calls (source_id, external_id, url, title) "
                    "VALUES (:s, 'M0080', 'https://example.org/m0080', 't') "
                    "RETURNING id"
                ),
                {"s": src},
            ).scalar_one()

        # This is Gate 1's real leg -- `upgrade head` alone would pass even
        # with a missing/broken `downgrade`.
        command.upgrade(_cfg(pg_url), "head")
        with engine.connect() as conn:
            insp = inspect(conn)
            assert "embedding_fingerprint" in {c["name"] for c in insp.get_columns("calls")}
            fingerprint = conn.execute(
                text("SELECT embedding_fingerprint FROM calls WHERE id=:c"),
                {"c": cid},
            ).scalar_one()
        assert fingerprint is None, (
            "an existing call must come out of 0080 with a NULL fingerprint "
            f"(re-embedded on the first tick), got {fingerprint!r}"
        )

        # Round-trip the drop: downgrade removes it, re-upgrade brings it back.
        command.downgrade(_cfg(pg_url), "0079")
        with engine.begin() as conn:
            assert "embedding_fingerprint" not in {
                c["name"] for c in inspect(conn).get_columns("calls")
            }
        command.upgrade(_cfg(pg_url), "head")
        with engine.connect() as conn:
            assert "embedding_fingerprint" in {
                c["name"] for c in inspect(conn).get_columns("calls")
            }
    finally:
        try:
            command.upgrade(_cfg(pg_url), "head")
            with engine.begin() as conn:
                # Scoped to the rows this file seeds -- an unscoped DELETE
                # would wipe another file's rows on this xdist worker.
                conn.execute(text("DELETE FROM calls WHERE external_id='M0080'"))
                conn.execute(text("DELETE FROM sources WHERE slug='s0080'"))
        finally:
            # Last, and unskippable: a raise in the cleanup above must not
            # leak a connection. pyproject.toml turns ResourceWarning into an
            # error, which would surface inside an unrelated test.
            engine.dispose()
