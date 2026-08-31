import { LANE_COLORS, type LaneLink, type LaneRow } from "./versionLanes";

/** Column pitch, and the row height every strip must match. Fixed, not derived
 *  from content: strips only join into continuous lines when every row is the
 *  same height. The subject text ellipsises rather than wrapping for the same
 *  reason. */
export const LANE_W = 14;
export const ROW_H = 22;

const DOT_R = 4;

function x(col: number): number {
  return col * LANE_W + LANE_W / 2;
}

/** The path for one line crossing this row.
 *
 *  A link with both ends set runs edge to edge. A link with one end null starts
 *  or finishes at the dot, so it is drawn to the row's middle — which is what
 *  makes a branch appear to leave from, or arrive at, the commit itself.
 *  Non-vertical segments bend through the mid-height, the same shape VS Code's
 *  source-control graph uses. */
export function pathFor(link: LaneLink, lane: number): string {
  const mid = ROW_H / 2;
  const topX = link.top != null ? x(link.top) : x(lane);
  const botX = link.bottom != null ? x(link.bottom) : x(lane);
  const startY = link.top != null ? 0 : mid;
  const endY = link.bottom != null ? ROW_H : mid;

  if (topX === botX) return `M ${topX} ${startY} L ${botX} ${endY}`;
  return (
    `M ${topX} ${startY} L ${topX} ${mid * 0.5} ` +
    `Q ${topX} ${mid} ${(topX + botX) / 2} ${mid} ` +
    `Q ${botX} ${mid} ${botX} ${mid * 1.5} L ${botX} ${endY}`
  );
}

/** Provenance colour for the dot. Kept as an explicit map rather than a lane
 *  colour: the lane says WHICH branch, the dot says WHO made the commit, and
 *  collapsing the two would lose one of them. */
function dotFill(role: string | null): string {
  if (role === "agent") return "var(--app-lane-0)";
  if (role === "user") return "var(--app-lane-1)";
  if (role === "reconciled") return "var(--app-lane-2)";
  return "var(--app-muted)";
}

/** One row of the commit graph. Purely decorative: the row's meaning is carried
 *  by the sha, subject and branch pills beside it, so this is aria-hidden. */
export function VersionLane({
  row,
  columns,
  role,
  selected,
}: {
  row: LaneRow;
  /** The LIST-WIDE maximum, passed in by VersionGraph. Never row.columns —
   *  per-row widths would slide the pills and subject sideways as the graph
   *  narrows. */
  columns: number;
  /** Provenance of the commit: "agent" | "user" | "reconciled" | null. */
  role: string | null;
  selected: boolean;
}) {
  const width = Math.max(columns, 1) * LANE_W;
  return (
    <svg
      width={width}
      height={ROW_H}
      aria-hidden="true"
      style={{ flex: `0 0 ${width}px`, display: "block" }}
    >
      {row.links.map((link, i) => (
        <path
          key={i}
          d={pathFor(link, row.lane)}
          fill="none"
          strokeWidth={2}
          stroke={`var(--app-lane-${link.color % LANE_COLORS})`}
        />
      ))}
      <circle
        cx={x(row.lane)}
        cy={ROW_H / 2}
        r={DOT_R}
        data-role={role ?? "none"}
        fill={dotFill(role)}
        stroke={selected ? "var(--app-accent-bright)" : "var(--app-editor)"}
        strokeWidth={2}
      />
    </svg>
  );
}
