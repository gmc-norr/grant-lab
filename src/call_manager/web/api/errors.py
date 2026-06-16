from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.requests import Request


def envelope_dict(code: str, message: str, detail: object | None = None) -> dict:
    """The single source of truth for the /api error-envelope SHAPE.

    Used by the FastAPI layer (api_error) AND the raw-ASGI layers
    (CSRF + body-size middleware), so the contract can't drift between them.
    """
    body: dict = {"code": code, "message": message}
    if detail is not None:
        body["detail"] = detail
    return {"error": body}


def is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def api_error(
    status_code: int, code: str, message: str, detail: object | None = None
) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=envelope_dict(code, message, detail))


STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    500: "internal_error",
}


def code_for(status_code: int) -> str:
    return STATUS_CODES.get(status_code, "error")


def api_request(request: Request) -> bool:
    return is_api_path(request.url.path)
