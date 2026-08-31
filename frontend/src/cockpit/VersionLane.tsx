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
  // The bend's control heights must never run PAST an end that is the dot.
  // With a fixed `mid * 1.5`, a line arriving at the dot was drawn to 5.5px
  // BELOW it and then back up -- a 1.5px stub of the arriving branch's colour
  // poking out under a 4px dot. Same overshoot upwards for a line leaving one.
  const y1 = link.top != null ? mid * 0.5 : startY;
  const y2 = link.bottom != null ? mid * 1.5 : endY;
  return (
    `M ${topX} ${startY} L ${topX} ${y1} ` +
    `Q ${topX} ${mid} ${(topX + botX) / 2} ${mid} ` +
    `Q ${botX} ${mid} ${botX} ${y2} L ${botX} ${endY}`
  );
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
      {/* The dot takes its LANE's colour, not a provenance colour. Both were
          drawn from the same six-colour palette, so a dot could come out green
          on a blue line -- which reads as a rendering fault, because in a commit
          graph colour means "which branch". Provenance keeps its data-role hook
          and is stated in words in the hover card. */}
      <circle
        cx={x(row.lane)}
        cy={ROW_H / 2}
        r={DOT_R}
        data-role={role ?? "none"}
        fill={`var(--app-lane-${row.lane % LANE_COLORS})`}
        stroke={selected ? "var(--app-accent-bright)" : "var(--app-editor)"}
        strokeWidth={2}
      />
    </svg>
  );
}
