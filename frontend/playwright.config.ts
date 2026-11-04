import { defineConfig } from "@playwright/test";

// Two kinds of browser run, selected by E2E_MODE:
//   E2E_MODE=dev  (default) → e2e/smoke.spec.ts via the Vite dev server on :5173,
//                             which proxies to uvicorn (SPA_MODE=dev) on :8000
//   E2E_MODE=prod           → e2e/smoke.spec.ts against FastAPI serving the
//                             packaged SPA (SPA_MODE=prod) on :8000
//   E2E_MODE=match          → e2e/match.spec.ts against the production bundle,
//                             built by the webServer below and served statically
//                             on :4173 with the API mocked in-page. The only mode
//                             that starts its own server.
// dev and prod need a seeded Postgres and a running uvicorn, so
// scripts/e2e_run.sh brings those up (and Vite in dev mode) and these specs only
// drive the browser.
//
// CSRF constraint: the OriginCSRFMiddleware rejects any state-changing POST whose
// Origin != APP_BASE_URL, so uvicorn MUST run with APP_BASE_URL set to the origin
// the browser POSTs from, or login/logout 403:
//   E2E_MODE=dev  → APP_BASE_URL=http://localhost:5173 (the Vite origin)
//   E2E_MODE=prod → APP_BASE_URL=http://localhost:8000 (FastAPI serves directly)
const MODE = process.env.E2E_MODE ?? "dev";

export default defineConfig({
  testDir: "./e2e",
  // Each mode runs exactly one spec, so a bare `playwright test` can never drive
  // the mocked spec at a real backend, or the smoke spec at the static mock server.
  testMatch: MODE === "match" ? /match\.spec\.ts$/ : /smoke\.spec\.ts$/,
  webServer:
    MODE === "match"
      ? {
          // The artefact IS the thing under test, so it is always rebuilt: a
          // build that fails at tsc cannot leave a previous dist/ standing.
          command: "pnpm run build && node ../scripts/serve_spa_static.mjs dist 4173",
          url: "http://127.0.0.1:4173/index.html",
          // NOT `!process.env.CI`: reusing a server on :4173 would skip the build
          // and validate whatever bundle is already being served.
          reuseExistingServer: false,
          timeout: 180_000,
        }
      : undefined,
  use: {
    baseURL:
      MODE === "match"
        ? "http://127.0.0.1:4173"
        : MODE === "prod"
          ? "http://localhost:8000"
          : "http://localhost:5173",
    // CI uploads frontend/test-results/ on failure; a trace is the only way to
    // see WHY a step failed on a headless runner.
    trace: process.env.CI ? "retain-on-failure" : "off",
  },
});
