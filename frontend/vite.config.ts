import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig, type Connect, type PluginOption } from "vite";

import { isSpaRoute, isViteAsset, shouldServeViaVite } from "./src/dev-proxy";

const BACKEND = "http://localhost:8000";

// Serve index.html for SPA-owned document routes; let everything else fall
// through to the default-to-backend proxy below (-> uvicorn -> FastAPI real 404
// for unknowns). Built from the generated manifest, so Plan 3's flip is just a
// regenerate. configureServer runs in dev only (vite build never calls it).
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
        next(); // -> Vite dev assets, or the default-to-backend proxy below
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), spaIndexFallback()],
  // Build to frontend/dist (INSIDE the frontend dir — writable by the non-root
  // container builder; the package tree at ../src is NOT copied into the build
  // image). The packaging step (Task 6) copies dist/ -> src/call_manager/web/spa/.
  // Do NOT point outDir outside __dirname: in the container __dirname is /build,
  // so ../src/... would resolve to /src/... which is neither present nor writable.
  build: { outDir: resolve(__dirname, "dist"), emptyOutDir: true },
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
