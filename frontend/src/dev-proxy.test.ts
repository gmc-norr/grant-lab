import { describe, expect, it } from "vitest";

import { isSpaRoute, isViteAsset, shouldServeViaVite } from "./dev-proxy";

describe("isSpaRoute (shipped manifest = ['/'] after the Plan-3 Task-9 flip)", () => {
  it("owns the root / exactly", () => {
    expect(isSpaRoute("/")).toBe(true);
  });
  it("does NOT own backend or unknown paths (they reach FastAPI's real 404)", () => {
    expect(isSpaRoute("/api/me")).toBe(false);
    expect(isSpaRoute("/login")).toBe(false);
    expect(isSpaRoute("/calls/1")).toBe(false); // un-ported call-detail page
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
});

describe("shouldServeViaVite (the proxy bypass decision)", () => {
  it("serves SPA routes + Vite assets via Vite; proxies everything else to uvicorn", () => {
    expect(shouldServeViaVite("/")).toBe(true); // SPA shell at the root
    expect(shouldServeViaVite("/@vite/client")).toBe(true); // dev asset
    expect(shouldServeViaVite("/login")).toBe(false); // backend route -> uvicorn (not Vite's index)
    expect(shouldServeViaVite("/api/me")).toBe(false); // backend route -> uvicorn
    expect(shouldServeViaVite("/calls/1")).toBe(false); // un-ported detail page -> uvicorn
    expect(shouldServeViaVite("/totally-unknown")).toBe(false); // unknown -> uvicorn real 404
  });
});
