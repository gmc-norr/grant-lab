from __future__ import annotations

from typing import Literal

from fastapi import APIRouter

import call_manager.web.api.annotations as annotations_router
from call_manager.web.api import (
    admin,
    admin_golden_set,
    admin_retention,
    admin_users,
    auth,
    calls,
    citation_audit,
    export,
    files,
    me,
    profiles,
    references,
    rubric,
    settings,
    turns,
    workspaces,
)
from call_manager.web.api.errors import STATUS_CODES, ApiError

# Document the error-envelope shape on every status the /api surface actually
# emits, declared on the router so it applies to all ops. Without this, each op
# advertised FastAPI's stock HTTPValidationError ({detail: [...]}) for its 422,
# but the runtime returns {error: {code, message, detail?}} — which broke
# Plan 3's OpenAPI -> TS codegen. Schema-only: the runtime still emits the
# envelope via call_manager.web.api.errors.envelope_dict. The status set is
# DERIVED from STATUS_CODES (single source of truth) so the documented responses
# can't drift from the codes the handlers actually map.

# Mounted in BOTH editions. Call Match release 1 is these plus the non-/api
# health and OAuth routers.
_ALWAYS = (
    auth.router,
    me.router,
    admin.router,
    admin_users.router,
    admin_golden_set.router,
    admin_retention.router,
    calls.router,
    profiles.router,
)

# Mounted ONLY in the full edition. In a match build these routes are never
# registered, so a request for one gets FastAPI's real 404 — there is no handler
# to reach. Pinned by tests/architecture/test_edition_surface.py.
_FULL_ONLY = (
    annotations_router.router,
    workspaces.router,
    turns.router,
    references.router,
    files.router,
    rubric.router,
    export.router,
    citation_audit.router,
    # Despite the module name, every route in web/api/settings.py is
    # workspace-scoped (/workspaces/{id}/settings and /workspaces/{id}/members*)
    # — it is the proposal-assist workspace settings router, not app settings.
    # Leaving it in _ALWAYS leaked three /api/workspaces paths into a match build.
    settings.router,
)


def build_api_router(*, edition: Literal["match", "full"] = "full") -> APIRouter:
    """Compose the /api router for one edition. `edition` is Settings.app_edition."""
    r = APIRouter(
        prefix="/api",
        responses={code: {"model": ApiError} for code in STATUS_CODES},
    )
    for sub in _ALWAYS:
        r.include_router(sub)
    if edition != "match":
        for sub in _FULL_ONLY:
            r.include_router(sub)
    return r


# Module-level full-edition router. Kept so existing `from ... import router`
# call sites (and the OpenAPI codegen) keep working unchanged.
router = build_api_router(edition="full")
