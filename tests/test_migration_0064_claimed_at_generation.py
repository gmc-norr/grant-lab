"""Migration 0064: claimed_at_generation on the 7 live job tables.

Asserts the column exists at head on every table with the right properties, that
the dead AttachmentJob table is NOT touched, and that the migration round-trips.
"""

from __future__ import annotations

from alembic.config import Config
from sqlalchemy import Integer, create_engine, inspect, text

from alembic import command

# The 7 LIVE job tables. attachment_jobs is deliberately absent: dead schema, no
# production reference outside db/models.py, no claim path.
FENCED_TABLES = [
    "turn_jobs",
    "coherence_jobs",
    "evaluator_jobs",
    "uncited_claims_jobs",
    "workspace_setup_jobs",
    "review_runs",
    "claim_analysis_jobs",
]


def _cfg(pg_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_claimed_at_generation_present_on_every_fenced_table(migrated_engine):
    insp = inspect(migrated_engine)
    for table in FENCED_TABLES:
        cols = {c["name"]: c for c in insp.get_columns(table)}
        assert "claimed_at_generation" in cols, (
            f"{table} is missing claimed_at_generation"
        )
        col = cols["claimed_at_generation"]
        assert col["nullable"], (
            f"{table}.claimed_at_generation must be NULLABLE — rows in flight when "
            f"the migration lands have no generation to record"
        )
        assert col["default"] is None, (
            f"{table}.claimed_at_generation must have NO server_default, got "
            f"{col['default']!r}. Generation 0 is a REAL generation "
            f"(workspaces.lifecycle_generation defaults to 0), so a default of 0 "
            f"would assert 'claimed at generation 0' about every legacy row."
        )


def test_the_dead_attachment_jobs_table_is_not_touched(migrated_engine):
    """attachment_jobs is dead schema (no production reference outside
    db/models.py). Adding the column there is scope creep the spec excludes."""
    insp = inspect(migrated_engine)
    cols = {c["name"] for c in insp.get_columns("attachment_jobs")}
    assert "claimed_at_generation" not in cols, (
        "attachment_jobs must NOT get claimed_at_generation — it is dead schema "
        "with no claim path (spec 4.5)"
    )


def test_every_model_declares_the_column_matching_the_migration():
    """ORM/migration agreement. NOTHING ELSE IN THIS REPO CHECKS THIS.

    There is no metadata-vs-schema comparison anywhere in the suite —
    test_alembic_v2_round_trip.py round-trips 0005-0008 and asserts specific
    named columns; it does not diff Base.metadata against head. So an
    implementation that adds all seven DATABASE columns and zero MODEL
    properties passes every other test in this plan, and the drift only surfaces
    in F4-F7 when a claim path tries to set an attribute that does not exist.

    Reads the mapped columns directly, so it needs no database.
    """
    from grant_lab.db.models.workspace import (
        AttachmentJob,
        ClaimAnalysisJob,
        CoherenceJob,
        EvaluatorJob,
        ReviewRun,
        TurnJob,
        UncitedClaimsJob,
        WorkspaceSetupJob,
    )

    fenced_models = [
        TurnJob,
        CoherenceJob,
        EvaluatorJob,
        UncitedClaimsJob,
        WorkspaceSetupJob,
        ReviewRun,
        ClaimAnalysisJob,
    ]
    assert {m.__tablename__ for m in fenced_models} == set(FENCED_TABLES), (
        "the models checked here and the tables in the migration have drifted"
    )

    for model in fenced_models:
        col = model.__table__.c.get("claimed_at_generation")
        assert col is not None, (
            f"{model.__name__} has no claimed_at_generation property — the "
            f"migration adds the column but the ORM cannot see it"
        )
        assert col.nullable, f"{model.__name__}.claimed_at_generation must be nullable"
        assert col.server_default is None, (
            f"{model.__name__}.claimed_at_generation must have NO server_default "
            f"— 0 is a real generation"
        )
        assert isinstance(col.type, Integer), (
            f"{model.__name__}.claimed_at_generation must be Integer, got "
            f"{col.type!r}"
        )

    assert AttachmentJob.__table__.c.get("claimed_at_generation") is None, (
        "AttachmentJob must NOT declare claimed_at_generation — dead schema"
    )


def _cols(conn, table: str) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t"
            ),
            {"t": table},
        )
    }


def test_migration_0064_round_trips(pg_url, pg_session_factory):
    """0064 applies over a row that predates it, rolls back, and re-applies.

    `pg_session_factory` is requested SOLELY for its teardown. The testcontainer
    database is SESSION-scoped and shared by the whole suite; that fixture's
    teardown is the only thing that TRUNCATEs. Without it this test leaks a live
    `queued` workspace_setup_jobs row that a later test's claim_next_setup_job
    can pick up.

    The seeding order matters and is the point: downgrade FIRST, insert at 0063,
    then upgrade — so the row genuinely predates the column. Seeding at head and
    calling the row "pre-existing" would prove nothing about existing data.

    Every step after the first downgrade is wrapped so the shared database is
    restored to head even on failure. Stranding it at 0063 would leave the next
    test's TRUNCATE and every ORM query facing a schema the models do not match,
    surfacing as opaque unrelated failures. Same precedent and same reasoning as
    test_alembic_v2_round_trip.py's try/finally.
    """
    cfg = _cfg(pg_url)
    eng = create_engine(pg_url)
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0063")
        try:
            # Seed AT 0063 — before the column exists.
            with eng.begin() as c:
                c.execute(text("INSERT INTO users (email) VALUES ('mig64@umu.se')"))
                uid = c.execute(
                    text("SELECT id FROM users WHERE email='mig64@umu.se'")
                ).scalar_one()
                c.execute(
                    text(
                        "INSERT INTO workspaces (id, title, owner_id, status, "
                        "current_phase) VALUES (gen_random_uuid(), 'W64', :u, "
                        "'drafting', 'setup')"
                    ),
                    {"u": uid},
                )
                ws_id = c.execute(
                    text("SELECT id FROM workspaces WHERE title='W64'")
                ).scalar_one()
                c.execute(
                    text(
                        "INSERT INTO workspace_setup_jobs (id, workspace_id, status) "
                        "VALUES (gen_random_uuid(), :w, 'queued')"
                    ),
                    {"w": ws_id},
                )

            with eng.connect() as c:
                for table in FENCED_TABLES:
                    assert "claimed_at_generation" not in _cols(c, table), (
                        f"downgrade left claimed_at_generation on {table}"
                    )
        finally:
            command.upgrade(cfg, "head")

        with eng.connect() as c:
            for table in FENCED_TABLES:
                assert "claimed_at_generation" in _cols(c, table), (
                    f"re-upgrade did not restore claimed_at_generation on {table}"
                )
            # The row that predates the column survived, identified by ITS OWN id
            # — a global count would be hostage to any other row in the shared DB.
            got = c.execute(
                text(
                    "SELECT claimed_at_generation FROM workspace_setup_jobs "
                    "WHERE workspace_id = :w"
                ),
                {"w": ws_id},
            ).scalar_one()
        assert got is None, (
            f"a row that predates the column must read NULL (legacy, unfenced), "
            f"got {got!r} — a server_default would make this 0, which is a real "
            f"generation and would fence the row against a real epoch"
        )
    finally:
        eng.dispose()
