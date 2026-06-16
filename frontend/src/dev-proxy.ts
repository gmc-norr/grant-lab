// Dev-server route carve-out for vite.config.ts. Built from the generated
// manifest (routes.gen.ts), never a hand-edited regex, so Plan 3's flip to
// SPA_ROUTES=["/"] is just a regenerate. Mirrors is_spa_route /
// is_backend_route in src/call_manager/web/spa_routes.py.
//
// Dev posture: Vite forwards EVERYTHING to uvicorn by default and carves out
// only (a) SPA-owned page routes (served the SPA shell) and (b) Vite's own dev
// assets. An unknown / typo path therefore reaches FastAPI's real 404 in dev
// too — never Vite's HTML 404 and never index.html.
import { SPA_ROUTES } from "./routes.gen";

// Vite's own dev-asset prefixes — these must NEVER be forwarded to uvicorn
// (proxying them would break HMR and source loading).
export const VITE_DEV_PREFIXES = ["/@vite", "/@id", "/@fs", "/src", "/node_modules"];

export function isViteAsset(url: string): boolean {
  return VITE_DEV_PREFIXES.some((p) => url === p || url.startsWith(p + "/"));
}

// EXACT/owned-route match against the manifest — NOT a generic startsWith("/").
// Root "/" owns ONLY itself, so after Plan 3 flips SPA_ROUTES to ["/"] this does
// NOT collapse to "match everything" (a naive startsWith(r.replace(/\/$/,"")+"/")
// would, since "/" -> "" -> matches every path). Unknown/typo paths then fall
// through to the default proxy -> uvicorn -> FastAPI real 404.
export function isSpaRoute(path: string, routes: readonly string[] = SPA_ROUTES): boolean {
  return routes.some((r) =>
    r === "/" ? path === "/" : path === r || path.startsWith(r.replace(/\/$/, "") + "/"),
  );
}

// The dev front-door decision used by vite.config.ts's proxy `bypass`:
// true  -> serve via Vite (SPA shell for an owned route, or a Vite dev asset);
// false -> proxy to uvicorn (a real backend route, or unknown -> FastAPI 404).
export function shouldServeViaVite(url: string): boolean {
  return isViteAsset(url) || isSpaRoute(url);
}
