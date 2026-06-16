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

    @app.get("/{full_path:path}")
    def _spa_fallback(full_path: str, request: Request):  # noqa: ANN202
        path = "/" + full_path
        if is_spa_route(path):
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="not found")
