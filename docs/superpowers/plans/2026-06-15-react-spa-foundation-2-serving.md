# React SPA Foundation — Plan 2: SPA Serving + Vite Scaffold + Route Manifest + `SPA_MODE`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the React/Vite build and wire FastAPI to serve it — with an explicit `SPA_MODE`, a route manifest as the single source of truth for SPA-vs-backend routing, a dev proxy where Vite is the sole front door (unknown paths still reach FastAPI's real 404), a dev CSP relaxation so Vite HMR works, and a packaged prod build that fails fast if missing. Validates end-to-end via a temporary `/app` SPA mount; the live Jinja dashboard at `/` is untouched until Plan 3.

**Architecture:** One Python backend (no Node/SSR). `SPA_MODE = dev | prod | disabled`: `disabled` (tests/back-end-only) mounts nothing; `prod` mounts the built assets + a manifest-gated `index.html` fallback and fails fast if the build is absent; `dev` leaves serving to Vite (which proxies everything to uvicorn by default and carves out SPA routes + its own dev assets) and relaxes the CSP. A checked-in `routes.json` + a generator produce a Python module and a TS module so the same ownership table drives Python serving, the Vite proxy, and (Plan 3) React Router.

**Tech Stack:** Vite + React + TypeScript, **pnpm** (frozen lockfile, exact pins, scripts off + `minimumReleaseAge` cooldown); FastAPI `StaticFiles`/`FileResponse`; pydantic-settings; hatch packaging built in an **ephemeral secretless container** (`uv build --wheel`).

**Depends on:** Plan 1 (the `/api` router + `is_api_path`). **Followed by:** Plan 3 (the React app; flips `/` to the SPA and retires `/app`).

---

## Rev 2 — review fixes (apply these; from the multi-agent plan review)

