"""Errors and wire-visible error strings shared by the whole application.

Top-level on purpose: `grant_lab/db/__init__.py` imports every model, so a
`grant_lab.db.*` home would drag SQLAlchemy into every importer.
"""

from __future__ import annotations


def is_lock_timeout(exc: BaseException) -> bool:
    """True iff *exc* is a Postgres lock_not_available (SQLSTATE 55P03) — a
    `SET LOCAL lock_timeout` that fired.

    55P03 is routine contention, not a server fault. Every site that must tell
    contention from a real database failure calls this, so the SQLSTATE check
    exists exactly once.

    psycopg exposes the code as `sqlstate`; older drivers use `pgcode`. Anything
    that is not a DBAPI error at all (no `.orig`) is not a lock timeout.
    """
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate == "55P03"


#: Served with 429 + Retry-After: 2 when a lock timeout reaches the wire (main.py's
#: OperationalError handler). The wording is wire-visible, so it is kept byte for byte.
BUSY_ERROR_MESSAGE = "workspace is busy — try again in a moment"
