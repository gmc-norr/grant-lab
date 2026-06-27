from __future__ import annotations

from fastapi import APIRouter

import call_manager.web.api.annotations as annotations_router
from call_manager.web.api import (
    admin,
    admin_golden_set,
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
router = APIRouter(
    prefix="/api",
    responses={code: {"model": ApiError} for code in STATUS_CODES},
)
router.include_router(auth.router)
router.include_router(me.router)
router.include_router(admin.router)
router.include_router(admin_users.router)
router.include_router(admin_golden_set.router)
router.include_router(annotations_router.router)
router.include_router(calls.router)
router.include_router(profiles.router)
router.include_router(workspaces.router)
router.include_router(turns.router)
router.include_router(references.router)
router.include_router(files.router)
router.include_router(settings.router)
router.include_router(rubric.router)
router.include_router(export.router)
router.include_router(citation_audit.router)
