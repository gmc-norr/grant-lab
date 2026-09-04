import { defineConfig } from "@playwright/test";

// Dual-mode smoke: the SAME specs run against either dev front-door.
//   E2E_MODE=dev  (default) → Vite dev server (proxying to uvicorn SPA_MODE=dev) on :5173
//   E2E_MODE=prod           → FastAPI serving the built SPA (SPA_MODE=prod) on :8000
//   E2E_MODE=match          → the MATCH bundle, built and gate-checked by the
//                             webServer below, served statically on :4173 with the
//                             API mocked in-page. e2e/match.spec.ts only, and it
//                             is the only mode that starts its own server.
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
  // Match mode owns its server, because the artefact IS the thing under test:
  // the two commands are chained so a build that fails at tsc cannot leave the
  // previous dist/ standing for the gate — and for the browser — to pass over.
  // `pnpm test` (vitest) never sees these specs; testDir is e2e/ and
  // vite.config.ts scopes vitest to src/.
  // Match mode runs ONE spec. Without this, `E2E_MODE=match playwright test` with
  // no path argument collects e2e/collab.spec.ts and e2e/dashboard.spec.ts too and
  // drives them at the static mock server, which has no Postgres, no uvicorn and no
  // seeded user. `pnpm run e2e:match` names the spec, so the documented entry point
  // was never wrong — but a gate that is only correct when invoked exactly one way
  // is one bare command away from a confusing red run, or from silently adopting the
  // next spec someone drops in e2e/.
  testMatch: MODE === "match" ? /match\.spec\.ts$/ : undefined,
  webServer:
    MODE === "match"
      ? {
          command:
            "pnpm run build:match && pnpm run check:bundle && node ../scripts/serve_spa_static.mjs dist 4173",
          url: "http://127.0.0.1:4173/index.html",
          // NOT `!process.env.CI`. Reusing a server on :4173 skips the command
          // above entirely — so a local run would validate whatever bundle is
          // already being served, which may predate every change under test. This
          // repo has already shipped one stale-artefact defect of exactly that
          // shape (a build that failed at tsc wrote no manifest, and a separately
          // invoked check read the previous clean one and exited 0). The artefact
          // IS the thing under test here, so it is always rebuilt.
          reuseExistingServer: false,
          timeout: 180_000,
        }
      : undefined,
  // dev and prod start NO webServer here on purpose: their topologies differ
  // (Vite + uvicorn vs uvicorn alone) and the seeded DB must be brought up out of
  // band, so scripts/collab_e2e_run.sh owns that and those specs only drive the
  // browser. Match mode has neither problem — no Postgres, no uvicorn — so it can
  // own its server, and doing so is what makes the bundle under test unambiguous.
  use: {
    baseURL:
      MODE === "match"
        ? "http://127.0.0.1:4173"
        : MODE === "prod"
          ? "http://localhost:8000"
          : "http://localhost:5173",
    // CI uploads frontend/test-results/ on failure (ci.yml `e2e` job); a trace is
    // the only way to see WHY a two-browser collab step failed on a headless runner.
    trace: process.env.CI ? "retain-on-failure" : "off",
  },
});
