from __future__ import annotations

from fastapi import APIRouter

from grant_lab.web.api import (
    admin,
    admin_golden_set,
    admin_users,
    auth,
    boards,
    calls,
    me,
    profiles,
    public_boards,
)
from grant_lab.web.api.errors import STATUS_CODES, ApiError

# Document the error-envelope shape on every status the /api surface actually
# emits, declared on the router so it applies to all ops. Without this, each op
# advertised FastAPI's stock HTTPValidationError ({detail: [...]}) for its 422,
# but the runtime returns {error: {code, message, detail?}} — which broke
# Plan 3's OpenAPI -> TS codegen. Schema-only: the runtime still emits the
# envelope via grant_lab.web.api.errors.envelope_dict. The status set is
# DERIVED from STATUS_CODES (single source of truth) so the documented responses
# can't drift from the codes the handlers actually map.

# Every /api router, in mount order (which is also the OpenAPI order and the
# route-matching precedence). Pinned by NAME in
# tests/architecture/test_route_surface.py: adding one is a product decision.
_ROUTERS = (
    auth.router,
    me.router,
    admin.router,
    admin_users.router,
    calls.router,
    profiles.router,
    boards.router,
    public_boards.router,
    admin_golden_set.router,
)


def build_api_router() -> APIRouter:
    """Compose the /api router."""
    r = APIRouter(
        prefix="/api",
        responses={code: {"model": ApiError} for code in STATUS_CODES},
    )
    for sub in _ROUTERS:
        r.include_router(sub)
    return r
