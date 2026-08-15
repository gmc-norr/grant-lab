"""Slice C3 — the mutex-held tail is bounded, and its timeout is a 429.

`acquire_commit_mutex` used to restore `lock_timeout` to DEFAULT (= disabled)
after winning the mutex, so every lock wait afterwards was unbounded WHILE the
transaction held the mutex every other writer for that workspace needs. This
file holds the gates for the fix.
"""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy.exc import OperationalError

from call_manager.proposal_assist.drafting.locking import BUSY_ERROR_MESSAGE


class _Orig(Exception):
    """Stands in for psycopg's error object, which carries the SQLSTATE the
    handler keys on. The REAL 55P03 path is covered by the DB-backed gates
    below; this stub is only for the mapping itself."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__("canceling statement due to lock timeout")
        self.sqlstate = sqlstate


def _probe_app(app: FastAPI, sqlstate: str) -> None:
    @app.get("/api/_c3_probe")
    def _probe():  # pragma: no cover - body is the raise
        raise OperationalError("SELECT 1", {}, _Orig(sqlstate))


def test_a_lock_timeout_becomes_the_same_429_envelope_the_routes_emit(_app, client_authed_owner):
    """Byte-parity with the routes' own 429: code `too_many_requests`, the shared
    busy message, and Retry-After: 2."""
    _probe_app(_app, "55P03")
    resp = client_authed_owner.get("/api/_c3_probe")
    assert resp.status_code == 429, resp.text
    assert resp.headers.get("Retry-After") == "2"
    body = resp.json()
    assert body["error"]["code"] == "too_many_requests"
    assert body["error"]["message"] == BUSY_ERROR_MESSAGE


def test_a_non_lock_OperationalError_is_still_a_500(_app, client_authed_owner):
    """The handler must not swallow real database failures. 08006 is
    connection_failure — nothing to do with contention."""
    _probe_app(_app, "08006")
    resp = client_authed_owner.get("/api/_c3_probe")
    assert resp.status_code == 500, resp.text
    assert resp.json()["error"]["code"] == "internal_error"
