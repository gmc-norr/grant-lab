#!/usr/bin/env node
// Serve a built SPA directory over HTTP with an index.html fallback.
//
//   node scripts/serve_spa_static.mjs <dir> [port]
//
// Why this exists: frontend/e2e/match.spec.ts drives the production bundle in a
// real browser with the API mocked in-page. That needs no Postgres, uvicorn or
// seeded user (the real-backend smoke, scripts/e2e_run.sh, does), so this is
// zero-dependency Node stdlib and starts instantly.
//
// HOW IT DIFFERS FROM PRODUCTION, stated so nobody reads more into a green run:
// FastAPI's web/spa_serving.py serves index.html only for routes the generated
// manifest OWNS and returns a real JSON 404 for anything else. This serves
// index.html for any GET whose path has no file extension. It is NOT a test of
// the manifest's ownership rules; frontend/src/router.test.tsx and
// frontend/e2e/smoke.spec.ts cover those.
import { createReadStream, existsSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, resolve, sep } from "node:path";

const [, , dirArg, portArg] = process.argv;
if (!dirArg) {
  console.error("usage: serve_spa_static.mjs <dir> [port]");
  process.exit(2);
}
const ROOT = resolve(dirArg);
const PORT = Number(portArg ?? 4173);
if (!existsSync(join(ROOT, "index.html"))) {
  console.error(`FATAL: no index.html in ${ROOT} — build first`);
  process.exit(1);
}

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".map": "application/json; charset=utf-8",
  ".txt": "text/plain; charset=utf-8",
};

function send(res, status, file) {
  res.writeHead(status, { "content-type": TYPES[extname(file)] ?? "application/octet-stream" });
  createReadStream(file).pipe(res);
}

createServer((req, res) => {
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.writeHead(405).end();
    return;
  }
  const urlPath = decodeURIComponent((req.url ?? "/").split("?")[0]);
  // Traversal guard: normalize, then require the result to stay under ROOT.
  const candidate = resolve(join(ROOT, normalize(urlPath)));
  if (candidate !== ROOT && !candidate.startsWith(ROOT + sep)) {
    res.writeHead(403).end();
    return;
  }
  if (existsSync(candidate) && statSync(candidate).isFile()) {
    send(res, 200, candidate);
    return;
  }
  // A missing path WITH an extension is a genuine 404 (a broken asset reference
  // must not be masked by the shell — that is how a spec passes over a bundle
  // whose JS never loaded).
  if (extname(urlPath)) {
    res.writeHead(404, { "content-type": "text/plain" }).end("not found");
    return;
  }
  send(res, 200, join(ROOT, "index.html"));
}).listen(PORT, "127.0.0.1", () => {
  console.log(`serving ${ROOT} on http://127.0.0.1:${PORT} (SPA fallback)`);
});
