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

    # SPA shell fallback for document requests, registered LAST so every real
    # route wins and only unmatched paths reach here. Serves index.html for
    # manifest-owned routes (is_spa_route), else a real 404. GET + HEAD only:
    # HEAD returns the same headers with an empty body (FileResponse strips it
    # automatically), so monitors / reverse proxies probing a servable page get
    # 200, not 405. A POST/PUT/etc. to an unmatched path still 405s — correct
    # (no handler), and for /api it flows through the Plan-1 JSON envelope.
    # include_in_schema=False keeps this internal serving mechanism out of
    # /openapi.json (it is not an API operation, and Plan 3 generates a TS
    # client from that schema).
    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def _spa_fallback(full_path: str):  # noqa: ANN202
        path = "/" + full_path
        if is_spa_route(path):
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="not found")
