from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Match

from call_manager.web.spa_routes import is_spa_route

SPA_DIR = Path(__file__).resolve().parent / "spa"  # src/call_manager/web/spa


class _ImmutableStaticFiles(StaticFiles):
    """StaticFiles that adds an immutable Cache-Control header to 200 responses.

    Vite hashes all filenames under /assets/ (e.g. main.Bx3kPq2F.js), so
    public, max-age=31536000, immutable is safe — a content change always
    produces a new URL. index.html is served separately (no immutable header)."""

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["cache-control"] = "public, max-age=31536000, immutable"
        return response


def mount_spa(app: FastAPI, *, spa_mode: str) -> None:
    """Serve the built SPA when spa_mode == 'prod'. 'dev' is served by Vite;
    'disabled' mounts nothing. Must be called AFTER all API/HTML routers are
    included so real routes win and only unmatched paths reach the fallback."""
    if spa_mode != "prod":
        return
    index = SPA_DIR / "index.html"
    if not index.exists():
        raise RuntimeError(f"SPA build missing at {index} (SPA_MODE=prod)")
    assets = SPA_DIR / "assets"
    if assets.exists():
        app.mount("/assets", _ImmutableStaticFiles(directory=assets), name="spa-assets")

    # SPA shell fallback, registered LAST so every real route wins and only
    # unmatched paths reach here. It owns ALL methods on a catch-all path so that
    # status is decided explicitly rather than by Starlette's path-matched-but-
    # method-mismatched 405: an UNKNOWN/typo path must be a real 404 for EVERY
    # method (matching SPA_MODE=disabled and the Plan-2 "unknown -> real 404"
    # contract; for /api it flows through the Plan-1 JSON envelope). A
    # manifest-owned SPA route serves the shell on GET/HEAD (HEAD gets the same
    # headers with an empty body — FileResponse strips it) and 405s other methods
    # (the page exists but only serves a document). include_in_schema=False keeps
    # this internal mechanism out of /openapi.json (Plan 3 generates a TS client
    # from that schema). If it owned only GET/HEAD, a POST to ANY unmatched path
    # would path-match and 405 with a misleading Allow header instead of 404.
    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    def _spa_fallback(full_path: str, request: Request):  # noqa: ANN202
        path = "/" + full_path
        if is_spa_route(path):
            if request.method in ("GET", "HEAD"):
                return FileResponse(index)
            raise HTTPException(status_code=405, headers={"Allow": "GET, HEAD"})
        # Non-SPA path. Distinguish a real route hit with the WRONG method (405)
        # from a genuinely unknown path (404): the catch-all owns ALL methods, so
        # without this a wrong-method request to a real route (e.g. POST /api/me,
        # which is GET-only) would be masked here as 404 instead of the native
        # 405 that SPA_MODE=dev/disabled return (no catch-all → Starlette's
        # method-mismatch 405). Collect the methods of any CONCRETE route whose
        # path matches (excluding THIS catch-all and method-less mounts).
        # Short-circuit: /api/* sub-paths that reach the catch-all are
        # genuinely unknown (real /api routes always win before us). Skip the
        # O(routes) walk — they can never be a 405 from here, only a 404.
        # Other backend prefixes (/healthz, /auth, /workspaces …) still need
        # the walk so that a real route hit with the wrong method returns 405.
        if path.startswith("/api/"):
            raise HTTPException(status_code=404, detail="not found")
        allowed: set[str] = set()
        probe = {"type": "http", "path": path, "method": "GET"}
        for route in app.routes:
            if getattr(route, "endpoint", None) is _spa_fallback:
                continue
            methods = getattr(route, "methods", None)
            if not methods:
                continue
            match, _ = route.matches(probe)
            if match is not Match.NONE:
                allowed.update(methods)
        if allowed:
            raise HTTPException(status_code=405, headers={"Allow": ", ".join(sorted(allowed))})
        raise HTTPException(status_code=404, detail="not found")
