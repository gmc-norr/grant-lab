"""Round-trip + behavior checks for migration 0035.

0035 (a) adds ix_references_uploaded_by and (b) re-creates several
user-referencing FKs with explicit ON DELETE RESTRICT. Both upgrade and
downgrade resolve the (auto-generated) constraint names from the catalog, so
these tests verify the dynamic lookup actually finds and rewrites them.

NOTE: pg_url is the SHARED session container. These tests mutate the schema
(downgrade→upgrade), so each MUST leave the schema at head for later tests.
"""

from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command

_USER_FKS = [
    ("workspaces", "owner_id"),
    ("attachments", "uploaded_by"),
    ("phase_approvals", "approving_user_id"),
    ("turn_jobs", "submitted_by_user_id"),
    ("references", "uploaded_by"),
]


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def _index_exists(conn, name: str) -> bool:
    return (
        conn.execute(text("SELECT 1 FROM pg_indexes WHERE indexname = :n"), {"n": name}).scalar()
        is not None
    )


def _fk_delete_rule(conn, table: str, column: str) -> str | None:
    return conn.execute(
        text(
            """
            SELECT rc.delete_rule
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.referential_constraints AS rc
              ON tc.constraint_name = rc.constraint_name
            JOIN information_schema.constraint_column_usage AS ccu
              ON tc.constraint_name = ccu.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_name = :table
              AND kcu.column_name = :column
              AND ccu.table_name = 'users'
              AND ccu.column_name = 'id'
            LIMIT 1
            """
        ),
        {"table": table, "column": column},
    ).scalar()


def test_0035_round_trip_and_restrict(pg_url):
    cfg = _cfg(pg_url)
    command.upgrade(cfg, "head")
    eng = create_engine(pg_url)
    try:
        # At head: index exists and every user FK is explicit RESTRICT.
        with eng.connect() as c:
            assert _index_exists(c, "ix_references_uploaded_by")
            for table, column in _USER_FKS:
                assert _fk_delete_rule(c, table, column) == "RESTRICT", (table, column)

        # Downgrade to 0034: index gone, FKs back to NO ACTION.
        command.downgrade(cfg, "0034")
        with eng.connect() as c:
            assert not _index_exists(c, "ix_references_uploaded_by")
            for table, column in _USER_FKS:
                assert _fk_delete_rule(c, table, column) == "NO ACTION", (table, column)

        # Re-upgrade: back to RESTRICT + index, schema clean.
        command.upgrade(cfg, "head")
        with eng.connect() as c:
            assert _index_exists(c, "ix_references_uploaded_by")
            for table, column in _USER_FKS:
                assert _fk_delete_rule(c, table, column) == "RESTRICT", (table, column)
    finally:
        eng.dispose()
        command.upgrade(cfg, "head")  # restore head for subsequent tests


def test_0035_restrict_blocks_user_delete(pg_url):
    """A user owning a workspace cannot be hard-deleted under RESTRICT."""
    from sqlalchemy.exc import IntegrityError

    cfg = _cfg(pg_url)
    command.upgrade(cfg, "head")
    eng = create_engine(pg_url)
    try:
        with eng.begin() as c:
            c.execute(text("INSERT INTO users (email) VALUES ('owner@umu.se')"))
            uid = c.execute(text("SELECT id FROM users WHERE email='owner@umu.se'")).scalar_one()
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
                c.execute(text("DELETE FROM users WHERE email='owner@umu.se'"))
        except IntegrityError:
            raised = True
        assert raised, "RESTRICT must forbid deleting a user who owns a workspace"
    finally:
        # Cleanup: drop the workspace then the user.
        with eng.begin() as c:
            c.execute(
                text(
                    "DELETE FROM workspaces WHERE owner_id IN "
                    "(SELECT id FROM users WHERE email='owner@umu.se')"
                )
            )
            c.execute(text("DELETE FROM users WHERE email='owner@umu.se'"))
        eng.dispose()
