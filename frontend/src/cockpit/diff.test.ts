import { describe, expect, it } from "vitest";

import { lineDiff } from "./diff";

describe("lineDiff", () => {
  it("marks identical lines same", () => {
    expect(lineDiff("a\nb", "a\nb").map((r) => r.tag)).toEqual(["same", "same"]);
  });
  it("marks an added line", () => {
    expect(lineDiff("a", "a\nb").map((r) => r.tag)).toContain("add");
  });
  it("marks a deleted line", () => {
    expect(lineDiff("a\nb", "a").map((r) => r.tag)).toContain("del");
  });
  it("represents a replacement as del + add", () => {
    const rows = lineDiff("old line", "new line");
    expect(rows.some((r) => r.tag === "del" && r.text === "old line")).toBe(true);
    expect(rows.some((r) => r.tag === "add" && r.text === "new line")).toBe(true);
  });
});
