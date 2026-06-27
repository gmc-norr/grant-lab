"""Pure-ASGI hardening middleware.

Both classes are written as raw ASGI middleware (not Starlette's
``BaseHTTPMiddleware``) on purpose: ``BaseHTTPMiddleware`` buffers the
response body, which breaks the SSE turn-stream endpoint
(``/workspaces/.../turns/.../stream``). Raw ASGI lets us touch only the
response *start* (headers) and the request *receive* stream without
materializing bodies.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from call_manager.web.api.errors import envelope_dict, is_api_path


def build_csp(dev: bool = False) -> str:
    """Content-Security-Policy for the dashboard.

    ``script-src 'self'`` (no ``'unsafe-inline'``) is the high-value line —
    it's the safety net if any XSS sink is ever introduced. The React SPA uses
    no inline scripts (Vite bundles everything). ``frame-ancestors 'none'``
    is the modern clickjacking control (paired with ``X-Frame-Options`` for
    older browsers).

    ``dev=True`` (only when ``SPA_MODE=dev``) relaxes two directives so the Vite
    dev server works: the SPA shell carries an inline bootstrap ``<script>`` and
    opens an HMR WebSocket to localhost:5173. This is a DEV-ONLY loosening —
    ``prod`` and ``disabled`` keep the strict ``script-src 'self'`` /
    ``connect-src 'self'`` policy (asserted by tests). Self-hosted, no-CDN,
    no-``unsafe-inline``-in-prod is itself a supply-chain control.
    """
    script = "script-src 'self'"
    connect = "connect-src 'self'"
    if dev:
        script = "script-src 'self' 'unsafe-inline'"
        connect = "connect-src 'self' ws: http://localhost:5173"
    return "; ".join(
        [
            "default-src 'self'",
            script,
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self'",
            connect,
            "form-action 'self'",
            "base-uri 'self'",
            "frame-ancestors 'none'",
            "object-src 'none'",
        ]
    )


class SecurityHeadersMiddleware:
    """Attach defense-in-depth security headers to every HTTP response.

    Only adds a header if the handler didn't already set it (so a route that
    deliberately sets, e.g., its own ``X-Content-Type-Options`` on a file
    download is not double-stamped).
    """

    def __init__(self, app: ASGIApp, *, csp: str, hsts: bool) -> None:
        self.app = app
        headers: list[tuple[bytes, bytes]] = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            # `same-origin`, NOT `no-referrer`: per the Fetch spec, a
            # no-referrer policy makes browsers serialize the Origin header
            # as `null` on form POSTs, which OriginCSRFMiddleware then 403s
            # — every browser form submission in the app would fail. With
            # `same-origin`, nothing leaks to external sites and Origin is
            # sent normally, keeping the CSRF check effective.
            (b"referrer-policy", b"same-origin"),
            (b"content-security-policy", csp.encode("latin-1")),
        ]
        if hsts:
            # 1 year, include subdomains. Only emitted when the deployment is
            # https (cookie_secure) — pointless/harmful over plain-HTTP dev.
            headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
        self._headers = headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {name.lower() for name, _ in headers}
                for name, value in self._headers:
                    if name not in present:
                        headers.append((name, value))
            await send(message)

        await self.app(scope, receive, send_wrapper)


class MaxBodySizeMiddleware:
    """Reject request bodies larger than ``max_bytes``.

    Primary defense is the fast ``Content-Length`` check — browser form and
    multipart uploads always declare it, so an oversized body is rejected
    (413) before any of it is read into memory. The streaming counter is a
    best-effort backstop for chunked / length-spoofed bodies: it stops
    accumulating once the cap is hit. In production a reverse-proxy
    ``client_max_body_size`` should be the first line; this is the app-level
    safety net.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        as_json = is_api_path(scope.get("path", ""))

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    await _send_error(send, 400, "bad_request", "malformed content-length", as_json)
                    return
                if declared > self.max_bytes:
                    await _send_error(
                        send, 413, "payload_too_large", "request body too large", as_json
                    )
                    return
                break

        total = 0

        async def limited_receive() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    # Stop feeding the app more body and signal disconnect so it
                    # unwinds; memory stays bounded by max_bytes.
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, limited_receive, send)


async def _send_error(send: Send, status: int, code: str, message: str, as_json: bool) -> None:
    if as_json:
        body = json.dumps(envelope_dict(code, message)).encode()
        ctype = b"application/json"
    else:
        body = message.encode()
        ctype = b"text/plain; charset=utf-8"
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", ctype),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
