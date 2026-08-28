import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { lineDiff, pairRows, refineRuns } from "./diff";
import { SideBySideDiffView } from "./SideBySideDiffView";

function renderCompare(a: string, b: string) {
  return render(
    <SideBySideDiffView
      pairs={pairRows(refineRuns(lineDiff(a, b)))}
      olderLabel="abc1234 · older"
      newerLabel="def5678 · newer"
    />,
  );
}

describe("SideBySideDiffView", () => {
  it("names which side is which", () => {
    renderCompare("alpha", "alpha");
    expect(screen.getByTestId("side-header-older").textContent).toBe("older · abc1234 · older");
    expect(screen.getByTestId("side-header-newer").textContent).toBe("newer · def5678 · newer");
  });

  it("marks both cells of a pair with the same pair index, fillers included", () => {
    // Position in two filtered lists is NOT a reliable way to match a pair's
    // halves — after the first insertion the indices diverge. The browser gate
    // in Task 6 relies on this attribute, so it is pinned here.
    const { container } = renderCompare("alpha\ndelta", "alpha\nbravo\ndelta");
    const indices = [...container.querySelectorAll("[data-pair]")].map((n) =>
      (n as HTMLElement).dataset.pair,
    );
    // Every index appears exactly twice: one left cell and one right cell.
    const counts = new Map<string, number>();
    for (const i of indices) counts.set(i!, (counts.get(i!) ?? 0) + 1);
    expect([...counts.values()].every((c) => c === 2)).toBe(true);
    for (const [i] of counts) {
      expect(container.querySelector(`[data-pair="${i}"][data-side="left"]`)).not.toBeNull();
      expect(container.querySelector(`[data-pair="${i}"][data-side="right"]`)).not.toBeNull();
    }
  });

  it("puts old text in the left column and new text in the right", () => {
    const { container } = renderCompare("recruit 40 patients", "recruit 120 patients");
    const left = container.querySelector('[data-side="left"][data-diff="del"]');
    const right = container.querySelector('[data-side="right"][data-diff="add"]');
    expect(left?.textContent).toBe("recruit 40 patients");
    expect(right?.textContent).toBe("recruit 120 patients");
  });

  it("highlights only the changed words, not the whole line", () => {
    const { container } = renderCompare("recruit 40 patients", "recruit 120 patients");
    const delSpans = [...container.querySelectorAll('[data-span="del"]')].map((n) => n.textContent);
    const addSpans = [...container.querySelectorAll('[data-span="add"]')].map((n) => n.textContent);
    expect(delSpans).toEqual(["40"]);
    expect(addSpans).toEqual(["120"]);
  });

  it("renders a filler cell, with no add or del tint, opposite an insertion", () => {
    const { container } = renderCompare("alpha\ndelta", "alpha\nbravo\ndelta");
    const fillers = container.querySelectorAll("[data-filler]");
    expect(fillers).toHaveLength(1);
    const filler = fillers[0];
    expect(filler.getAttribute("data-side")).toBe("left");
    expect(filler.textContent).toBe("");
    // The change is on the OTHER side of the row; tinting this cell would say
    // something was deleted here, and nothing was.
    expect(filler.getAttribute("data-diff")).toBeNull();
  });

  it("emits two cells per pair, so a pair is one grid row", () => {
    const { container } = renderCompare("alpha\nbravo", "alpha\nBRAVO");
    const pairs = pairRows(refineRuns(lineDiff("alpha\nbravo", "alpha\nBRAVO")));
    const cells = container.querySelectorAll("[data-side][data-cell]");
    expect(cells).toHaveLength(pairs.length * 2);
  });

  it("renders repeated identical lines without collapsing them", () => {
    const { container } = renderCompare("same\nsame\nsame", "same\nsame\nsame");
    const cells = container.querySelectorAll('[data-side="left"][data-cell]');
    expect(cells).toHaveLength(3);
  });
});
