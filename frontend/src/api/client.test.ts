import { describe, expect, it } from "vitest";

import { unwrap } from "./client";

describe("unwrap", () => {
  it("returns data on success", () => {
    expect(unwrap({ data: { ok: true }, error: undefined })).toEqual({ ok: true });
  });
  it("throws an ApiError carrying the envelope code on error", () => {
    expect(() =>
      unwrap({ data: undefined, error: { error: { code: "forbidden", message: "no" } } }),
    ).toThrowError(/forbidden/);
  });
});
