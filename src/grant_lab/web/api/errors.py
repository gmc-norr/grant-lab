from __future__ import annotations

import math

from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request


class ApiErrorBody(BaseModel):
    """OpenAPI SCHEMA mirror of the inner envelope (see ``envelope_dict``).

    Schema-only — the runtime keeps emitting plain dicts via ``envelope_dict``;
    these models exist solely so the generated OpenAPI documents the real
    ``{error: {code, message, detail?}}`` shape (Plan 3 TS codegen).
    """

    code: str
    message: str
    detail: object | None = None


class ApiError(BaseModel):
    """OpenAPI SCHEMA mirror of the error envelope (see ``envelope_dict``)."""

    error: ApiErrorBody


def envelope_dict(code: str, message: str, detail: object | None = None) -> dict:
    """The single source of truth for the /api error-envelope SHAPE.

    Used by the FastAPI layer (api_error) AND the raw-ASGI layers
    (CSRF + body-size middleware), so the contract can't drift between them.
    """
    body: dict = {"code": code, "message": message}
    if detail is not None:
        body["detail"] = detail
    return {"error": body}


def json_safe_detail(value: object) -> object:
    """*value* with every non-finite float replaced by its name ("nan", "inf").

    `JSONResponse` serializes with `json.dumps(..., allow_nan=False)`, so a NaN
    or Infinity ANYWHERE inside an error body makes the dump raise inside the
    exception handler — and a handler that raises after the response has started
    degrades the 422 it was about to send into a 500 (`RuntimeError: Caught
    handled exception, but response already started`).

    The one error body that can carry arbitrary client floats is the
    `RequestValidationError` detail, because it echoes the rejected `input`.
    Measured 2026-10-28: `PUT /api/workspaces/{id}/rubric` with
    `{"criteria":[{… "weight": NaN …}]}` produced exactly that 500 the moment
    `CriterionIn.weight` started refusing non-finite values, so the refusal and
    this guard are one fix, not two. `jsonable_encoder` does not help: a float is
    already JSON-able as far as it is concerned.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)  # 'nan' | 'inf' | '-inf'
    if isinstance(value, dict):
        return {k: json_safe_detail(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe_detail(v) for v in value]
    return value


def is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def api_error(
    status_code: int,
    code: str,
    message: str,
    detail: object | None = None,
    headers: dict | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=envelope_dict(code, message, detail),
        headers=headers,
    )


STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "too_many_requests",
    500: "internal_error",
}


def code_for(status_code: int) -> str:
    return STATUS_CODES.get(status_code, "error")


def api_request(request: Request) -> bool:
    return is_api_path(request.url.path)
