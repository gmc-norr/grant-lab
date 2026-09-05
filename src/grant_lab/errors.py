"""Errors and wire-visible error strings shared by the WHOLE application.

Top-level on purpose. The obvious home was `grant_lab/db/errors.py`, and it is
wrong: `grant_lab/db/__init__.py` imports every model to populate
`Base.metadata`, so ANY `grant_lab.db.*` import drags in SQLAlchemy — which
breaks the stdlib-only `ports/` fence (tests/proposal_assist/test_f3b_ports_boundary.py)
and, via ports/errors.py, closed an import cycle through proposal_assist.fencing.
`grant_lab/__init__.py` is empty, so this module costs an importer nothing.

`is_lock_timeout` lived in `proposal_assist/drafting/locking.py` until 2026-09-05.
Nothing about it is proposal-assist: it reads a Postgres SQLSTATE off a DBAPI
error. Its home mattered because `main.py`'s GENERIC OperationalError handler —
which both editions register — imported it, and that single import pulled the
whole proposal-assist package into a match process.
See tests/architecture/test_match_edition_process_cut.py.
"""

from __future__ import annotations


def is_lock_timeout(exc: BaseException) -> bool:
    """True iff *exc* is a Postgres lock_not_available (SQLSTATE 55P03) — a
    `SET LOCAL lock_timeout` that fired.

    Since Slice C3 the mutex-held tail keeps a 2s cap in force, so 55P03 is the
    routine "someone else holds a row I need" signal rather than a server fault.
    Every site that must tell contention from a real database failure calls this,
    so the SQLSTATE check exists exactly once.

    psycopg exposes the code as `sqlstate`; older drivers use `pgcode`. Anything
    that is not a DBAPI error at all (no `.orig`) is not a lock timeout.
    """
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate == "55P03"


#: Served with 429 + Retry-After: 2 when a lock timeout reaches the wire, and
#: returned verbatim by the proposal-assist tools. Here, beside is_lock_timeout,
#: for the same reason: main.py's GENERIC OperationalError handler answers with
#: this string in BOTH editions, and importing it from proposal_assist pulled that
#: package into a match process. `ports/errors.py` re-exports it, so every
#: proposal-assist import site is unchanged, and the wire bytes are untouched.
BUSY_ERROR_MESSAGE = "workspace is busy — try again in a moment"
