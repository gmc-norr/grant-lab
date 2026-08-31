import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LANE_W, ROW_H, VersionLane } from "./VersionLane";
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
  });
});
