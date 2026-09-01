// Dev-server route carve-out for vite.config.ts. Built from the generated
// manifest (routes.gen.ts), never a hand-edited regex. Mirrors is_spa_route /
// is_backend_route in src/call_manager/web/spa_routes.py.
//
// Dev posture: Vite forwards EVERYTHING to uvicorn by default and carves out
// only (a) SPA-owned page routes (served the SPA shell) and (b) Vite's own dev
// assets. An unknown / typo path therefore reaches FastAPI's real 404 in dev
// too — never Vite's HTML 404 and never index.html.
import { SPA_ROUTES } from "./routes.gen";

// Non-namespaced Vite dev-asset prefixes that must NEVER be forwarded to uvicorn
// (proxying them would break source loading). Vite's "/@…" namespace is handled
// separately in isViteAsset (see below).
export const VITE_DEV_PREFIXES = ["/src", "/node_modules"];

export function isViteAsset(url: string): boolean {
  // Vite owns its entire "/@…" namespace — the HMR client (/@vite), virtual
  // modules (/@id), fs access (/@fs), and plugin-injected runtimes like
  // plugin-react's /@react-refresh. No backend route lives under /@, so serve
  // the whole namespace via Vite; enumerating individual /@ prefixes risked
  // omitting a plugin runtime (it did — /@react-refresh — blanking the SPA in dev).
  if (url.startsWith("/@")) return true;
  return VITE_DEV_PREFIXES.some((p) => url === p || url.startsWith(p + "/"));
}

// EXACT/owned-route match against the manifest — NOT a generic startsWith("/").
// Root "/" owns ONLY itself, so including it in SPA_ROUTES does NOT collapse to
// "match everything" (a naive startsWith(r.replace(/\/$/,"")+"/") would, since
// "/" -> "" -> matches every path). Unknown/typo paths then fall through to the
// default proxy -> uvicorn -> FastAPI real 404.
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