- **[HIGH] `is_spa_route` must be an EXACT/PATTERN match, never a prefix.** The original body used `path.startswith(...)` against `"/"`, which matches every path → any typo (`/made-up-page`) would be served `index.html` instead of FastAPI's real 404. **Folded into Task 2 body (Rev 4):** the generated `is_spa_route` matches only manifest SPA-owned page routes exactly (plus React-Router sub-paths beneath an owned route); `BACKEND_PREFIXES`/`is_backend_route` exist ONLY to drive the Task-5 dev-proxy carve-out, not to define ownership. An unknown non-SPA path returns False → real 404. (Earlier drafts of this note proposed `is_spa_route = (not is_backend_route) and …`, i.e. "True for every non-backend path" — that is the SAME prefix-everything bug and is **superseded** by the exact-match body in Task 2.)
- **[HIGH] Task 5 dev proxy = default-to-backend, carve out the SPA.** The original body proxied "only `BACKEND_PREFIXES` to uvicorn, everything else stays with Vite", which leaves typo/unknown paths on Vite's HTML 404. **Folded into Task 5 body (Rev 4):** Vite forwards EVERYTHING to uvicorn by default and carves out only (a) SPA page routes (→ `index.html`, via a manifest-driven `spaIndexFallback` plugin) and (b) Vite's own dev assets. Unknown path in dev → uvicorn → FastAPI real 404. (The earlier "proxy each backend prefix, else Vite" snippet is **superseded**.)
- **[MED] Task 3 prod-serving test must not mutate the real tree.** Make `mount_spa`'s dir injectable (param or `settings`) and point the test at `tmp_path`, or `monkeypatch.setattr(spa_serving, "SPA_DIR", tmp_path)`. Don't write into `src/call_manager/web/spa/`.
- **[MED] Task 6 use `uv build --wheel`** (not `python -m hatchling build` — hatchling isn't a runnable module in the venv), then the `unzip -l dist/*.whl | grep ...` check.
- **[MED] All `uvicorn ... --factory` recipes must set required env** (`DATABASE_URL`, `SESSION_SECRET`, `ALLOWED_EMAIL_DOMAINS` — required by `config.py`), or the app won't start.

## Rev 3 — supply-chain hardening: FRONTEND + BUILD (per the 2026-06-16 supply-chain spec)

The [supply-chain spec](../specs/2026-06-16-supply-chain-security-design.md) **overrides** this plan's npm + on-host-build decisions. Authoritative detail: that spec §3.1 / §4 / §6 / §8. Apply:

- **pnpm, not npm (override).** Task 4: use **pnpm** — pin via `package.json` `packageManager` + corepack; commit a `frontend/.npmrc` (or the pnpm settings key) with: lifecycle **scripts off** (pnpm v10 default; minimal reviewed `onlyBuiltDependencies` allowlist), **`minimumReleaseAge` ~7–14 days** (e.g. `10080`), **`save-exact` / `save-prefix=''`** (exact pins, no `^`/`~`), installs via **`pnpm install --frozen-lockfile`**. Commit **`pnpm-lock.yaml`** (not `package-lock.json`). Replace every `npm`/`npm ci`/`npm run` (here and in Plan 3) with pnpm equivalents.
- **Containerized, secretless build (override of the on-host build).** Task 6: the frontend build runs in an **ephemeral, non-root, read-only-rootfs, egress-restricted (registries only) container** (new `Dockerfile`) that gets source + lockfiles but **no app env, no `var/`, no DB creds, no internal-network reach**, and emits **only** `src/call_manager/web/spa/`; base image pinned **by digest**; pnpm pinned via `packageManager`/corepack; `--frozen-lockfile`, scripts off. Then `uv build --wheel` packages it (hatch `artifacts`); assert `…/web/spa/index.html` before (re)start; wire into the README deploy sequence. (CI-only artifact build is the stronger future step, spec §10.)
- **JS detection:** add the `npm` ecosystem entry to `.github/dependabot.yml`; CI runs **Socket CLI** (`@socketsecurity/cli`), **OSV-Scanner** over `pnpm-lock.yaml`, and `pnpm audit`.
- **CSP (reaffirmed):** `script-src 'self'` hashed/no-inline + self-host all assets (no CDN) + `connect-src 'self'` — now also a supply-chain control; keep the CI `script-src`-no-`unsafe-inline` assertion.
- **CI (§7):** pnpm frozen + scripts-off install; `uv sync --frozen`; the detection steps; build ordering (container build → `uv build`).

## Rev 4 — task-body reconciliation (Codex)

Rev 2/3 above are correct decisions but were left as header notes while the canonical task bodies still scaffolded the stale (pre-fix) implementation. This rev **folds the fixes INTO the task bodies** so an implementation agent that follows the checkbox steps builds the correct version. Specifically: Task 2's generator/tests now do **exact/pattern** SPA matching (no `startswith("/")`; unknown → real 404) with `BACKEND_PREFIXES` only for the proxy carve-out; Task 5's dev proxy is now **default-to-backend with a manifest-driven SPA/dev-asset carve-out** (not "proxy only `BACKEND_PREFIXES`") plus the three dev-proxy tests; Task 4 scaffolds **pnpm** (frozen lockfile, exact pins, `.npmrc` cooldown/scripts-off) not npm/`package-lock.json`/carets; Task 6 builds in the **ephemeral secretless container** + `uv build --wheel` (not host `npm ci`/`python -m hatchling`) and adds JS scanners. **The task bodies are authoritative; these rev notes are history.**

## File structure

- Modify `src/call_manager/config.py` — add `spa_mode` to `Settings`.
- Create `frontend/routes.json` — the route-ownership source of truth.
- Create `scripts/gen_routes.py` — generator → Python + TS artifacts.
- Create `src/call_manager/web/spa_routes.py` — **generated**; `SPA_ROUTES`, `BACKEND_PREFIXES`, `is_spa_route()` (exact match), `is_backend_route()` (proxy carve-out).
- Create `frontend/src/routes.gen.ts` — **generated**; `SPA_ROUTES` + `BACKEND_PREFIXES`; consumed by `vite.config.ts` (+ React Router in Plan 3).
- Create `src/call_manager/web/spa_serving.py` — `mount_spa(app, settings)`.
- Modify `src/call_manager/main.py` — call `mount_spa(...)`; relax CSP when `spa_mode == "dev"`.
- Modify `src/call_manager/web/middleware.py` — `build_csp(dev: bool=False)` dev variant.
- Create `frontend/` scaffold: `package.json`, `.npmrc` (pnpm config), `pnpm-lock.yaml`, `Dockerfile` (secretless build), `vite.config.ts`, `tsconfig.json`, `index.html`, `src/main.tsx`, `src/App.tsx`.
- Modify `pyproject.toml` — hatch `artifacts` for the built SPA.
- Modify `README.md` — containerized-build deploy sequence; `.github/dependabot.yml` — `npm` ecosystem; `.github/workflows/ci.yml` — pnpm frozen install + JS scanners + container build → `uv build`.
- Modify `.gitignore` — ignore `src/call_manager/web/spa/` and `frontend/node_modules`, `frontend/dist`.
- Tests: `tests/test_spa_serving.py`, `tests/test_gen_routes.py`.

---

## Task 1: `SPA_MODE` setting

**Files:** Modify `src/call_manager/config.py` (the `Settings` class); Test: `tests/test_spa_serving.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_spa_serving.py
from call_manager.config import Settings


def test_spa_mode_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv("SPA_MODE", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    assert Settings().spa_mode == "disabled"


def test_spa_mode_from_env(monkeypatch):
    monkeypatch.setenv("SPA_MODE", "prod")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    assert Settings().spa_mode == "prod"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_spa_serving.py -k spa_mode -v`
Expected: FAIL — `Settings` has no `spa_mode`.

- [ ] **Step 3: Add the field**

In `src/call_manager/config.py`, add to the `Settings` class (use `Literal`; pydantic-settings reads `SPA_MODE` case-insensitively):

```python
from typing import Literal
# ...
    spa_mode: Literal["dev", "prod", "disabled"] = "disabled"
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_spa_serving.py -k spa_mode -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/config.py tests/test_spa_serving.py
git commit -m "feat(spa): SPA_MODE setting (dev|prod|disabled, default disabled)"
```

---

## Task 2: Route manifest + generator

**Files:** Create `frontend/routes.json`, `scripts/gen_routes.py`, `src/call_manager/web/spa_routes.py`, `frontend/src/routes.gen.ts`; Test: `tests/test_gen_routes.py`

> **Contract (authoritative — supersedes any prefix wording above):** the generated manifest is the **single source of truth** for SPA-vs-backend ownership. SPA ownership is an **EXACT/PATTERN match, never `startswith`/prefix**. `is_spa_route("/app")` is True; `is_spa_route("/made-up-page")` MUST be False so it reaches FastAPI's **real 404**. A `startswith("/")`-style test (matches everything) is the bug this task explicitly avoids. React Router MAY keep an in-app `*` catch-all, but the **server** is authoritative and returns `index.html` ONLY for manifest SPA routes. `BACKEND_PREFIXES`/`is_backend_route` exist only to drive the dev-proxy carve-out (Task 5), NOT to define SPA ownership.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gen_routes.py
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_generator_emits_python_and_ts(tmp_path, monkeypatch):
    # Run the generator; it reads frontend/routes.json and writes the two artifacts.
    subprocess.run([sys.executable, "scripts/gen_routes.py"], cwd=ROOT, check=True)
    py = (ROOT / "src/call_manager/web/spa_routes.py").read_text()
    ts = (ROOT / "frontend/src/routes.gen.ts").read_text()
    assert "SPA_ROUTES" in py and "is_spa_route" in py
    assert "BACKEND_PREFIXES" in py and "is_backend_route" in py
    assert "SPA_ROUTES" in ts and "BACKEND_PREFIXES" in ts


def test_is_spa_route_is_exact_not_prefix():
    from call_manager.web.spa_routes import is_spa_route
    # v1 manifest: only the temporary scaffold route is SPA-owned, by EXACT match.
    assert is_spa_route("/app") is True
    # An unknown/typo path is NOT SPA-owned — it must fall through to FastAPI's
    # real 404, NOT be served index.html. This is the anti-`startswith("/")` guard.
    assert is_spa_route("/made-up-page") is False
    assert is_spa_route("/") is False        # Jinja dashboard until Plan 3
    assert is_spa_route("/api/me") is False
    assert is_spa_route("/calls/123") is False
    # A SPA sub-path under the owned route is handled CLIENT-side by React Router;
    # the server still treats the document request as the owned SPA shell.
    assert is_spa_route("/app/anything") is True


def test_is_backend_route_for_proxy_carveout():
    from call_manager.web.spa_routes import is_backend_route
    # BACKEND_PREFIXES exist for the dev-proxy carve-out (Task 5), not for SPA ownership.
    assert is_backend_route("/api/me") is True
    assert is_backend_route("/calls/123") is True
    assert is_backend_route("/app") is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_gen_routes.py -v`
Expected: FAIL — generator + module don't exist.

- [ ] **Step 3: Author the manifest source**

The manifest expresses, per page route, an **exact match** (and reserves a `pattern` form as the forward-looking contract for sibling splits like `/workspaces/{id}` SPA vs `/workspaces/{id}/<action>` backend — do NOT build a generic pattern engine yet). `backend_prefixes` is enumerated from the real `include_router` calls in `src/call_manager/main.py:468-483` (verify against the code) and is used ONLY to build the dev-proxy carve-out:

```json
// frontend/routes.json
{
  "spa_routes": ["/app"],
  "backend_prefixes": ["/api", "/events", "/healthz", "/login", "/logout", "/register",
    "/auth", "/me", "/calls", "/admin", "/golden-set", "/profiles", "/rubric",
    "/workspaces", "/sections", "/annotations", "/files", "/references", "/citations",
    "/static"],
  "_comment": "spa_routes = SPA-owned page routes, matched EXACTLY (not by prefix). v1 = temporary scaffold mount; Plan 3 flips this to ['/'] and removes /app. Everything NOT a spa_route is backend-owned (a real handler OR FastAPI's real 404). backend_prefixes drives the Task-5 dev-proxy carve-out only — it is NOT how SPA ownership is decided."
}
```

- [ ] **Step 4: Write the generator**

The generator emits `is_spa_route` as an **exact/pattern match** (NOT `startswith` against `"/"`), plus `BACKEND_PREFIXES`/`is_backend_route` for the dev-proxy carve-out. For v1 the only owned route is `/app`; a request to `/app` (and React-Router sub-paths under it) is the SPA shell, but unknown sibling paths are NOT SPA-owned.

```python
# scripts/gen_routes.py
"""Generate route-ownership artifacts from frontend/routes.json.

Single source of truth → a Python module (FastAPI serving) and a TS module
(Vite proxy + React Router). Run in CI/pre-build.

SPA ownership is an EXACT/PATTERN match, never a prefix/`startswith` test:
a typo/unknown path must reach FastAPI's real 404, never be served index.html.
The pattern/precedence machinery is the forward-looking contract for sibling
splits and is added only when such a route first exists; v1 stays exact-match.
BACKEND_PREFIXES is emitted for the Task-5 dev-proxy carve-out, NOT for ownership.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = json.loads((ROOT / "frontend/routes.json").read_text())
SPA = sorted(SRC["spa_routes"], key=len, reverse=True)            # longest-first (precedence)
BACKEND = sorted(SRC["backend_prefixes"], key=len, reverse=True)

PY = '''# AUTO-GENERATED by scripts/gen_routes.py — do not edit by hand.
from __future__ import annotations

# SPA-owned page routes (EXACT match; React Router owns client sub-paths beneath).
SPA_ROUTES: tuple[str, ...] = ({spa})
# Backend prefixes — used ONLY to build the dev-proxy carve-out (Task 5),
# never to decide SPA ownership.
BACKEND_PREFIXES: tuple[str, ...] = ({backend})


def is_backend_route(path: str) -> bool:
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in BACKEND_PREFIXES)


def is_spa_route(path: str) -> bool:
    """True only for a manifest SPA-owned page route. A non-root owned route
    matches EXACTLY or via a React-Router client sub-path beneath it; the ROOT
    route "/" owns ONLY itself — never a subtree. So after Plan 3 flips
    SPA_ROUTES to ("/",), is_spa_route("/") is True but "/api/x", "/calls/1",
    "/made-up-page" are all False and fall through to FastAPI's real 404. This
    is deliberately NOT a `startswith("/")`-style match — note that a naive
    `path.startswith(r.rstrip("/") + "/")` collapses to "match everything" the
    moment the owned route is "/" (rstrip → "", startswith("/") → always True),
    which is exactly the bug this root special-case prevents."""
    for r in SPA_ROUTES:
        if r == "/":
            if path == "/":
                return True
        elif path == r or path.startswith(r.rstrip("/") + "/"):
            return True
    return False
'''.format(
    spa=", ".join(repr(r) for r in SPA) + ("," if SPA else ""),
    backend=", ".join(repr(p) for p in BACKEND) + ("," if BACKEND else ""),
)

TS = (
    "// AUTO-GENERATED by scripts/gen_routes.py — do not edit by hand.\n"
    f"export const SPA_ROUTES: string[] = {json.dumps(SPA)};\n"
    f"export const BACKEND_PREFIXES: string[] = {json.dumps(BACKEND)};\n"
)

(ROOT / "src/call_manager/web/spa_routes.py").write_text(PY)
(ROOT / "frontend/src").mkdir(parents=True, exist_ok=True)
(ROOT / "frontend/src/routes.gen.ts").write_text(TS)
print("wrote spa_routes.py and routes.gen.ts:", {"spa": SPA, "backend": BACKEND})
```

> **Note on the root special-case (load-bearing — this is the bug the matcher must survive):** in v1 the owned route is the exact path `/app`, and React Router owns sub-paths beneath it client-side, so the document request for `/app/...` resolves to the same SPA shell (subtree match). The catch is the **flip**: when Plan 3 changes `spa_routes` to `["/"]`, a naive subtree match `path.startswith(r.rstrip("/") + "/")` degenerates — `"/".rstrip("/")` is `""`, so `path.startswith("/")` matches **every** path, and `/api/x`, `/calls/1`, `/made-up-page` would all be (wrongly) SPA-owned and served `index.html` 200. The matcher above prevents this by special-casing `"/"` to **exact-only** (the root owns only itself, never a subtree). So post-flip `is_spa_route("/")` is True while `/api/x`, `/calls/1`, `/made-up-page` stay False and fall through to FastAPI's real 404 — independent of `is_backend_route` (which `is_spa_route` does **not** consult; backend-vs-SPA precedence is enforced in the serving/proxy layer by checking real routes first). This is verified by a post-flip regression test in **Plan 3 Task 9** (the flip is where the bug would activate, so the test lives there).

- [ ] **Step 5: Generate + run to verify it passes**

Run: `uv run python scripts/gen_routes.py && uv run pytest tests/test_gen_routes.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/routes.json scripts/gen_routes.py src/call_manager/web/spa_routes.py frontend/src/routes.gen.ts tests/test_gen_routes.py
git commit -m "feat(spa): route manifest + generator (Python + TS artifacts)"
```

---

## Task 3: FastAPI SPA serving (`mount_spa`) — prod + disabled

**Files:** Create `src/call_manager/web/spa_serving.py`; Modify `src/call_manager/main.py` (call after routers are included, before returning `app`); Test: `tests/test_spa_serving.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_spa_serving.py  (append)
from pathlib import Path
from fastapi.testclient import TestClient
from call_manager.main import create_app


def _env(monkeypatch, pg_url, **extra):
    for k, v in {
        "DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver", **extra,
    }.items():
        monkeypatch.setenv(k, v)


def test_disabled_mode_serves_no_spa(monkeypatch, pg_url, pg_session_factory):
    _env(monkeypatch, pg_url, SPA_MODE="disabled")
    c = TestClient(create_app())
    # The temp SPA route is not mounted; unknown path is a real 404, not index.html.
    assert c.get("/app").status_code == 404


def test_prod_mode_missing_build_fails_fast(monkeypatch, pg_url, pg_session_factory, tmp_path):
    import call_manager.web.spa_serving as spa_serving
    # Point SPA_DIR at an EMPTY tmp dir — never the real package tree — so the
    # fail-fast is asserted hermetically and can't be masked by a stray build.
    monkeypatch.setattr(spa_serving, "SPA_DIR", tmp_path / "spa")
    _env(monkeypatch, pg_url, SPA_MODE="prod")
    import pytest
    with pytest.raises(RuntimeError, match="SPA build missing"):
        create_app()


def test_prod_mode_serves_index_for_spa_routes(monkeypatch, pg_url, pg_session_factory, tmp_path):
    import call_manager.web.spa_serving as spa_serving
    # Fake a build in a tmp dir and INJECT it — never write into the real
    # src/call_manager/web/spa tree (that pollutes the worktree + leaks across tests).
    # Requires mount_spa to read the module-level SPA_DIR at call time.
    spa_dir = tmp_path / "spa"
    (spa_dir / "assets").mkdir(parents=True)
    (spa_dir / "index.html").write_text("<!doctype html><title>spa</title>")
    monkeypatch.setattr(spa_serving, "SPA_DIR", spa_dir)
    _env(monkeypatch, pg_url, SPA_MODE="prod")
    c = TestClient(create_app())
    assert "<title>spa</title>" in c.get("/app").text          # SPA route → index
    assert c.get("/api/does-not-exist").status_code == 404      # /api → real 404 (Plan 1 envelope)
    assert c.get("/totally-unknown").status_code == 404         # non-SPA → real 404, not index
```

> The test injects `SPA_DIR` via `monkeypatch.setattr`, so `mount_spa` must reference the **module-level `SPA_DIR` at call time** (not capture it as a default arg / closure) for the patch to take effect — the Step 3 implementation does exactly that.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_spa_serving.py -k "mode" -v`
Expected: FAIL — `mount_spa` not implemented / `/app` not served.

- [ ] **Step 3: Implement `mount_spa`**

```python
# src/call_manager/web/spa_serving.py
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

    # Catch-all, registered LAST: serve index.html ONLY for manifest SPA routes;
    # everything else (unknown /api, typos) gets FastAPI's real 404.
    @app.get("/{full_path:path}")
    def _spa_fallback(full_path: str, request: Request):  # noqa: ANN202
        path = "/" + full_path
        if is_spa_route(path):
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="not found")
```

In `src/call_manager/main.py`, after the routers are included and before `return app`:

```python
from call_manager.web.spa_serving import mount_spa
# ...
    mount_spa(app, spa_mode=settings.spa_mode)
    return app
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_spa_serving.py -v`
Expected: PASS (disabled, missing-build, prod-serves).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/spa_serving.py src/call_manager/main.py tests/test_spa_serving.py
git commit -m "feat(spa): FastAPI serving for SPA_MODE=prod (manifest-gated, fail-fast, real 404)"
```

---

## Task 4: Vite + React + TypeScript scaffold

**Files:** Create `frontend/package.json`, `frontend/.npmrc`, `frontend/vite.config.ts`, `frontend/tsconfig.json`, `frontend/index.html`, `frontend/src/main.tsx`, `frontend/src/App.tsx`; Modify `.gitignore`

> **Package manager (authoritative — overrides the npm wording elsewhere; per the 2026-06-16 supply-chain spec §3.1):** use **pnpm, not npm**. Pin pnpm via `package.json` `packageManager` + corepack; commit a **`pnpm-lock.yaml`** (NOT `package-lock.json`); use **exact version pins** (no `^`/`~`); installs are **`pnpm install --frozen-lockfile`**; lifecycle **scripts are OFF by default** (pnpm v10 default, with a minimal reviewed `onlyBuiltDependencies` allowlist) and a **`minimumReleaseAge` cooldown** (~7–14 days). There is no host `npm install`/`npm ci` and no caret ranges anywhere in this plan or Plan 3.

This task is scaffolding (no pytest). Verification = the **containerized** build (Task 6) produces `src/call_manager/web/spa/index.html`. (Authoring/install here uses `pnpm install --frozen-lockfile`; the actual production build is the secretless container in Task 6, never a host build.)

- [ ] **Step 1: Add gitignores**

Append to `.gitignore`:

```
frontend/node_modules/
frontend/dist/
src/call_manager/web/spa/
```

- [ ] **Step 2: Create `frontend/package.json`** (pnpm-pinned, exact pins, scripts-off allowlist)

Versions are **exact** (no `^`/`~`); bump them through the reviewed-dep-add rule (supply-chain spec §2). `packageManager` pins pnpm so corepack/the build container can't be steered onto a different version. **The pin MUST be ≥ `pnpm@10.16.0`** — `minimumReleaseAge` (the cooldown control, Step 2b) was **added in pnpm v10.16.0** ([pnpm settings docs](https://pnpm.io/settings#minimumreleaseage)); an older pin silently ignores the cooldown. `pnpm.onlyBuiltDependencies` is the **minimal reviewed allowlist** of packages permitted to run lifecycle scripts (everything else is scripts-off); start empty and add only after review.

```json
{
  "name": "call-manager-frontend",
  "private": true,
  "type": "module",
  "packageManager": "pnpm@10.16.0",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "test": "vitest run",
    "lint": "eslint ."
  },
  "dependencies": {
    "react": "18.3.1",
    "react-dom": "18.3.1"
  },
  "devDependencies": {
    "@types/react": "18.3.3",
    "@types/react-dom": "18.3.0",
    "@vitejs/plugin-react": "4.3.1",
    "typescript": "5.5.4",
    "vite": "5.4.0",
    "vitest": "2.0.5",
    "@testing-library/react": "16.0.0",
    "jsdom": "24.1.1",
    "@socketsecurity/cli": "1.0.0"
  },
  "pnpm": {
    "onlyBuiltDependencies": []
  }
}
```

- [ ] **Step 2b: Create `frontend/pnpm-workspace.yaml` + `frontend/.npmrc`** (committed pnpm config — NOT developer-local)

These settings must live in the repo so CI, the build container, and every contributor get them identically. **The cooldown is a pnpm-native setting and goes in `pnpm-workspace.yaml` (camelCase `minimumReleaseAge`), NOT `.npmrc`** — `.npmrc`'s `minimum-release-age` is not how pnpm reads this control ([pnpm settings docs](https://pnpm.io/settings#minimumreleaseage)). `.npmrc` keeps only the install-write-behavior keys it genuinely owns.

```yaml
# frontend/pnpm-workspace.yaml — pnpm-native project settings (supply-chain spec §3.1)
# Cooldown: refuse any version published more recently than ~7 days (minutes).
# Requires pnpm ≥ 10.16.0 (pinned via package.json packageManager).
minimumReleaseAge: 10080
# Exempt nothing yet; add a package here only after review if a hotfix is urgent.
minimumReleaseAgeExclude: []
```

```ini
# frontend/.npmrc — install write-behavior (these ARE valid npmrc keys)
# Exact pins: never write ^/~ ranges into package.json.
save-exact=true
save-prefix=
```

> Lifecycle scripts are off by pnpm v10 default; the `onlyBuiltDependencies` allowlist in `package.json` is the only opt-in and stays minimal/reviewed. Frozen installs are enforced by the explicit `--frozen-lockfile` flag on every `pnpm install` (Dockerfile + CI), not an rc key. _Confirm `minimumReleaseAge` is honored: `pnpm config get minimumReleaseAge` after install, and that the pinned pnpm is ≥ 10.16.0._

- [ ] **Step 3: Create `frontend/tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "skipLibCheck": true,
    "types": ["vite/client", "vitest/globals"]
  },
  "include": ["src"]
}
```

- [ ] **Step 4: Create `frontend/vite.config.ts`** (build output into the packaged tree; dev proxy in Task 5)

```ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

export default defineConfig({
  plugins: [react()],
  // Build to frontend/dist (INSIDE the frontend dir — writable by the non-root
  // container builder; the package tree at ../src is NOT copied into the build
  // image). The packaging step (Task 6) copies dist/ → src/call_manager/web/spa/.
  // Do NOT point outDir outside __dirname: in the container __dirname is /build,
  // so ../src/... resolves to /src/... which is neither present nor writable.
  build: { outDir: resolve(__dirname, "dist"), emptyOutDir: true },
});
```

- [ ] **Step 5: Create `frontend/index.html` + minimal app**

```html
<!-- frontend/index.html -->
<!doctype html>
<html lang="en">
  <head><meta charset="UTF-8" /><meta name="viewport" content="width=device-width, initial-scale=1.0" /><title>Call Manager</title></head>
  <body><div id="root"></div><script type="module" src="/src/main.tsx"></script></body>
</html>
```

```tsx
// frontend/src/main.tsx
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode><App /></StrictMode>
);
```

```tsx
// frontend/src/App.tsx
export function App() {
  return <main><h1>Call Manager</h1><p>SPA foundation OK.</p></main>;
}
```

- [ ] **Step 6: Generate the lockfile, install (frozen), build + verify the artifact**

Use pnpm via corepack (pinned by `packageManager`). The first run resolves and writes `pnpm-lock.yaml` (commit it); thereafter installs are `--frozen-lockfile`. The authoritative production build is the secretless container (Task 6) — this host run is only to author the scaffold and confirm it compiles:

```bash
corepack enable
# First time only: resolve + write the lockfile (then commit pnpm-lock.yaml).
cd frontend && pnpm install && cd ..
# Steady state (CI + the build container) uses the frozen, integrity-checked install:
cd frontend && pnpm install --frozen-lockfile && pnpm run build && cd ..
test -f src/call_manager/web/spa/index.html && echo "BUILD OK"
```
Expected: `BUILD OK`. Confirm **`frontend/pnpm-lock.yaml`** was created (there is NO `package-lock.json`).

- [ ] **Step 7: Commit**

```bash
git add frontend/package.json frontend/pnpm-workspace.yaml frontend/.npmrc frontend/pnpm-lock.yaml frontend/tsconfig.json frontend/vite.config.ts frontend/index.html frontend/src/main.tsx frontend/src/App.tsx .gitignore
git commit -m "feat(spa): Vite + React + TS scaffold (pnpm, frozen lockfile, exact pins)"
```

---

## Task 5: Dev proxy (Vite sole front door) + dev CSP relaxation

**Files:** Modify `frontend/vite.config.ts` (dev server); Modify `src/call_manager/web/middleware.py` (`build_csp(dev=...)`); Modify `src/call_manager/main.py` (use dev CSP when `spa_mode == "dev"`); Test: `tests/test_spa_serving.py` (CSP relaxation)

- [ ] **Step 1: Write the failing test (dev CSP relaxation)**

```python
# tests/test_spa_serving.py  (append)
def test_dev_mode_relaxes_csp(monkeypatch, pg_url, pg_session_factory):
    _env(monkeypatch, pg_url, SPA_MODE="dev")
    c = TestClient(create_app())
    csp = c.get("/login").headers.get("content-security-policy", "")
    # Dev must allow Vite HMR websocket + inline dev scripts; prod must not.
    assert "ws:" in csp or "ws://localhost" in csp

def test_prod_mode_keeps_strict_script_src(monkeypatch, pg_url, pg_session_factory):
    _env(monkeypatch, pg_url, SPA_MODE="disabled")
    c = TestClient(create_app())
    csp = c.get("/login").headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp and "script-src 'self' 'unsafe-inline'" not in csp
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_spa_serving.py -k csp -v`
Expected: FAIL — CSP is identical in all modes today.

- [ ] **Step 3: Add a dev CSP variant**

In `src/call_manager/web/middleware.py`, extend `build_csp`:

```python
def build_csp(dev: bool = False) -> str:
    script = "script-src 'self'"
    connect = "connect-src 'self'"
    if dev:
        # Vite injects inline dev scripts and opens an HMR WebSocket.
        script = "script-src 'self' 'unsafe-inline'"
        connect = "connect-src 'self' ws: http://localhost:5173"
    return "; ".join([
        "default-src 'self'", script, "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:", "font-src 'self'", connect,
        "form-action 'self'", "base-uri 'self'", "frame-ancestors 'none'", "object-src 'none'",
    ])
```

In `src/call_manager/main.py`, build the CSP per mode (line ~280):

```python
    app.add_middleware(
        SecurityHeadersMiddleware,
        csp=build_csp(dev=settings.spa_mode == "dev"),
        hsts=settings.cookie_secure,
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_spa_serving.py -k csp -v`
Expected: PASS.

- [ ] **Step 5: Configure the Vite dev server (default-to-backend, carve out the SPA)**

**Posture (authoritative):** Vite forwards **EVERYTHING to uvicorn by default**, carving out ONLY (a) SPA-owned page routes → Vite serves `index.html`, and (b) Vite's own dev-asset paths (`/@vite`, `/@id`, `/src`, `/node_modules`, the HMR WebSocket). This is the **opposite** of "proxy only `BACKEND_PREFIXES` to uvicorn" (which would wrongly leave unknown/typo paths on Vite's HTML 404). An unknown, non-SPA path in dev therefore goes uvicorn → **FastAPI's real 404**. The carve-out is built **from the generated manifest** (`SPA_ROUTES`), never a hand-edited regex, so Plan 3's flip is just a regenerate.

Add to `frontend/vite.config.ts` a `server` block plus a tiny plugin that intercepts SPA-owned routes and otherwise falls through to the default-to-backend proxy:

```ts
import type { Connect, PluginOption } from "vite";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { SPA_ROUTES } from "./src/routes.gen";

const BACKEND = "http://localhost:8000";
// Vite's own dev-asset prefixes — these must NEVER be forwarded to uvicorn.
const VITE_DEV_PREFIXES = ["/@vite", "/@id", "/@fs", "/src", "/node_modules"];

const isViteAsset = (url: string) =>
  VITE_DEV_PREFIXES.some((p) => url === p || url.startsWith(p + "/"));
// EXACT/owned-route match against the manifest (not a generic startsWith("/")).
// Root "/" owns ONLY itself — never a subtree — so after Plan 3 flips
// SPA_ROUTES to ["/"] this does NOT collapse to "match everything" (which a
// naive startsWith(r.replace(/\/$/,"") + "/") would, since "/" → "" → matches
// every path). Unknown/typo paths then fall through to the default proxy →
// uvicorn → FastAPI's real 404, matching the prod matcher in Task 2.
const isSpaRoute = (path: string) =>
  SPA_ROUTES.some((r) =>
    r === "/" ? path === "/" : path === r || path.startsWith(r.replace(/\/$/, "") + "/"),
  );

// Serve index.html for SPA-owned document routes; let everything else fall
// through to the default proxy (→ uvicorn → FastAPI real 404 for unknowns).
function spaIndexFallback(): PluginOption {
  return {
    name: "spa-index-fallback",
    configureServer(server) {
      server.middlewares.use((req: Connect.IncomingMessage, res, next) => {
        const url = (req.url ?? "/").split("?")[0];
        const accepts = (req.headers.accept ?? "").includes("text/html");
        if (req.method === "GET" && accepts && !isViteAsset(url) && isSpaRoute(url)) {
          const html = readFileSync(resolve(__dirname, "index.html"), "utf-8");
          server.transformIndexHtml(url, html).then((out) => {
            res.setHeader("Content-Type", "text/html");
            res.end(out);
          });
          return;
        }
        next(); // → Vite dev assets, or the default-to-backend proxy below
      });
    },
  };
}

// In defineConfig({ plugins: [react(), spaIndexFallback()], ... }) add:
  server: {
    port: 5173,
    proxy: {
      // DEFAULT TO BACKEND: forward every path to FastAPI — EXCEPT the two
      // classes that must stay with Vite. A bare "^/" target would also swallow
      // /@vite, /@id, /@fs, /src, /node_modules and the SPA document routes,
      // breaking HMR/source loading. `bypass` returning the request URL tells
      // Vite to SKIP proxying and let its own middleware chain serve it (Vite
      // dev assets) or the spaIndexFallback plugin serve index.html (SPA routes).
      // Returning undefined proxies to uvicorn (backend route, or unknown → real 404).
      "^/": {
        target: BACKEND,
        changeOrigin: true,
        ws: false,
        bypass(req) {
          const url = (req.url ?? "/").split("?")[0];
          if (isViteAsset(url) || isSpaRoute(url)) return req.url; // serve via Vite
          return undefined; // → uvicorn (real 404 for unknown non-SPA paths)
        },
      },
    },
  },
```

> Why not "proxy only `BACKEND_PREFIXES`": that posture leaves typo/unknown paths on Vite, which would answer with Vite's HTML 404 (or worse, `index.html`) instead of FastAPI's JSON 404. Default-to-backend + manifest carve-out is the only posture that satisfies "unknown non-SPA path → FastAPI real 404 in dev too." The `bypass` is what makes "^/" safe: without it the catch-all would intercept Vite's own dev-asset requests (HMR would break). `BACKEND_PREFIXES` from the manifest is still available if a future split needs explicit per-prefix proxy options, but it is NOT the gate. Document the two-process dev recipe (below).

- [ ] **Step 5b: Write the three dev-proxy tests**

These assert the dev front-door posture end-to-end (run the two-process recipe, or drive Vite's middleware in a node:test harness):

1. **CSRF POST through Vite** — a state-changing POST (e.g. `/logout`) issued at the Vite origin carries `Origin: http://localhost:5173`, is forwarded to uvicorn, and passes CSRF (because `APP_BASE_URL=http://localhost:5173` in dev) rather than 403-ing.
2. **Un-ported page → FastAPI** — `GET /login` (an existing backend HTML route, not a SPA route) is forwarded to uvicorn and returns the server-rendered page, not Vite's `index.html`.
3. **Unknown non-SPA path → FastAPI real 404 (JSON)** — `GET /totally-unknown` (with `Accept: application/json`) is forwarded to uvicorn and returns FastAPI's real `404`, NOT Vite's HTML 404 and NOT `index.html`.

- [ ] **Step 6: Document the dev recipe**

Add to the project dev docs (or `frontend/README.md`):

```
# Terminal 1: backend (required env from config.py, else create_app won't start)
SPA_MODE=dev APP_BASE_URL=http://localhost:5173 \
  DATABASE_URL=postgresql+psycopg://... SESSION_SECRET=dev-secret-at-least-16-chars \
  ALLOWED_EMAIL_DOMAINS=ki.se \
  uv run uvicorn call_manager.main:create_app --factory --reload --port 8000
# Terminal 2: frontend (pnpm, not npm)
cd frontend && pnpm install --frozen-lockfile && pnpm run dev   # open http://localhost:5173
```

(`APP_BASE_URL=http://localhost:5173` so CSRF accepts same-origin POSTs through Vite. `DATABASE_URL`/`SESSION_SECRET`/`ALLOWED_EMAIL_DOMAINS` are required by `config.py` for the app to boot.)

- [ ] **Step 7: Commit**

```bash
git add frontend/vite.config.ts src/call_manager/web/middleware.py src/call_manager/main.py tests/test_spa_serving.py
git commit -m "feat(spa): Vite dev proxy (sole front door) + dev CSP relaxation"
```

---

## Task 6: Containerized secretless build + prod packaging (hatch) + CI wheel-contents check + JS scanners

**Files:** Create `frontend/Dockerfile` (the build container), `.dockerignore` (repo-root, keeps the build context minimal/secretless); Modify `pyproject.toml` (hatch `artifacts`); Modify `README.md` (deploy sequence); Modify `.github/dependabot.yml`; Modify `.github/workflows/ci.yml`

> **Build location (authoritative — per the 2026-06-16 supply-chain spec §4):** the frontend build runs in an **ephemeral, NON-ROOT, SECRETLESS, EGRESS-RESTRICTED container** — NOT a host `npm ci` / host `pnpm build` and **NOT `python -m hatchling` on the host**. The container gets only the source tree + lockfiles (no app env, no `var/` mount, no DB creds, no internal-network reach; egress only to package registries), runs `pnpm install --frozen-lockfile` with scripts off, and emits **only** `src/call_manager/web/spa/`. Then **`uv build --wheel`** packages that emitted tree (hatch `artifacts`); a CI assertion proves the wheel contains `spa/index.html`. (CI-only artifact build is the strictly-stronger future step, spec §10.)

- [ ] **Step 1: Add the hatch artifacts rule**

In `pyproject.toml`, under `[tool.hatch.build.targets.wheel]` (line 51), add an `artifacts` entry so the gitignored, container-emitted build dir is still packaged (hatchling excludes VCS-ignored files by default — this is mandatory, not optional):

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/call_manager"]
artifacts = ["src/call_manager/web/spa/**/*"]
```

- [ ] **Step 2: Author the secretless build `Dockerfile`**

Create `frontend/Dockerfile` — a one-shot, non-root, egress-restricted build that takes source + lockfiles and emits ONLY the SPA bundle. Base image pinned **by digest**; pnpm pinned via `packageManager`/corepack; `--frozen-lockfile`; scripts off:

```dockerfile
# frontend/Dockerfile — ephemeral, non-root, secretless SPA build.
# Pin base by DIGEST (not a floating tag). Example digest is illustrative.
FROM node:20-bookworm-slim@sha256:<pin-the-real-digest>

# Non-root build user; no Docker socket, no app secrets, no var/ mount.
RUN useradd --create-home --uid 10001 builder
USER builder
WORKDIR /build

# corepack activates the pnpm pinned by package.json "packageManager".
RUN corepack enable

# Copy ONLY lockfiles + manifest + pnpm config first (cache-friendly), install frozen.
COPY --chown=builder:builder frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml frontend/.npmrc ./
# Scripts off (pnpm v10 default + onlyBuiltDependencies allowlist in package.json);
# integrity-checked against the committed lockfile.
RUN pnpm install --frozen-lockfile

# Now the source; build emits to /build/dist (inside the writable build context).
COPY --chown=builder:builder frontend/ ./
RUN pnpm run build   # outDir = /build/dist (vite.config.ts → resolve(__dirname,"dist"))
```

Also add a repo-root **`.dockerignore`** (the build context is `.`): without it, `COPY frontend/ ./` would clobber the freshly-installed `/build/node_modules` with the host's (and could drag host cruft/secrets into the image — against the secretless posture):

```
# .dockerignore — keep the build context minimal + secretless
.git
var/
**/node_modules
frontend/dist
src/call_manager/web/spa
*.env
.env*
```

Run it so the build never touches host secrets or the network beyond registries (drop egress after the fetch where your runtime supports it). Extract the bundle from `/build/dist` into the package tree on the host:

```bash
# Build the image (fetch phase needs the registry; nothing else).
docker build -f frontend/Dockerfile -t call-manager-spa-build .
# Extract ONLY the emitted bundle into the package tree (no secrets ever entered).
container=$(docker create call-manager-spa-build)
rm -rf src/call_manager/web/spa && mkdir -p src/call_manager/web/spa
docker cp "$container:/build/dist/." src/call_manager/web/spa/
docker rm "$container"
test -f src/call_manager/web/spa/index.html && echo "CONTAINER BUILD OK"
```

> The container has **no** `--env-file`, no `var/` mount, no DB creds, and (ideally) `--network` restricted to the registry / dropped after fetch. A compromised build script finds an empty, sandboxed box (spec §4.1).

- [ ] **Step 3: Build the wheel from the container-emitted tree and assert it contains the SPA**

On the host (which now holds ONLY the static bundle the container produced), package with **`uv build --wheel`** (NOT `python -m hatchling build` — hatchling isn't a runnable module in the venv):

```bash
uv run python scripts/gen_routes.py     # keep generated route artifacts fresh
uv build --wheel
unzip -l dist/*.whl | grep "call_manager/web/spa/index.html" && echo "WHEEL OK"
```
Expected: the grep matches; `WHEEL OK`. (If it doesn't match, the `artifacts` glob is wrong — fix before proceeding.)

- [ ] **Step 4: Wire the containerized build into the README deploy sequence**

Add to `README.md` (Production deployment) the ordering so a redeploy always (re)builds the SPA in the container, asserts the artifact, then (re)starts the app — the app process never runs a package manager and only consumes the static files:

```
# 1. Build the SPA in the ephemeral secretless container (no host pnpm/npm):
docker build -f frontend/Dockerfile -t call-manager-spa-build .
container=$(docker create call-manager-spa-build)
rm -rf src/call_manager/web/spa && mkdir -p src/call_manager/web/spa
docker cp "$container:/build/dist/." src/call_manager/web/spa/
docker rm "$container"
# 2. Verify the artifact BEFORE (re)starting the app:
test -f src/call_manager/web/spa/index.html || { echo "SPA build missing"; exit 1; }
# 3. Package + (re)start (uv build, then the existing systemctl flow):
uv build --wheel
sudo systemctl restart call-manager
```

- [ ] **Step 5: Add JS detection + the build ordering to CI**

In `.github/workflows/ci.yml`, add the pnpm frozen/scripts-off install, the JS scanners, and the container build → `uv build` ordering (so a missing-build wheel fails the pipeline, not just runtime). Replace any `npm`/`python -m hatchling` with pnpm/`uv build`:

```yaml
# Frontend deps: pnpm via corepack, frozen, scripts off (minimumReleaseAge from pnpm-workspace.yaml).
- run: corepack enable
- run: cd frontend && pnpm install --frozen-lockfile
# JS supply-chain scanners over the pnpm lockfile (spec §5, §7):
- run: cd frontend && pnpm audit
# Socket CLI: pinned exact devDependency (NO `npx --yes` — that would fetch an
# unpinned latest at runtime, defeating the exact-pin + cooldown posture).
- run: cd frontend && pnpm exec socket scan create .
- uses: google/osv-scanner-action@v1                         # OSV over pnpm-lock.yaml (+ uv.lock)
  with: { scan-args: "--lockfile=frontend/pnpm-lock.yaml" }
# Build the SPA in the container, then package + assert wheel contents:
- run: docker build -f frontend/Dockerfile -t call-manager-spa-build .
- run: |
    c=$(docker create call-manager-spa-build)
    rm -rf src/call_manager/web/spa && mkdir -p src/call_manager/web/spa
    docker cp "$c:/build/dist/." src/call_manager/web/spa/
    docker rm "$c"
- run: uv run python scripts/gen_routes.py    # keep generated artifacts fresh
- run: uv build --wheel
- run: unzip -l dist/*.whl | grep -q "call_manager/web/spa/index.html"
```

Also add Dependency Review on PRs (`actions/dependency-review-action`) and the `npm` ecosystem to `.github/dependabot.yml`:

```yaml
# .github/dependabot.yml (add the npm ecosystem entry; Python entry stays)
- package-ecosystem: "npm"
  directory: "/frontend"
  schedule: { interval: "weekly" }
```

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml frontend/Dockerfile README.md .github/dependabot.yml .github/workflows/ci.yml
git commit -m "build(spa): containerized secretless build + uv build wheel + JS scanners + wheel-contents check"
```

---

## Self-review checklist (completed)

- **Spec coverage:** `SPA_MODE` tri-state ✓; generated route manifest (Python + TS, **exact/pattern SPA ownership** + `BACKEND_PREFIXES` proxy carve-out, v1 trivial) ✓; prod manifest-gated serving + fail-fast + **real-404 for unknown/typo paths** ✓; dev **default-to-backend + manifest SPA/dev-asset carve-out** + unknown→FastAPI-404 + dev-CSP relaxation + the three dev-proxy tests ✓; **pnpm** (frozen, exact pins, scripts-off + cooldown) ✓; **containerized secretless build** + `uv build --wheel` + CI wheel-contents check + JS scanners ✓. The `X-Forwarded` requirement was dropped in spec rev 5 (not here). `/events/ping`, OpenAPI→TS, and flipping `/` to the SPA are in Plan 3.
- **Placeholder scan:** the CI file path, the Dockerfile base-image digest, and the pnpm version pin must be adapted to the actual workflow / pinned values (flagged inline). No "TODO/implement later". No `startswith("/")` SPA matching, no "proxy only `BACKEND_PREFIXES`", no npm/`package-lock.json`/caret ranges, no host `npm ci`/`python -m hatchling`.
- **Type consistency:** `is_spa_route` (exact), `is_backend_route`, `SPA_ROUTES`, `BACKEND_PREFIXES`, `mount_spa`, `build_csp(dev=...)` consistent across tasks.

## Note

Plan 2 mounts the SPA at the temporary `/app` route so the live Jinja dashboard at `/` is untouched. Plan 3's final task flips `routes.json` `spa_routes` to `["/"]`, regenerates, removes the Jinja `/` handler, and retires `/app`.
