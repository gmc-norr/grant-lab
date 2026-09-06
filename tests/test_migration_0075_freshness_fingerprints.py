"""Migration 0075: the two freshness-fingerprint columns."""

from __future__ import annotations

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from grant_lab.db.models.profiles import Profile

# A deterministic 1024-dim vector (matches profiles.embedding's Vector(1024)).
# Seeded at 0074 so migration 0075 runs against a profile row that already
# holds a vector -- the exact shape a paired-NULL CHECK would choke on.
_SEED_VECTOR = "[" + ",".join(["0.125"] * 1024) + "]"


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_both_fingerprint_columns_exist(migrated_engine):
    insp = inspect(migrated_engine)
    assert "embedding_fingerprint" in {c["name"] for c in insp.get_columns("profiles")}
    assert "profile_fingerprint" in {c["name"] for c in insp.get_columns("call_rankings")}


def test_no_paired_null_check_on_embedding_fingerprint(migrated_engine):
    """Forward pin ONLY -- this is NOT the gate for the asymmetry.

    Deliberate asymmetry: profiles are re-embedded, rankings are not
    re-ranked. A paired-NULL CHECK on embedding_fingerprint would look
    symmetrical with ck_profiles_embedding_model_paired and would FAIL
    migration 0075 on any pre-existing profile row.

    This assertion was already green before 0075 was written -- it asserts a
    constraint that has never existed still does not exist -- so it cannot go
    red for the right reason on its own. The real gate is
    `test_0075_grandfathers_rankings_and_keeps_profile_vectors`, which runs
    0075 against a seeded profile row that holds a vector: adding the
    constraint by any route makes that migration itself fail.

    Two cheap pins are kept here because they are nearly free: the live DB
    (`compare_metadata` in test_schema_drift.py does not compare CHECK
    constraints -- see test_migration_0072_boards.py:5) and the ORM's
    __table_args__, which is where the mistake would most plausibly be made
    and which the live-DB check alone would not see until someone wrote the
    migration for it.
    """
    live = {c["name"] for c in inspect(migrated_engine).get_check_constraints("profiles")}
    assert "ck_profiles_embedding_fingerprint_paired" not in live
    assert "ck_profiles_embedding_model_paired" in live

    orm = {c.name for c in Profile.__table__.constraints}
    assert "ck_profiles_embedding_fingerprint_paired" not in orm


def test_0075_grandfathers_rankings_and_keeps_profile_vectors(migrated_engine, pg_url):
    """The data gate: seed a ranking AND an embedded profile AT 0074, then upgrade.

    `migrated_engine` is requested but unused in the body ON PURPOSE: it is
    what forces `upgrade head` to have run before this test's own
    `downgrade 0074`. Do not delete it -- without it the shared container's
    revision at entry is whatever the previous test file left behind.

    Two claims, one alembic drive (a second downgrade/upgrade cycle is slow
    and re-opens the risk of stranding the shared session-scoped container
    below head):

    1. Rankings written before 0075 are stamped 'legacy'.
    2. A profile that already holds a vector keeps it, and gets a NULL
       `embedding_fingerprint`. That is the asymmetry's real gate: if a
       paired-NULL CHECK on embedding_fingerprint is ever added by any route,
       `command.upgrade(..., "head")` below fails on this seeded row.

    `finally` restores head unconditionally -- without it, a failure here
    leaves the shared container one revision behind and every later test
    file on this xdist worker fails on a missing column.
    """
    engine = create_engine(pg_url)
    try:
        command.downgrade(_cfg(pg_url), "0074")
        with engine.begin() as conn:
            assert "profile_fingerprint" not in {
                c["name"] for c in inspect(conn).get_columns("call_rankings")
            }
            assert "embedding_fingerprint" not in {
                c["name"] for c in inspect(conn).get_columns("profiles")
            }
            src = conn.execute(
                text("INSERT INTO sources (name, slug) VALUES ('S0075','s0075') RETURNING id")
            ).scalar_one()
            uid = conn.execute(
                text(
                    "INSERT INTO users (email, status) "
                    "VALUES ('m0075@umu.se','active') RETURNING id"
                )
            ).scalar_one()
            # Non-NULL embedding + embedding_model: both are required by
            # ck_profiles_embedding_model_paired, and the vector is the thing
            # 0075 must leave untouched.
            pid = conn.execute(
                text(
                    "INSERT INTO profiles "
                    "(name, kind, description, structured, created_by, "
                    " embedding, embedding_model) "
                    "VALUES ('M0075','organizational_unit','d','{}'::jsonb, :u, "
                    " CAST(:v AS vector), 'bge-m3') RETURNING id"
                ),
                {"u": uid, "v": _SEED_VECTOR},
            ).scalar_one()
            vector_before = conn.execute(
                text("SELECT embedding::text FROM profiles WHERE id=:p"),
                {"p": pid},
            ).scalar_one()
            assert vector_before is not None
            cid = conn.execute(
                text(
                    "INSERT INTO calls (source_id, external_id, title, url) "
                    "VALUES (:s,'M0075','t','https://example.org/m0075') RETURNING id"
                ),
                {"s": src},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO call_rankings "
                    "(call_id, profile_id, aggregate_score, model, prompt_version, "
                    " raw_response) "
                    "VALUES (:c,:p,50,'gemma','v1','{}'::jsonb)"
                ),
                {"c": cid, "p": pid},
            )

        # This is the gate. A paired-NULL CHECK on embedding_fingerprint --
        # added in the migration, or in the ORM and then autogenerated -- makes
        # this line raise on the profile row seeded above.
        command.upgrade(_cfg(pg_url), "head")
        with engine.connect() as conn:
            got = conn.execute(
                text(
                    "SELECT profile_fingerprint FROM call_rankings "
                    "WHERE call_id=:c AND profile_id=:p"
                ),
                {"c": cid, "p": pid},
            ).scalar_one()
            vector_after, fingerprint_after = conn.execute(
                text("SELECT embedding::text, embedding_fingerprint FROM profiles WHERE id=:p"),
                {"p": pid},
            ).one()
        assert got == "legacy", f"pre-0075 ranking was not grandfathered: {got!r}"
        assert vector_after == vector_before, "0075 did not leave the profile's vector alone"
        assert fingerprint_after is None, (
            "an existing profile must come out of 0075 with a NULL fingerprint "
            f"(re-embedded on the first tick), got {fingerprint_after!r}"
        )
    finally:
        try:
            command.upgrade(_cfg(pg_url), "head")
            with engine.begin() as conn:
                # Scoped to the rows this file seeds: the container is shared
                # session-wide, so an unscoped DELETE would wipe another
                # file's rows on this xdist worker.
                conn.execute(
                    text(
                        "DELETE FROM call_rankings WHERE profile_id IN "
                        "(SELECT id FROM profiles WHERE name='M0075')"
                    )
                )
                conn.execute(text("DELETE FROM calls WHERE external_id='M0075'"))
                conn.execute(text("DELETE FROM profiles WHERE name='M0075'"))
                conn.execute(text("DELETE FROM users WHERE email='m0075@umu.se'"))
                conn.execute(text("DELETE FROM sources WHERE slug='s0075'"))
        finally:
            # Last, and unskippable: a raise in the cleanup above must not leak
            # a connection. pyproject.toml turns ResourceWarning into an error,
            # which would surface inside an unrelated test.
            engine.dispose()
