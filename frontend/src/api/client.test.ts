import { afterEach, describe, expect, it, vi } from "vitest";

import { unwrap } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("unwrap", () => {
  it("returns data on success", () => {
    expect(unwrap({ data: { ok: true }, error: undefined })).toEqual({ ok: true });
  });
  it("throws an ApiError carrying the envelope code on error", () => {
    expect(() =>
      unwrap({ data: undefined, error: { error: { code: "forbidden", message: "no" } } }),
    ).toThrowError(/forbidden/);
  });
  it("on a 403 account_inactive, full-page-navs to the carried redirect target", () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { assign } as never);
    expect(() =>
      unwrap({
        error: {
          error: {
            code: "account_inactive",
            message: "account not active",
            detail: { redirect: "/auth/no-access" },
          },
        },
        response: new Response(null, { status: 403 }),
      }),
    ).toThrowError(/account_inactive/);
    expect(assign).toHaveBeenCalledWith("/auth/no-access");
  });
  it("routes a pending account_inactive 403 to /auth/pending", () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { assign } as never);
    expect(() =>
      unwrap({
        error: {
          error: {
            code: "account_inactive",
            message: "account not active",
            detail: { redirect: "/auth/pending" },
          },
        },
        response: new Response(null, { status: 403 }),
      }),
    ).toThrowError(/account_inactive/);
    expect(assign).toHaveBeenCalledWith("/auth/pending");
  });
  it("a plain 403 (forbidden) does NOT navigate", () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { assign } as never);
    expect(() =>
      unwrap({
        error: { error: { code: "forbidden", message: "no" } },
        response: new Response(null, { status: 403 }),
      }),
    ).toThrowError(/forbidden/);
    expect(assign).not.toHaveBeenCalled();
  });

  it("throws ApiError on a non-2xx response with no JSON error body (e.g. 502 gateway)", () => {
    // openapi-fetch leaves data=undefined and error=undefined for non-JSON bodies;
    // unwrap must not silently return undefined and must instead surface the HTTP status.
    expect(() =>
      unwrap({
        data: undefined,
        error: undefined,
        response: new Response("Bad Gateway", { status: 502 }),
      }),
    ).toThrowError(/502/);
  });

  it("does NOT throw when data is present and response is ok", () => {
    const result = unwrap({
      data: { id: 1 },
      error: undefined,
      response: new Response(null, { status: 200 }),
    });
    expect(result).toEqual({ id: 1 });
  });
});
