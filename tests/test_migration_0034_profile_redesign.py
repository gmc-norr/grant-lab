from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_0034_maps_kinds_and_adds_columns(pg_url):
    # NOTE: pg_url is the SHARED session container. This test mutates the schema
    # (downgrade→upgrade) and seeds rows, so the finally MUST (a) delete the
    # seeded rows and (b) leave the schema at head, or it poisons later tests
    # that assume a clean head schema (pg_session_factory truncates rows but not
    # schema version).
    cfg = _cfg(pg_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0033")  # back to the pre-redesign enum schema
    eng = create_engine(pg_url)
    try:
        # Seed legacy rows under the 0033 schema (profile_kind enum: org/group/user).
        with eng.begin() as c:
            c.execute(text("INSERT INTO users (email) VALUES ('m@ki.se')"))
            uid = c.execute(text("SELECT id FROM users WHERE email='m@ki.se'")).scalar_one()
            for nm, k in [("Org A", "org"), ("Grp B", "group"), ("Dr C", "user")]:
                c.execute(
                    text(
                        "INSERT INTO profiles (name, kind, description, structured, "
                        "is_active, created_by) VALUES (:n, :k, '', '{}'::jsonb, true, :u)"
                    ),
                    {"n": nm, "k": k, "u": uid},
                )
        command.upgrade(cfg, "head")  # apply 0034
        with eng.connect() as c:
            rows = dict(c.execute(text("SELECT name, kind FROM profiles ORDER BY name")).all())
            assert rows["Org A"] == "organizational_unit"
            assert rows["Grp B"] == "organizational_unit"
            assert rows["Dr C"] == "individual"
            levels = dict(c.execute(text("SELECT name, level FROM profiles ORDER BY name")).all())
            assert levels["Grp B"] == "group"
            assert levels["Org A"] is None  # owner fills in later
            assert levels["Dr C"] is None
            # parent_profile_id column exists and is nullable
            assert (
                c.execute(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_name='profiles' AND column_name='parent_profile_id'"
                    )
                ).scalar_one()
                == "YES"
            )
    finally:
        # Hermetic cleanup: drop the rows we seeded and guarantee head schema.
        with eng.begin() as c:
            c.execute(text("DELETE FROM profiles WHERE name IN ('Org A','Grp B','Dr C')"))
            c.execute(text("DELETE FROM users WHERE email='m@ki.se'"))
        eng.dispose()
        command.upgrade(cfg, "head")  # restore for subsequent tests


def test_0034_downgrade_runs_clean(pg_url):
    cfg = _cfg(pg_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0033")  # documented-lossy; must not error
    command.upgrade(cfg, "head")


def test_0034_downgrade_survives_unexpected_kind(pg_url):
    """A profile whose kind is outside the legacy enum domain (e.g. a future
    new-only kind) must NOT break the downgrade's `kind::profile_kind` cast —
    the guard remaps stragglers to 'group' first."""
    cfg = _cfg(pg_url)
    command.upgrade(cfg, "head")  # at-head schema: kind is Text + check
    eng = create_engine(pg_url)
    try:
        # Drop the head check so we can seed an out-of-domain kind/level that
        # neither the head constraint nor the legacy enum would accept. This
        # simulates a row written by a future migration that 0034's CASE
        # doesn't know how to map back.
        with eng.begin() as c:
            c.execute(text("INSERT INTO users (email) VALUES ('weird@ki.se')"))
            uid = c.execute(text("SELECT id FROM users WHERE email='weird@ki.se'")).scalar_one()
            c.execute(text("ALTER TABLE profiles DROP CONSTRAINT IF EXISTS ck_profiles_kind"))
            c.execute(text("ALTER TABLE profiles DROP CONSTRAINT IF EXISTS ck_profiles_level"))
            c.execute(
                text(
                    "INSERT INTO profiles (name, kind, level, description, structured, "
                    "is_active, created_by) "
                    "VALUES ('Weird', 'consortium', 'network', '', '{}'::jsonb, true, :u)"
                ),
                {"u": uid},
            )

        # The downgrade cast would fail on 'consortium' without the guard.
        command.downgrade(cfg, "0033")
        with eng.connect() as c:
            kind = c.execute(
                text("SELECT kind::text FROM profiles WHERE name='Weird'")
            ).scalar_one()
            assert kind == "group", "guard must collapse unknown kinds to 'group'"
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM profiles WHERE name='Weird'"))
            c.execute(text("DELETE FROM users WHERE email='weird@ki.se'"))
        eng.dispose()
        command.upgrade(cfg, "head")  # restore head for subsequent tests
