from __future__ import annotations

from fastapi import APIRouter

from call_manager.web.api import calls, me
from call_manager.web.api.errors import ApiError

# Document the error-envelope shape on every status the /api surface actually
# emits, declared on the router so it applies to all ops. Without this, each op
# advertised FastAPI's stock HTTPValidationError ({detail: [...]}) for its 422,
# but the runtime returns {error: {code, message, detail?}} — which broke
# Plan 3's OpenAPI -> TS codegen. Schema-only: the runtime still emits the
# envelope via call_manager.web.api.errors.envelope_dict.
router = APIRouter(
    prefix="/api",
    responses={
        400: {"model": ApiError},
        401: {"model": ApiError},
        403: {"model": ApiError},
        404: {"model": ApiError},
        405: {"model": ApiError},
        409: {"model": ApiError},
        413: {"model": ApiError},
        422: {"model": ApiError},
        429: {"model": ApiError},
        500: {"model": ApiError},
    },
)
router.include_router(me.router)
router.include_router(calls.router)
