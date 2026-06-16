from __future__ import annotations

from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request


class ApiErrorBody(BaseModel):
    code: str
    message: str
    detail: object | None = None


class ApiError(BaseModel):
    error: ApiErrorBody


def is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def api_error(
    status_code: int, code: str, message: str, detail: object | None = None
) -> JSONResponse:
    body = ApiError(error=ApiErrorBody(code=code, message=message, detail=detail))
    return JSONResponse(status_code=status_code, content=body.model_dump(exclude_none=True))


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
