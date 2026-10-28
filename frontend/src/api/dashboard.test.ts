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

// THE ALLOWED SET IS THE SERVER'S, not a guess: src/grant_lab/web/api/calls.py
// `get_calls` declares `sort: Literal["newest", "deadline"] = Query(default="newest")`
// and `page: int = Query(default=1, ge=1)`, which the generated client mirrors as
// `sort?: "newest" | "deadline"` (api/schema.gen.ts). Anything outside those is a
// 422 the feed cannot render around — the same degrade-don't-error rule `view`
// and `min_score` already follow above.
describe("dashboardParamsFrom sort sanitization", () => {
  it("keeps the two values the server accepts", () => {
    expect(P("sort=newest").sort).toBe("newest");
    expect(P("sort=deadline").sort).toBe("deadline");
  });

  it("drops an unknown sort so the server's own default applies", () => {
    // Re-verified by the 2026-10-26 review: `?sort=foo` sent 4 requests and
    // dead-ended after 7.5 s.
    expect(P("sort=foo").sort).toBeUndefined();
    expect(P("sort=").sort).toBeUndefined();
    expect(P("sort=NEWEST").sort).toBeUndefined();
    expect(P("sort=newest,deadline").sort).toBeUndefined();
    expect(P("").sort).toBeUndefined();
  });
});

describe("dashboardParamsFrom page sanitization", () => {
  it("keeps a positive integer page", () => {
    expect(P("page=1").page).toBe(1);
    expect(P("page=7").page).toBe(7);
  });

  it("degrades every non-positive-integer page to 1", () => {
    // `?page=abc` was sent on the wire as page=NaN before this.
    expect(P("page=abc").page).toBe(1);
    expect(P("page=0").page).toBe(1); // the server's bound is ge=1
    expect(P("page=-3").page).toBe(1);
    expect(P("page=1.5").page).toBe(1); // a float is a 422 on an `int` field
    expect(P("page=2abc").page).toBe(1);
    expect(P("page=").page).toBe(1);
    expect(P("").page).toBe(1);
    // Beyond Number.MAX_SAFE_INTEGER the value no longer round-trips as the
    // integer it was written as, so it is not a page anyone asked for.
    expect(P("page=99999999999999999999").page).toBe(1);
    expect(P("page=Infinity").page).toBe(1);
  });
});
