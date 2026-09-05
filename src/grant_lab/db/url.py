"""The one place a database URL is resolved outside the app process.

Alembic's env.py used to read only `os.environ["DATABASE_URL"]`, while the app
reads `.env` through pydantic-settings — so README step 4 (`uv run alembic
upgrade head`) failed exactly as written, and the natural workaround put the
production password on the command line (review 2026-09-02 §2.6c / A9 F2).
"""

from __future__ import annotations

import os


def resolve_database_url(explicit: str | None = None) -> str:
    """`explicit` (a test fixture's injected URL) wins; then `DATABASE_URL` in the
    environment; then the app's own loader, `Settings().database_url`, which
    reads `.env` from the current directory. Raises RuntimeError naming all three
    when none yields a URL, so the operator is told what to set rather than
    where the code looked."""
    if explicit:
        return explicit
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    try:
        from grant_lab.config import Settings

        return Settings().database_url
    except Exception as exc:  # noqa: BLE001 — every failure means "not configured"
        raise RuntimeError(
            "No database URL: set DATABASE_URL in the environment, put it in .env "
            "(the app's own settings file, read from the current directory), or "
            "inject via cfg.set_main_option('sqlalchemy.url', ...)"
        ) from exc
