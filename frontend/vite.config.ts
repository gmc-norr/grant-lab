import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import react from "@vitejs/plugin-react";
import type { Connect, PluginOption } from "vite";
// vitest/config's defineConfig is a superset of vite's that also accepts `test`.
import { defineConfig } from "vitest/config";

import { isSpaRoute, isViteAsset, shouldServeViaVite } from "./src/dev-proxy";

const BACKEND = "http://localhost:8000";

// Serve index.html for SPA-owned document routes; let everything else fall
// through to the default-to-backend proxy below (-> uvicorn -> FastAPI real 404
// for unknowns). Route ownership comes from the generated manifest.
// configureServer runs in dev only (vite build never calls it).
function spaIndexFallback(): PluginOption {
  return {
    name: "spa-index-fallback",
    configureServer(server) {
      server.middlewares.use((req: Connect.IncomingMessage, res, next) => {
        const url = (req.url ?? "/").split("?")[0];
        // No Accept gate: prod's _spa_fallback serves the shell for ANY GET to
        // an SPA-owned route, so dev must too (else GET /app with an explicit
        // Accept: application/json would 404 in dev but 200 in prod). The
        // isViteAsset guard still routes /@vite, /src, /node_modules to Vite,
        // and isSpaRoute still proxies /api/... + unknown paths to uvicorn.
        if (req.method === "GET" && !isViteAsset(url) && isSpaRoute(url)) {
          const html = readFileSync(resolve(__dirname, "index.html"), "utf-8");
          server.transformIndexHtml(url, html).then((out) => {
            res.setHeader("Content-Type", "text/html");
            res.end(out);
          });
          return;
        }
        next(); // -> Vite dev assets, or the default-to-backend proxy below
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), spaIndexFallback()],
  // Build to frontend/dist (INSIDE the frontend dir — writable by the non-root
  // container builder; the package tree at ../src is NOT copied into the build
  // image). The packaging step (Task 6) copies dist/ -> src/grant_lab/web/spa/.
  // Do NOT point outDir outside __dirname: in the container __dirname is /build,
  // so ../src/... would resolve to /src/... which is neither present nor writable.
  build: { outDir: resolve(__dirname, "dist"), emptyOutDir: true },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    // Scope unit tests to src/ so vitest does NOT collect the Playwright e2e
    // specs (frontend/e2e/*.spec.ts) — they use @playwright/test's runner +
    // browser fixtures and would crash under vitest. Playwright runs them via
    // its own config (playwright.config.ts).
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    exclude: ["**/node_modules/**"],
    // `pnpm run test:coverage` measures app code only: tests and the generated
    // API types are left out. CI checks the summary against coverage-floors.json.
    coverage: {
      provider: "v8",
      include: ["src/**"],
      exclude: ["src/**/*.test.{ts,tsx}", "src/api/schema.gen.ts"],
      reportsDirectory: "./coverage",
      reporter: ["json-summary", "text-summary"],
    },
  },
  server: {
    port: 5173,
    // DEFAULT TO BACKEND: forward every path to uvicorn EXCEPT the two classes
    // that must stay with Vite. `bypass` returning the request URL tells Vite to
    // SKIP proxying and let its own middleware (dev assets) or the
    // spaIndexFallback plugin (SPA routes) serve it. Returning undefined proxies
    // to uvicorn (a real backend route, or an unknown path -> FastAPI real 404).
    // The `bypass` is what makes a bare "^/" target safe: without it the
    // catch-all would also swallow /@vite, /@id, /@fs, /src, /node_modules and
    // the HMR requests, breaking dev.
    // Preserve the incoming Origin: APP_BASE_URL is the browser-facing Vite
    // origin, and the HTTP CSRF check compares against it.
    proxy: {
      "^/": {
        target: BACKEND,
        changeOrigin: true,
        ws: false,
        bypass(req) {
          const url = (req.url ?? "/").split("?")[0];
          return shouldServeViaVite(url) ? req.url : undefined;
        },
      },
    },
  },
});
