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

from grant_lab.web.api.errors import envelope_dict, is_api_path


def build_csp(
    dev: bool = False,
    ws_origin: str | None = None,
    *,
    allow_same_origin_frame: bool = False,
    form_action_origin: str | None = None,
) -> str:
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

    ``ws_origin`` (non-dev only) adds a single, narrow same-origin WebSocket
    token — e.g. ``wss://grants.example.org`` — to ``connect-src``. It's
    required for the Yjs collaboration socket: ``connect-src 'self'`` is not
    reliably treated as covering ``ws:``/``wss:`` across browser engines.
    Ignored when ``dev=True``, whose broad ``ws:`` token already covers it.

    ``form_action_origin`` adds ONE origin to ``form-action`` -- the identity
    provider's, when Google sign-in is on. The login page POSTs a native form
    to /auth/google/start, which 303s to the provider, and Chromium applies the
    document's form-action to that redirect: with ``'self'`` alone it blocked
    Google sign-in outright (security review 2026-10-31 S7).
    """
    script = "script-src 'self'"
    connect = "connect-src 'self'"
    if ws_origin:
        # 3b-5d.D4: narrow same-origin allowance for the Yjs WebSocket
        # (connect-src 'self' does not reliably cover ws:/wss: across engines).
        # Exactly ONE origin, derived from APP_BASE_URL — never a bare scheme.
        connect = f"connect-src 'self' {ws_origin}"
    form_action = "form-action 'self'"
    if form_action_origin:
        form_action = f"form-action 'self' {form_action_origin}"
    if dev:
        script = "script-src 'self' 'unsafe-inline'"
        connect = "connect-src 'self' ws: http://localhost:5173"
    return "; ".join(
        [
            "default-src 'self'",
            script,
            # 'unsafe-inline' for styles is intentional: Mantine (the UI component
            # library) injects CSS-in-JS at runtime via style elements. Nonces or
            # hashes would require per-request CSP headers and a bundler plugin;
            # blocking inline styles would break the entire UI. This is lower risk
            # than 'unsafe-inline' on scripts (which is NOT present in prod).
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self'",
            connect,
            form_action,
            "base-uri 'self'",
            "frame-ancestors 'self'" if allow_same_origin_frame else "frame-ancestors 'none'",
            "object-src 'none'",
        ]
    )


# Single source of truth for the HSTS value, so SecurityHeadersMiddleware and
# the body-cap early-reject path can never drift. 1 year, include subdomains;
# no `preload` (a hard-to-reverse browser-preload-list commitment that's wrong
# for an internal/intranet hostname).
HSTS_VALUE = b"max-age=31536000; includeSubDomains"

# Restrictive Permissions-Policy: explicitly disable powerful browser features
# the app never uses, so an injected/compromised sub-resource can't request
# them. Static value, shipped in both dev and prod.
PERMISSIONS_POLICY_VALUE = (
    b"camera=(), microphone=(), geolocation=(), usb=(), serial=(), payment=(), interest-cohort=()"
)


def security_headers(*, csp: str, hsts: bool) -> list[tuple[bytes, bytes]]:
    """The canonical defense-in-depth header set, shared by
    SecurityHeadersMiddleware (every response) and MaxBodySizeMiddleware's
    early-reject path (413/400) so the two can never diverge.

    `csp` is the policy string computed by build_csp(); `hsts` mirrors
    cookie_secure (https) — HSTS is omitted over plain-HTTP dev.
    """
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
        (b"permissions-policy", PERMISSIONS_POLICY_VALUE),
        (b"content-security-policy", csp.encode("latin-1")),
    ]
    if hsts:
        # 1 year, include subdomains. Only emitted when the deployment is
        # https (cookie_secure) — pointless/harmful over plain-HTTP dev.
        headers.append((b"strict-transport-security", HSTS_VALUE))
    return headers


class SecurityHeadersMiddleware:
    """Attach defense-in-depth security headers to every HTTP response.

    Only adds a header if the handler didn't already set it (so a route that
    deliberately sets, e.g., its own ``X-Content-Type-Options`` on a file
    download is not double-stamped).
    """

    def __init__(self, app: ASGIApp, *, csp: str, hsts: bool) -> None:
        self.app = app
        self._headers = security_headers(csp=csp, hsts=hsts)

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

    def __init__(self, app: ASGIApp, *, max_bytes: int, csp: str, hsts: bool) -> None:
        self.app = app
        self.max_bytes = max_bytes
        # Precompute the canonical security-header set so early-reject (413/400)
        # responses — emitted BEFORE SecurityHeadersMiddleware runs in the
        # response direction (body-cap is the outermost layer) — carry the SAME
        # CSP/HSTS/Permissions-Policy/etc. as every other response. Reuses the
        # shared builder so the policy is never duplicated as a literal.
        self._security_headers = security_headers(csp=csp, hsts=hsts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        as_json = is_api_path(scope.get("path", ""))

        # Collect all Content-Length header values. RFC 7230 §3.3.2 permits
        # multiple identical values (proxies may add one) but prohibits
        # distinct values — an inconsistency is a request-smuggling signal.
        cl_values: list[int] = []
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    cl_values.append(int(value))
                except ValueError:
                    await _send_error(
                        send,
                        400,
                        "bad_request",
                        "malformed content-length",
                        as_json,
                        self._security_headers,
                    )
                    return

        if cl_values:
            if len(set(cl_values)) > 1:
                # Two or more distinct values → request smuggling attempt; reject.
                await _send_error(
                    send,
                    400,
                    "bad_request",
                    "conflicting content-length headers",
                    as_json,
                    self._security_headers,
                )
                return
            declared = cl_values[0]
            if declared > self.max_bytes:
                await _send_error(
                    send,
                    413,
                    "payload_too_large",
                    "request body too large",
                    as_json,
                    self._security_headers,
                )
                return

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


async def _send_error(
    send: Send,
    status: int,
    code: str,
    message: str,
    as_json: bool,
    security_headers: list[tuple[bytes, bytes]],
) -> None:
    if as_json:
        body = json.dumps(envelope_dict(code, message)).encode()
        ctype = b"application/json"
    else:
        body = message.encode()
        ctype = b"text/plain; charset=utf-8"
    # Stamp the FULL canonical security-header set (CSP, HSTS in prod,
    # Permissions-Policy, nosniff, frame-options, referrer-policy) on these
    # early-reject responses. They are emitted before SecurityHeadersMiddleware
    # runs in the response direction (body-cap is the outermost layer), so we
    # must add them manually — reusing the same set every other response carries
    # so the policy can never drift between the two paths.
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", ctype),
        (b"content-length", str(len(body)).encode("ascii")),
        *security_headers,
    ]
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": body})
