import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from call_monitor.db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_database_url() -> str:
    """Prefer an explicit URL injected via ``cfg.set_main_option(...)`` (the
    test fixture path); fall back to ``DATABASE_URL`` env. Resolved lazily so
    metadata-only commands (``alembic heads``, ``history``, ``branches``)
    don't require a database URL — only commands that actually run migrations
    do.
    """
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "No database URL: set DATABASE_URL in the environment "
            "or inject via cfg.set_main_option('sqlalchemy.url', ...)"
        )
    return url


def run_migrations_offline() -> None:
    url = _resolve_database_url()
    config.set_main_option("sqlalchemy.url", url)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    config.set_main_option("sqlalchemy.url", _resolve_database_url())
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
