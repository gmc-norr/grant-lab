import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DiffView } from "./DiffView";
import { lineDiff, refineRuns } from "./diff";

describe("DiffView", () => {
  it("tags each row so the CSS can colour it", () => {
    render(<DiffView rows={lineDiff("a\nb", "a\nB")} />);
    const rows = document.querySelectorAll("[data-diff]");
    expect(Array.from(rows).map((r) => r.getAttribute("data-diff"))).toEqual([
      "same",
      "del",
      "add",
    ]);
  });

  it("renders word spans when a row carries them", () => {
    render(<DiffView rows={refineRuns(lineDiff("recruit 40 patients", "recruit 120 patients"))} />);
    const dels = document.querySelectorAll('[data-diff="del"] span[data-span="del"]');
    expect(Array.from(dels).map((s) => s.textContent)).toEqual(["40"]);
    const adds = document.querySelectorAll('[data-diff="add"] span[data-span="add"]');
    expect(Array.from(adds).map((s) => s.textContent)).toEqual(["120"]);
  });

  it("renders a row with no spans as plain text", () => {
    render(<DiffView rows={[{ tag: "del", text: "gone" }]} />);
    expect(screen.getByText(/gone/)).toBeInTheDocument();
    expect(document.querySelectorAll("span[data-span]")).toHaveLength(0);
  });

  it("survives repeated identical lines", () => {
    // Content-aware keys disambiguate by occurrence; duplicate keys would make
    // React drop rows.
    render(<DiffView rows={[
      { tag: "del", text: "same text" },
      { tag: "del", text: "same text" },
    ]} />);
    expect(document.querySelectorAll('[data-diff="del"]')).toHaveLength(2);
  });
});
