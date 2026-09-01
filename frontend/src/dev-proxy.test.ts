import { describe, expect, it } from "vitest";

import { isSpaRoute, isViteAsset, shouldServeViaVite } from "./dev-proxy";

describe("isSpaRoute (generated application-route manifest)", () => {
  it("owns the root, the /calls prefix, the /me prefix, and the /profiles prefix", () => {
    expect(isSpaRoute("/")).toBe(true);
    expect(isSpaRoute("/calls/1")).toBe(true);
    expect(isSpaRoute("/me")).toBe(true);
    expect(isSpaRoute("/profiles")).toBe(true);
  });
  it("does NOT own backend or unknown paths (they reach FastAPI's real 404)", () => {
    expect(isSpaRoute("/api/me")).toBe(false);
    expect(isSpaRoute("/login")).toBe(true);
    expect(isSpaRoute("/auth/pending")).toBe(true);
    expect(isSpaRoute("/auth/google/start")).toBe(false);
    expect(isSpaRoute("/totally-unknown")).toBe(false);
  });
});

describe("isSpaRoute branch coverage (explicit manifests)", () => {
  it("root owns ONLY itself — never collapses to match-everything", () => {
    // The load-bearing root special-case (mirrors the Python is_spa_route): a
    // naive startsWith(r.replace(/\/$/,"")+"/") would match every path once the
    // owned route is "/" ("/" -> "" -> matches all). Pin it.
    const flipped = ["/"];
    expect(isSpaRoute("/", flipped)).toBe(true);
    expect(isSpaRoute("/api/x", flipped)).toBe(false);
    expect(isSpaRoute("/calls/1", flipped)).toBe(false);
    expect(isSpaRoute("/made-up-page", flipped)).toBe(false);
  });
  it("a non-root owned route matches exactly + its client sub-paths only", () => {
    const nested = ["/app"];
    expect(isSpaRoute("/app", nested)).toBe(true);
    expect(isSpaRoute("/app/anything", nested)).toBe(true); // React-Router client sub-path
    expect(isSpaRoute("/appfoo", nested)).toBe(false); // sibling, NOT a sub-path of /app
    expect(isSpaRoute("/", nested)).toBe(false);
  });
});

describe("isViteAsset", () => {
  it("matches Vite dev-asset prefixes only", () => {
    expect(isViteAsset("/@vite/client")).toBe(true);
    expect(isViteAsset("/src/main.tsx")).toBe(true);
    expect(isViteAsset("/node_modules/.vite/deps/react.js")).toBe(true);
    expect(isViteAsset("/api/me")).toBe(false);
    expect(isViteAsset("/login")).toBe(false);
  });

  it("serves Vite's /@-namespaced runtime modules (e.g. plugin-react's /@react-refresh) via Vite", () => {
    // Regression: @vitejs/plugin-react injects `import "/@react-refresh"` into the
    // dev HTML. The carve-out listed /@vite + /@id + /@fs but omitted it, so the
    // proxy forwarded /@react-refresh to uvicorn and the SPA never mounted (blank
    // page in SPA_MODE=dev). No backend route lives under /@, so the whole Vite
    // namespace must be served by Vite — including future plugin-injected runtimes.
    expect(isViteAsset("/@react-refresh")).toBe(true);
    expect(isViteAsset("/@vite-plugin-whatever/x")).toBe(true);
    expect(shouldServeViaVite("/@react-refresh")).toBe(true);
  });
});

describe("shouldServeViaVite (the proxy bypass decision)", () => {
  it("serves SPA routes + Vite assets via Vite; proxies everything else to uvicorn", () => {
    expect(shouldServeViaVite("/")).toBe(true); // SPA shell at the root
    expect(shouldServeViaVite("/@vite/client")).toBe(true); // dev asset
    expect(shouldServeViaVite("/login")).toBe(true); // SPA route -> Vite
    expect(shouldServeViaVite("/auth/google/start")).toBe(false); // OAuth -> uvicorn (not shadowed)
    expect(shouldServeViaVite("/api/me")).toBe(false); // backend route -> uvicorn
    expect(shouldServeViaVite("/calls/1")).toBe(true); // owned detail route
    expect(shouldServeViaVite("/me")).toBe(true); // owned settings route
    expect(shouldServeViaVite("/profiles/5/edit")).toBe(true); // owned profiles route
    expect(shouldServeViaVite("/totally-unknown")).toBe(false); // unknown -> uvicorn real 404
  });
});
