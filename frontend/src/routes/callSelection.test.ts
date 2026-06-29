// frontend/src/routes/callSelection.test.ts
import { describe, expect, it } from "vitest";

import { deselectSearch, selectionSearch } from "./callSelection";

describe("selectionSearch", () => {
  it("sets selected and preserves existing params", () => {
    const out = new URLSearchParams(selectionSearch(new URLSearchParams("source=eu&page=2"), 7));
    expect(out.get("selected")).toBe("7");
    expect(out.get("source")).toBe("eu");
    expect(out.get("page")).toBe("2");
    expect(out.has("profile")).toBe(false);
  });

  it("pins the profile when profileId is provided", () => {
    const out = new URLSearchParams(selectionSearch(new URLSearchParams(""), 7, 3));
    expect(out.get("selected")).toBe("7");
    expect(out.get("profile")).toBe("3");
  });

  it("does not mutate the input params", () => {
    const input = new URLSearchParams("a=1");
    selectionSearch(input, 7, 3);
    expect(input.has("selected")).toBe(false);
    expect(input.has("profile")).toBe(false);
  });
});

describe("deselectSearch", () => {
  it("removes selected and keeps the rest", () => {
    const out = new URLSearchParams(deselectSearch(new URLSearchParams("selected=7&source=eu")));
    expect(out.has("selected")).toBe(false);
    expect(out.get("source")).toBe("eu");
  });
});
