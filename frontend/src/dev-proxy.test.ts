import { describe, expect, it } from "vitest";

import { isSpaRoute, isViteAsset, shouldServeViaVite } from "./dev-proxy";

describe("isSpaRoute (v1 manifest = ['/app'])", () => {
  it("owns /app exactly and its client sub-paths", () => {
    expect(isSpaRoute("/app")).toBe(true);
    expect(isSpaRoute("/app/anything")).toBe(true);
  });
  it("does NOT own backend or unknown paths (they reach FastAPI's real 404)", () => {
    expect(isSpaRoute("/")).toBe(false);
    expect(isSpaRoute("/api/me")).toBe(false);
    expect(isSpaRoute("/login")).toBe(false);
    expect(isSpaRoute("/totally-unknown")).toBe(false);
    expect(isSpaRoute("/appfoo")).toBe(false); // sibling, not a sub-path of /app
  });
});

describe("isSpaRoute after the Plan-3 flip to ['/']", () => {
  it("root owns ONLY itself — never collapses to match-everything", () => {
    const flipped = ["/"];
    expect(isSpaRoute("/", flipped)).toBe(true);
    expect(isSpaRoute("/api/x", flipped)).toBe(false);
    expect(isSpaRoute("/calls/1", flipped)).toBe(false);
    expect(isSpaRoute("/made-up-page", flipped)).toBe(false);
  });
});

describe("isViteAsset", () => {
  it("matches Vite dev-asset prefixes only", () => {
    expect(isViteAsset("/@vite/client")).toBe(true);
    expect(isViteAsset("/src/main.tsx")).toBe(true);
    expect(isViteAsset("/node_modules/.vite/deps/react.js")).toBe(true);
    expect(isViteAsset("/api/me")).toBe(false);
    expect(isViteAsset("/app")).toBe(false);
  });
});

describe("shouldServeViaVite (the proxy bypass decision)", () => {
  it("serves SPA routes + Vite assets via Vite; proxies everything else to uvicorn", () => {
    expect(shouldServeViaVite("/app")).toBe(true); // SPA shell
    expect(shouldServeViaVite("/@vite/client")).toBe(true); // dev asset
    expect(shouldServeViaVite("/login")).toBe(false); // backend route -> uvicorn (not Vite's index)
    expect(shouldServeViaVite("/api/me")).toBe(false); // backend route -> uvicorn
    expect(shouldServeViaVite("/totally-unknown")).toBe(false); // unknown -> uvicorn real 404
  });
});
