import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import react from "@vitejs/plugin-react";
import { loadEnv, type Connect, type PluginOption } from "vite";
// vitest/config's defineConfig is a superset of vite's that also accepts `test`.
import { defineConfig } from "vitest/config";

import { isSpaRoute, isViteAsset, shouldServeViaVite } from "./src/dev-proxy";

const BACKEND = "http://localhost:8000";

// Write the exact set of source modules Rollup put into the bundle, so a gate
// can assert on FILE PATHS instead of guessing from chunk names or grepping for
// strings. `generateBundle` runs after chunking, and every chunk's `modules` map
// is keyed by resolved module id — which is what makes this impossible to fool:
// renaming a chunk does not change it, and a module with no distinctive string
// still appears.
function moduleManifest(): PluginOption {
  return {
    name: "module-manifest",
    generateBundle(_opts, bundle) {
      const mods = new Set<string>();
      for (const out of Object.values(bundle)) {
        if (out.type !== "chunk") continue;
        for (const id of Object.keys(out.modules)) mods.add(id);
      }
      this.emitFile({
        type: "asset",
        fileName: "module-manifest.json",
        source: JSON.stringify([...mods].sort(), null, 2),
      });
    },
  };
}

// Serve index.html for SPA-owned document routes; let everything else fall
// through to the default-to-backend proxy below (-> uvicorn -> FastAPI real 404
// for unknowns). Route ownership comes from the generated manifest.
// configureServer runs in dev only (vite build never calls it).
// The tab title, per edition. index.html is SHARED — one file, one build entry
// — and the edition split happens at the router alias below, long after that
// <title> is fixed. Its own comment named this as the place to fix it if the
// two should differ, and 2026-09-08 decided they should: "Grant Lab" leads
// everywhere, and the match tab says which part of it you have open. That
// matters most with several tabs of the same app, which is the state a
// researcher triaging calls is actually in.
function editionTitle(edition: "match" | "full"): PluginOption {
  const title = edition === "match" ? "Grant Lab — Call Match" : "Grant Lab";
  // Anchored on the ATTRIBUTE, not on the tag's text. Measured 2026-09-08: the
  // first version matched the literal `<title>` tag, index.html's own comment
  // quoted that literal to explain the mechanism, String.replace takes the FIRST
  // occurrence — so the plugin rewrote the comment and left the title alone, in
  // both editions, with the build green. Prose can quote a tag; it does not
  // carry an attribute by accident.
  const ANCHOR = /<title data-edition-title>[^<]*<\/title>/g;
  return {
    name: "edition-title",
    transformIndexHtml(html: string) {
      const hits = html.match(ANCHOR)?.length ?? 0;
      // Throwing is the point. A silent no-op here ships the wrong tab title and
      // nothing anywhere goes red — which is exactly how the bug above survived
      // its own first build.
      if (hits !== 1) {
        throw new Error(
          `edition-title: expected exactly one <title data-edition-title> in index.html, found ${hits}`,
        );
      }
      return html.replace(ANCHOR, `<title data-edition-title>${title}</title>`);
    },
  };
}

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

// Which router the build compiles against. Resolved here, at config time, so
// exactly ONE router file enters the module graph — the excluded one is never
// read, let alone bundled. Doing this with a runtime ternary would not work:
// both routers construct their router at module scope, a side effect Rollup
// must preserve, so both graphs would ship. Pinned by scripts/check_match_bundle.mjs
// and by src/virtual-router.test.ts.
//
// It reads loadEnv(), NOT process.env, and that is load-bearing. Vite evaluates
// this config module BEFORE it reads any .env file (resolveConfig calls
// loadConfigFromFile ~70 lines ahead of loadEnv), and loadEnv writes only
// NODE_ENV/BROWSER/BROWSER_ARGS back into process.env — every VITE_* key from a
// .env file goes solely into the object that becomes import.meta.env. So
// `process.env.VITE_APP_EDITION` is undefined for a .env-configured build, while
// src/edition.ts (reading import.meta.env) sees "match": a match-branded shell
// rendered on the FULL router, with the whole cockpit graph bundled, silently.
// loadEnv returns the merged view — .env overridden by a real shell export —
// which is exactly what Vite feeds import.meta.env, so both readers agree.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, __dirname, "VITE_");
  const APP_EDITION = env.VITE_APP_EDITION === "match" ? "match" : "full";
  const ROUTER_FILE = APP_EDITION === "match" ? "src/router.simple.tsx" : "src/router.tsx";

  return {
  plugins: [react(), editionTitle(APP_EDITION), spaIndexFallback(), moduleManifest()],
  resolve: {
    alias: {
      "virtual:app-router": resolve(__dirname, ROUTER_FILE),
    },
  },
  // Build to frontend/dist (INSIDE the frontend dir — writable by the non-root
  // container builder; the package tree at ../src is NOT copied into the build
  // image). The packaging step (Task 6) copies dist/ -> src/grant_lab/web/spa/.
  // Do NOT point outDir outside __dirname: in the container __dirname is /build,
  // so ../src/... would resolve to /src/... which is neither present nor writable.
  build: { outDir: resolve(__dirname, "dist"), emptyOutDir: true },
  // Vitest: jsdom for component tests; jest-dom matchers loaded via the setup
  // file (kept in src/ so tsc picks up its `expect` type augmentation). `vite
  // build` ignores this `test` key.
  test: {
    // Derives the test-time VITE_APP_EDITION from the SAME config-time constant
    // the router alias uses, so the two readers cannot disagree under vitest.
    //
    // Be clear about what this line does NOT do. It is INERT — measured four ways
    // 2026-09-04: with it, without it, exported or not, the outcome is identical,
    // because vitest already feeds `import.meta.env` from the same loadEnv() view
    // the alias resolves from, and loadEnv merges process.env. It documents the
    // coupling; it does not create it.
    //
    // What it must NOT become is a literal "full". vitest's `test.env` OVERRIDES
    // an inherited process.env value in import.meta.env, so a literal breaks
    // `VITE_APP_EDITION=match pnpm run test src/virtual-router.test.ts` — the
    // alias resolves to router.simple.tsx while edition.ts still reads "full".
    //
    // The thing that actually closes the uncovered direction is the CI step:
    // src/virtual-router.test.ts is the only test that resolves the build-time
    // alias, and without a second explicitly-match invocation it asserts the FULL
    // direction and nothing else. See .github/workflows/ci.yml, right after the
    // bundle gate.
    env: { VITE_APP_EDITION: APP_EDITION },
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    // Scope unit tests to src/ so vitest does NOT collect the Playwright e2e
    // specs (frontend/e2e/*.spec.ts) — they use @playwright/test's runner +
    // browser fixtures and would crash under vitest. Playwright runs them via
    // its own config (playwright.config.ts).
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    exclude: ["**/node_modules/**", "src/**/*.probe.test.ts"],
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
    // origin, and both HTTP CSRF and WebSocket CSWSH checks compare against it.
    proxy: {
      "/api/ws": {
        target: BACKEND.replace(/^http/, "ws"),
        ws: true,
        changeOrigin: true,
      },
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
};
});
