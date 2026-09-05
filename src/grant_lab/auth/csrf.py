"""Origin / Referer enforcement for state-changing requests.

This is a lightweight CSRF defense that complements the `SameSite=Lax`
session cookie set in `routes/auth.py`. The layering is:

1. `SameSite=Lax` blocks the session cookie on most cross-site POSTs in
   modern browsers — no cookie ⇒ `get_current_user` returns 401 and the
   request can't affect user state.
2. This check catches residual cases where `SameSite=Lax` doesn't apply
   (Firefox's "Lax + POST" 2-minute window after top-level navigation;
   environments that downgrade to `SameSite=None`; method-override abuse
   where a GET gets treated as a POST upstream).

Design choice: we verify `Origin` (preferred) or `Referer` (fallback),
and when neither header is present we ALLOW the request. Rationale:
* Modern browsers always send `Origin` on `POST/PUT/PATCH/DELETE` — a
  missing `Origin` on a cross-site POST only happens pre-2019 or with a
  deliberately broken user-agent.
* The TestClient, curl, server-to-server callers, and APScheduler
  jobs don't send either header; refusing these would turn this middleware
  into an availability footgun.
* An attacker who can strip `Origin` AND `Referer` from a victim's
  browser is already operating at a layer below what a middleware can
  defend against.

This is the same relaxation Django uses in its default CSRF middleware
and what OWASP recommends as a practical baseline.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from grant_lab.web.api.errors import envelope_dict, is_api_path

_logger = logging.getLogger(__name__)

# Safe HTTP methods per RFC 7231 §4.2.1. We never check CSRF on these
# — they mustn't have side effects in the first place.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _hostport(url: str) -> str | None:
    """Return a canonical `scheme://host[:port]` origin key, or None if
    unparseable/empty.

    Scheme IS part of the key: an `Origin: http://example.com` must NOT match a
    configured `https://example.com`, or a network MITM that downgrades to
    plaintext could forge a same-origin-looking request.

    Normalizes the bare-loopback aliases (`localhost`, `127.0.0.1`, `::1`) onto
    a single canonical host token. They route to the same host on every OS, so
    a dev box configured as `http://localhost:8000` should accept
    `Origin: http://127.0.0.1:8000` (and vice versa) — otherwise VSCode
    port-forwarding and equivalent dev tooling trip the middleware. Production
    URLs use real hostnames where this collapse has no effect.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
        # .port is parsed lazily and raises ValueError on a malformed port
        # (e.g. "http://host:notaport"). Access it INSIDE the try so a hostile/
        # garbled Origin yields a clean mismatch (403), not a 500.
        port = parsed.port
    except ValueError:
        return None
    # urlparse.hostname unwraps IPv6 brackets and lowercases the host.
    host = parsed.hostname
    if not host:
        return None
    if host in ("localhost", "127.0.0.1", "::1"):
        host = "localhost"
    scheme = (parsed.scheme or "").lower()
    # Drop the scheme's DEFAULT port so an explicit `:443` (https) / `:80` (http)
    # on one side matches an implicit default on the other — they are the same
    # origin per RFC 3986/6454. Browsers usually omit the default port, but some
    # proxies/clients send it explicitly; without this they would 403.
    if port is not None and port == {"http": 80, "https": 443, "ws": 80, "wss": 443}.get(scheme):
        port = None
    hostport = f"{host}:{port}" if port else host
    return f"{scheme}://{hostport}" if scheme else hostport


class OriginCSRFMiddleware(BaseHTTPMiddleware):
    """Reject state-changing requests whose `Origin` or `Referer` doesn't
    match the configured `app_base_url`."""

    def __init__(self, app: ASGIApp, *, expected_base_url: str):
        super().__init__(app)
        self._expected_host = _hostport(expected_base_url)

    async def dispatch(self, request: Request, call_next):
        if request.method in _SAFE_METHODS or not self._expected_host:
            return await call_next(request)

        origin = request.headers.get("origin")
        referer = request.headers.get("referer")

        # `Origin: null` happens for sandboxed iframes, `data:` URLs, and
        # certain redirect chains. Treat it as a mismatch — a legitimate
        # same-origin form won't produce it.
        if origin == "null":
            return _forbidden("origin", origin, path=request.url.path)

        if origin:
            if _hostport(origin) != self._expected_host:
                return _forbidden("origin", origin, path=request.url.path)
            return await call_next(request)

        if referer:
            if _hostport(referer) != self._expected_host:
                return _forbidden("referer", referer, path=request.url.path)
            return await call_next(request)

        # Neither Origin nor Referer present — allow. This is intentional:
        # browsers send at least one of these on cross-origin requests, so an
        # absent pair is characteristic of same-origin navigations, curl/API
        # clients with stripped headers, and health checks. Blocking it would
        # break legitimate non-browser callers. See module docstring for the
        # full threat model.
        return await call_next(request)


def _forbidden(header: str, value: str, *, path: str = "") -> Response:
    _logger.warning(
        "csrf.origin_mismatch",
        extra={"event": "csrf_origin_mismatch", "header": header, "value": value},
    )
    if is_api_path(path):
        body = json.dumps(envelope_dict("forbidden", "origin mismatch"))
        return Response(status_code=403, content=body, media_type="application/json")
    return Response(status_code=403, content=b"origin mismatch")
