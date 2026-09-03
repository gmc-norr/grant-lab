import { defineConfig } from "@playwright/test";

// Dual-mode smoke: the SAME specs run against either dev front-door.
//   E2E_MODE=dev  (default) → Vite dev server (proxying to uvicorn SPA_MODE=dev) on :5173
//   E2E_MODE=prod           → FastAPI serving the built SPA (SPA_MODE=prod) on :8000
// The live run is deferred (see e2e/dashboard.spec.ts header): it needs a seeded
// active e2e user, a running Postgres, and `playwright install --with-deps`
// (sudo). CI wires the two webServers + seed; locally start them by hand.
//
// CSRF constraint: the OriginCSRFMiddleware rejects any state-changing POST whose
// Origin != APP_BASE_URL, so the uvicorn front-door MUST be started with
// APP_BASE_URL set to the ORIGIN THE BROWSER POSTS FROM, or login/logout 403:
//   E2E_MODE=dev  → APP_BASE_URL=http://localhost:5173 (the Vite origin)
//   E2E_MODE=prod → APP_BASE_URL=http://localhost:8000 (FastAPI serves directly)
const MODE = process.env.E2E_MODE ?? "dev";

export default defineConfig({
  testDir: "./e2e",
  // No webServer block here on purpose: the dev (Vite + uvicorn) and prod
  // (uvicorn-only) toplogies differ, and the seeded DB must be brought up out of
  // band. The harness/CI starts the servers; these specs only drive the browser.
  use: {
    baseURL: MODE === "prod" ? "http://localhost:8000" : "http://localhost:5173",
    // CI uploads frontend/test-results/ on failure (ci.yml `e2e` job); a trace is
    // the only way to see WHY a two-browser collab step failed on a headless runner.
    trace: process.env.CI ? "retain-on-failure" : "off",
  },
});
