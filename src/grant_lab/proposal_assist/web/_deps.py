"""Neutral FastAPI dependency wiring shared by the JSON /api workspace modules.

This module has no presentation-layer coupling.
"""

from __future__ import annotations

from fastapi import Request

from grant_lab.db.session import get_db_dependency


def _session_dep(request: Request):
    factory = request.app.state.session_factory
    yield from get_db_dependency(factory)()
