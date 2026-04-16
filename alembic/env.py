import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from call_monitor.db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Prefer an explicit URL injected via `cfg.set_main_option("sqlalchemy.url", ...)`
# (this is how the test fixture in tests/conftest.py drives migrations against
# a testcontainers Postgres). Fall back to DATABASE_URL from the environment
# so `uv run alembic upgrade head` works from a shell.
database_url = config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL")
if not database_url:
    raise RuntimeError(
        "No database URL: set DATABASE_URL in the environment "
        "or inject via cfg.set_main_option('sqlalchemy.url', ...)"
    )
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
