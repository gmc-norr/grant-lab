/** Column layout for the version graph. Pure: no React, no I/O, no imports.
 *
 *  Kept separate from the component on purpose. The rendering is easy and the
 *  layout is not; a pure function can be checked over thousands of generated
 *  graphs, which is the only way this logic gets real coverage — no repository
 *  in production has a fork yet.
 */

export type GraphCommit = { sha: string; parents: string[] };

/** One line crossing a row. `top`/`bottom` are column indices at the row's top
 *  and bottom edge; `null` means the line terminates at this row's dot. */
export type LaneLink = { top: number | null; bottom: number | null; color: number };

export type LaneRow = {
  sha: string;
  /** The column this commit's dot sits in. */
  lane: number;
  links: LaneLink[];
  /** Columns occupied at this row. The CALLER takes the maximum across all rows
   *  and uses that one width for every strip — sizing each row by its own value
   *  would slide the pills and subject sideways as the graph narrows. */
  columns: number;
};

/** How many distinct lane colours the theme provides (--app-lane-0..5). */
export const LANE_COLORS = 6;

function firstFree(lanes: (string | null)[]): number {
  const i = lanes.indexOf(null);
  return i === -1 ? lanes.length : i;
}

/** Assign a column to every commit and describe the lines crossing each row.
 *
 *  `commits` must be topologically ordered, children before parents — which is
 *  what `git log --topo-order` guarantees and what the history route now asks
 *  for.
 */
export function assignLanes(commits: GraphCommit[]): LaneRow[] {
  // lanes[i] = the sha column i is currently waiting for, or null if free.
  const lanes: (string | null)[] = [];
  const rows: LaneRow[] = [];

  for (const c of commits) {
    const above = lanes.slice();

    // 1. Which columns were waiting for me? Leftmost wins, so a long-lived
    //    branch keeps its column instead of drifting rightwards.
    const claimed: number[] = [];
    for (let j = 0; j < lanes.length; j++) if (lanes[j] === c.sha) claimed.push(j);
    const own = claimed.length ? claimed[0] : firstFree(lanes);

    // 2. Every claiming column's line ends at this dot.
    for (const j of claimed) lanes[j] = null;
    while (lanes.length <= own) lanes.push(null);

    // 3. Parents take columns. The first parent inherits this commit's own
    //    column so history reads as one continuous line.
    //
    //    DELIBERATELY no "reuse the column already waiting for this parent".
    //    Several columns may wait for the same sha; that is how two branches
    //    stay apart until they actually meet. Deduplicating here folds the
    //    second child's edge into the first child's column inside the SECOND
    //    CHILD'S OWN ROW, drawing a merge that never happened — and every line
    //    still joins up, so a continuity check does not notice.
    const outs: number[] = [];
    c.parents.forEach((p, k) => {
      const lane = k === 0 ? own : firstFree(lanes);
      while (lanes.length <= lane) lanes.push(null);
      lanes[lane] = p;
      outs.push(lane);
    });

    // 4. Let the graph narrow again once branches close.
    while (lanes.length && lanes[lanes.length - 1] === null) lanes.pop();
    const below = lanes.slice();

    // 5. Describe the lines.
    const links: LaneLink[] = [];
    for (let j = 0; j < above.length; j++) {
      // Passing straight through: occupied above by somebody other than me.
      if (above[j] != null && above[j] !== c.sha) {
        links.push({ top: j, bottom: j, color: j % LANE_COLORS });
      }
    }
    for (const j of claimed) links.push({ top: j, bottom: null, color: j % LANE_COLORS });
    for (const lane of outs) links.push({ top: null, bottom: lane, color: lane % LANE_COLORS });

    rows.push({
      sha: c.sha,
      lane: own,
      links,
      columns: Math.max(above.length, below.length, own + 1),
    });
  }

  return rows;
}
