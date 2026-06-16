from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
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

    # GET-only catch-all: the SPA shell is always a GET document request. A
    # non-GET / HEAD on an unmatched path therefore returns 405 (not 404) in
    # prod — accepted: the path still has no real handler, the /api envelope
    # still wraps it (method_not_allowed), and API clients treat 404/405 on an
    # unknown endpoint equivalently. Registered LAST so real routes win.
    @app.get("/{full_path:path}")
    def _spa_fallback(full_path: str):  # noqa: ANN202
        path = "/" + full_path
        if is_spa_route(path):
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="not found")
