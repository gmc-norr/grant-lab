# Call Manager — Frontend (React SPA)

Vite + React + TypeScript SPA, built and served per **Plan 2**
(`docs/superpowers/plans/2026-06-15-react-spa-foundation-2-serving.md`). The
live Jinja dashboard at `/` is untouched until Plan 3; this SPA mounts at the
temporary `/app` route.

## Toolchain

- **Node 22** — see `.nvmrc` (`nvm use`). Ubuntu 24.04's apt only ships Node 18.
- **pnpm 10.16.0** via corepack (pinned in `package.json` `packageManager`).
- Installs are **frozen, scripts-off**, with a `minimumReleaseAge` cooldown
  (`pnpm-workspace.yaml`). Exact pins only — no `^`/`~` (`.npmrc`).

## Local dev — two processes (Vite is the sole front door on :5173)

Vite forwards **everything to uvicorn by default** and carves out only (a) SPA
page routes (→ `index.html`) and (b) its own dev assets (`/@vite`, `/src`, …).
An unknown/typo path therefore still reaches **FastAPI's real 404** in dev.

```bash
# Terminal 1 — backend (SPA_MODE=dev; the env below is required by config.py,
# else create_app() won't start):
SPA_MODE=dev APP_BASE_URL=http://localhost:5173 \
  DATABASE_URL=postgresql+psycopg://... SESSION_SECRET=dev-secret-at-least-16-chars \
  ALLOWED_EMAIL_DOMAINS=ki.se \
  uv run uvicorn call_manager.main:create_app --factory --reload --port 8000

# Terminal 2 — frontend:
cd frontend && pnpm install --frozen-lockfile && pnpm run dev   # http://localhost:5173
```

`APP_BASE_URL=http://localhost:5173` so CSRF accepts same-origin POSTs issued
through Vite. `SPA_MODE=dev` also relaxes the CSP (allows Vite's inline
bootstrap script + the HMR WebSocket); `prod`/`disabled` keep the strict policy.

## Build

The **authoritative** production build runs in the secretless container (Task
6), not on the host. Locally, to confirm it compiles:

```bash
pnpm run build    # tsc typecheck + vite build -> frontend/dist/
```

The container copies `dist/` → `src/call_manager/web/spa/`, which FastAPI serves
when `SPA_MODE=prod`.

## Tests

```bash
pnpm run test     # vitest — e.g. the dev-proxy carve-out logic (src/dev-proxy.test.ts)
```

## Known gap (not silently skipped)

The dev front-door **routing decision** (SPA route vs Vite asset vs proxy-to-backend)
is unit-tested in `src/dev-proxy.test.ts`, including the Plan-3 root flip. The
full **two-process** end-to-end (CSRF POST through Vite passes; a backend route
like `/login` is not hijacked by Vite; an unknown path returns FastAPI's JSON
404) is verified by running the recipe above manually. A CI integration that
stands up uvicorn (with Postgres) + Vite together is intentionally deferred — it
is heavy and flaky relative to the value over the unit-level coverage.
