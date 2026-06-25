"""Neutral FastAPI dependency wiring shared by the JSON /api workspace modules.

``_session_dep`` historically lived in the (now-doomed) Jinja ``workspaces.py``.
It is relocated here so the kept JSON modules import it from a module that has
no template/Jinja coupling and survives the Jinja teardown (G2). Definition is
copied verbatim from ``proposal_assist/web/workspaces.py``.
"""

from __future__ import annotations

from fastapi import Request

from call_manager.db.session import get_db_dependency


def _session_dep(request: Request):
    factory = request.app.state.session_factory
    yield from get_db_dependency(factory)()
