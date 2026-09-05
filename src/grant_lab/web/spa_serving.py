from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Match

from grant_lab.web.spa_routes import is_spa_route

SPA_DIR = Path(__file__).resolve().parent / "spa"  # src/grant_lab/web/spa


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


def packaged_edition() -> str | None:
    """The edition recorded in the served bundle, or None when unknown.

    scripts/package_spa.sh writes SPA_DIR/edition.txt (`edition=match|full`,
    derived from dist/module-manifest.json — the bundle itself, not the caller's
    env). ABSENT MEANS UNKNOWN, NOT FULL: the CI/Docker path
    (.github/workflows/ci.yml:146-147) does its own `docker cp` and writes no
    edition.txt at all, so treating a missing file as a mismatch would refuse
    every existing deployment. A present-but-unparsable file is unknown too.
    """
    try:
        # ValueError alongside OSError: read_text() raises UnicodeDecodeError on a
        # non-UTF-8 file, and that is a ValueError — it escaped an `except OSError`
        # and failed boot, contradicting the "unparsable is unknown" promise above.
        #
        # NOT errors="replace": that decodes the garbage instead of raising, so the
        # value parses to something like "m\ufffdatch", which is not absent — it is
        # a KNOWN edition that disagrees, and the app refuses anyway. The test for
        # this caught that exact mistake.
        text = (SPA_DIR / "edition.txt").read_text()
    except (OSError, ValueError):
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "edition":
            # Only the two editions package_spa.sh writes count as known. Anything
            # else is a marker we cannot interpret, which is the same class as an
            # absent one — refusing on it would fail boot over a corrupt byte.
            return value.strip() if value.strip() in ("match", "full") else None
    return None


def mount_spa(app: FastAPI, *, spa_mode: str, edition: str) -> None:
    """Serve the built SPA when spa_mode == 'prod'. 'dev' is served by Vite;
    'disabled' mounts nothing. Must be called AFTER all API/HTML routers are
    included so real routes win and only unmatched paths reach the fallback.

    `edition` is Settings.app_edition, passed in rather than read from a global
    so this stays a pure function of its arguments (the tests build both
    directions without touching module state)."""
    if spa_mode != "prod":
        return
    index = SPA_DIR / "index.html"
    if not index.exists():
        raise RuntimeError(f"SPA build missing at {index} (SPA_MODE=prod)")

    # An APP_EDITION that disagrees with the bundle on disk is silent in every
    # other way: APP_EDITION=match served with a FULL bundle renders the full
    # router — cockpit links, the dark theme — against a backend where every
    # proposal-assist request 404s, and no gate anywhere notices. The bundle is
    # the only artefact that records which router was compiled in, so refuse to
    # start. Unknown (no edition.txt) proceeds — see packaged_edition().
    packaged = packaged_edition()
    if packaged is not None and packaged != edition:
        raise RuntimeError(
            f"SPA edition mismatch (SPA_MODE=prod): the packaged bundle at {SPA_DIR} "
            f"is edition={packaged!r} but APP_EDITION={edition!r}. Rebuild and "
            "package the matching edition (pnpm -C frontend build:match && "
            "pnpm -C frontend package:match for match; build + scripts/package_spa.sh "
            "for full), or change APP_EDITION to agree with the bundle."
        )
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
