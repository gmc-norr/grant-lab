from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from call_manager.web.spa_routes import is_spa_route

SPA_DIR = Path(__file__).resolve().parent / "spa"  # src/call_manager/web/spa


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
        app.mount("/assets", StaticFiles(directory=assets), name="spa-assets")

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
        raise HTTPException(status_code=404, detail="not found")
