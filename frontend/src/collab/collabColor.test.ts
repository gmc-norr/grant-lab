import { describe, expect, it } from "vitest";
import { colorForUserId, displayName } from "./collabColor";

describe("colorForUserId", () => {
  it("is deterministic and a valid hsl()", () => {
    const c = colorForUserId(42);
    expect(c).toMatch(/^hsl\(\d+(\.\d+)? \d+% \d+%\)$/);
    expect(colorForUserId(42)).toBe(c);
  });
  it("varies across ids", () => {
    expect(colorForUserId(1)).not.toBe(colorForUserId(2));
  });
});

describe("displayName", () => {
  it("prefers name, falls back to email local-part", () => {
    expect(displayName({ name: "Dr. Who", email: "who@umu.se" })).toBe("Dr. Who");
    expect(displayName({ name: null, email: "who@umu.se" })).toBe("who");
  });
  it("has a safe fallback when the user is unresolved", () => {
    expect(displayName(undefined)).toBe("Anonymous");
  });
  it("clamps very long labels", () => {
    expect(displayName({ name: "x".repeat(100), email: "x@umu.se" }).length).toBeLessThanOrEqual(32);
  });
});
