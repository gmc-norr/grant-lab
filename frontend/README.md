# Grant Lab — Frontend (React SPA)

Vite + React + TypeScript SPA. It owns all browser page routes: the calls triage feed
and call pages, profiles, boards (including the public corridor wall), admin, auth, and
settings. FastAPI provides the JSON APIs, OAuth endpoints and production bundle serving.

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
  ALLOWED_EMAIL_DOMAINS=umu.se \
  uv run uvicorn grant_lab.main:create_app --factory --reload --port 8000

# Terminal 2 — frontend:
cd frontend && pnpm install --frozen-lockfile && pnpm run dev   # http://localhost:5173
```

`APP_BASE_URL=http://localhost:5173` so CSRF accepts same-origin POSTs issued
through Vite. `SPA_MODE=dev` also relaxes the CSP (allows Vite's inline
bootstrap script + the HMR WebSocket); `prod`/`disabled` keep the strict policy.

## Build

```bash
pnpm run build      # tsc typecheck + vite build -> frontend/dist/
pnpm run package    # scripts/package_spa.sh: frontend/dist/ -> src/grant_lab/web/spa/
```

`package` is the only supported writer of `src/grant_lab/web/spa/`, which FastAPI
serves when `SPA_MODE=prod`. CI builds the same bundle in the secretless container
(`frontend/Dockerfile`) and packages it through the same script.

## Tests

```bash
pnpm run test       # vitest
pnpm run lint       # oxlint over src/ and e2e/
pnpm run e2e:match  # the production bundle in Chromium, API mocked in-page
bash ../scripts/e2e_run.sh   # real backend: testcontainers Postgres + uvicorn (+ Vite)
```

## Two-process end-to-end

The dev front-door routing decision is unit-tested in `src/dev-proxy.test.ts`. The
two-process path (login and logout POSTs through Vite, an unknown path reaching
FastAPI's JSON 404, a call page surviving a reload) is `e2e/smoke.spec.ts`, run by
`scripts/e2e_run.sh` in CI's `e2e` job in both `dev` and `prod` modes.

## Dependency audit posture

CI **blocks** on `pnpm audit --prod` (production/runtime deps that ship in the
browser bundle — currently `react`/`react-dom`, clean). It **surfaces but does
not block** on dev/build-tooling advisories (vite/esbuild/vitest dev-server
issues, etc.): those never reach production — the deployed app is FastAPI
serving static files, with no node/vite/vitest at runtime — and are further
mitigated by the ephemeral secretless build container and by not exposing dev
servers (we run `vitest run`, never `vitest --ui`). Dependabot (npm ecosystem,
`/frontend`) drives the toolchain bumps that clear them. Socket CLI (spec §5) is
a token-gated follow-up (needs `SOCKET_SECURITY_API_KEY`).
