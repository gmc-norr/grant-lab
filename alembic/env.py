from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from grant_lab.db.base import Base
from grant_lab.db.url import resolve_database_url

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False is critical: the default (True) sets
    # `disabled=True` on every logger NOT named in the [loggers] section of
    # alembic.ini — silently turning off our app's own loggers (e.g.
    # `grant_lab.auth.email`) for the rest of the process. The damage
    # surfaces in tests as "the message was logged but caplog saw nothing"
    # because `Logger.disabled=True` short-circuits before any handler runs.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _resolve_database_url() -> str:
    """Prefer an explicit URL injected via ``cfg.set_main_option(...)`` (the
    test fixture path); then ``DATABASE_URL`` in the environment; then the
    app's own ``.env`` via ``Settings()`` (grant_lab.db.url), so the README's
    ``uv run alembic upgrade head`` works with nothing exported. Resolved lazily
    so metadata-only commands (``alembic heads``, ``history``, ``branches``)
    don't require a database URL — only commands that actually run migrations
    do.
    """
    return resolve_database_url(config.get_main_option("sqlalchemy.url"))


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
