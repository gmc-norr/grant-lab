import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LANE_W, pathFor, ROW_H, VersionLane } from "./VersionLane";
import { assignLanes } from "./versionLanes";

describe("VersionLane", () => {
  const rows = assignLanes([
    { sha: "A", parents: ["C"] },
    { sha: "B", parents: ["C"] },
    { sha: "C", parents: [] },
  ]);

  it("sizes every strip to the LIST-WIDE column count, not its own", () => {
    // Row 0 occupies one column, row 1 occupies two. If each strip used its own
    // `columns`, the subject text beside them would slide sideways row to row.
    expect(rows[0].columns).toBe(1);
    expect(rows[1].columns).toBe(2);
    const { container: first } = render(
      <VersionLane row={rows[0]} columns={2} role={null} selected={false} />,
    );
    const { container: second } = render(
      <VersionLane row={rows[1]} columns={2} role={null} selected={false} />,
    );
    expect(first.querySelector("svg")?.getAttribute("width")).toBe(String(2 * LANE_W));
    expect(second.querySelector("svg")?.getAttribute("width")).toBe(String(2 * LANE_W));
  });

  it("is hidden from assistive technology", () => {
    const { container } = render(
      <VersionLane row={rows[0]} columns={2} role={null} selected={false} />,
    );
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });

  it("draws one path per link and exactly one dot", () => {
    const { container } = render(
      <VersionLane row={rows[2]} columns={2} role="user" selected={false} />,
    );
    expect(container.querySelectorAll("path")).toHaveLength(rows[2].links.length);
    expect(container.querySelectorAll("circle")).toHaveLength(1);
  });

  it("gives every path a real geometry, not just a count", () => {
    // Counting paths is a gate that cannot fail: replacing d={pathFor(...)}
    // with d="" leaves the count identical and erases every line. Measured
    // 2026-08-31 -- that mutation left all six tests in this file green.
    //
    // Row 2 is commit C, whose two children A and B occupy columns 0 and 1, so
    // one line arrives straight down its own column and one elbows in from the
    // column to its right.
    const { container } = render(
      <VersionLane row={rows[2]} columns={2} role="user" selected={false} />,
    );
    const d = [...container.querySelectorAll("path")].map((p) => p.getAttribute("d"));
    expect(d).toEqual([
      "M 7 0 L 7 11",
      "M 21 0 L 21 5.5 Q 21 11 14 11 Q 7 11 7 11 L 7 11",
    ]);
  });

  it("is exactly ROW_H tall so consecutive strips join up", () => {
    const { container } = render(
      <VersionLane row={rows[0]} columns={2} role={null} selected={false} />,
    );
    expect(container.querySelector("svg")?.getAttribute("height")).toBe(String(ROW_H));
  });

  it("colours the dot by its LANE, not by provenance", () => {
    // rows[1] sits in lane 1, so lane colour and the old provenance colour
    // differ. Asserting on rows[0] would pass either way -- lane 0's colour is
    // the same token the old "agent" mapping used.
    expect(rows[1].lane).toBe(1);
    const { container } = render(
      <VersionLane row={rows[1]} columns={2} role="agent" selected={false} />,
    );
    expect(container.querySelector("circle")).toHaveAttribute("fill", "var(--app-lane-1)");
    // Provenance is still exposed for CSS and tests, just not as the fill.
    expect(container.querySelector("circle")).toHaveAttribute("data-role", "agent");
  });

  it("rings the dot when the version is selected", () => {
    const { container: plain } = render(
      <VersionLane row={rows[0]} columns={1} role={null} selected={false} />,
    );
    const { container: picked } = render(
      <VersionLane row={rows[0]} columns={1} role={null} selected />,
    );
    expect(plain.querySelector("circle")).toHaveAttribute("stroke", "var(--app-editor)");
    expect(picked.querySelector("circle")).toHaveAttribute("stroke", "var(--app-accent-bright)");
    // Colour alone is not a ring. strokeWidth={0} paints nothing in either
    // state and left both assertions above green -- measured 2026-08-31.
    expect(picked.querySelector("circle")).toHaveAttribute("stroke-width", "2");
  });
});

describe("pathFor", () => {
  // The elbow geometry, checked directly rather than through a rendered row.
  // Until 2026-08-31 the two bend heights were fixed at mid*0.5 and mid*1.5
  // regardless of where the line ended, so a HALF link -- one whose other end
  // is the dot -- was drawn 5.5px past the dot and back again.
  it("stops at the dot on an arriving elbow, with no overshoot below it", () => {
    expect(pathFor({ top: 1, bottom: null, color: 1 }, 0)).toBe(
      "M 21 0 L 21 5.5 Q 21 11 14 11 Q 7 11 7 11 L 7 11",
    );
  });

  it("starts at the dot on a leaving elbow, with no overshoot above it", () => {
    expect(pathFor({ top: null, bottom: 1, color: 1 }, 0)).toBe(
      "M 7 11 L 7 11 Q 7 11 14 11 Q 21 11 21 16.5 L 21 22",
    );
  });

  it("still bends twice when the line crosses the whole row", () => {
    expect(pathFor({ top: 1, bottom: 0, color: 1 }, 5)).toBe(
      "M 21 0 L 21 5.5 Q 21 11 14 11 Q 7 11 7 16.5 L 7 22",
    );
  });

  it("draws a straight line when the column does not change", () => {
    expect(pathFor({ top: 2, bottom: 2, color: 2 }, 0)).toBe("M 35 0 L 35 22");
  });
});
