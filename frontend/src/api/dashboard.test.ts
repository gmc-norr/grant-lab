import { describe, expect, it } from "vitest";

import { dashboardParamsFrom } from "./dashboard";

const P = (qs: string) => dashboardParamsFrom(new URLSearchParams(qs));

describe("dashboardParamsFrom q/min_score sanitization", () => {
  it("trims q and drops empty/whitespace", () => {
    expect(P("q=%20onco%20").q).toBe("onco");
    expect(P("q=%20%20").q).toBeUndefined();
    expect(P("").q).toBeUndefined();
  });

  it("parses a valid in-range min_score (including 0)", () => {
    expect(P("min_score=60").min_score).toBe(60);
    expect(P("min_score=0").min_score).toBe(0);
    expect(P("min_score=100").min_score).toBe(100);
  });

  it("rejects malformed or out-of-range min_score -> undefined", () => {
    expect(P("min_score=abc").min_score).toBeUndefined();
    expect(P("min_score=-5").min_score).toBeUndefined();
    expect(P("min_score=150").min_score).toBeUndefined();
    expect(P("").min_score).toBeUndefined();
  });
});
