import { describe, expect, it } from "vitest";

import { assignLanes, type GraphCommit, type LaneRow } from "./versionLanes";

const C = (sha: string, ...parents: string[]): GraphCommit => ({ sha, parents });

/** Columns a row's lines occupy at its BOTTOM edge, and at its TOP edge.
 *  Derived from the public output, so the checkers below test the contract and
 *  not the implementation's internals. */
function bottoms(r: LaneRow): number[] {
  return r.links.filter((l) => l.bottom != null).map((l) => l.bottom as number);
}
function tops(r: LaneRow): number[] {
  return r.links.filter((l) => l.top != null).map((l) => l.top as number);
}

/** CONTINUITY: consecutive SVG strips must join into unbroken lines. */
function continuityProblems(rows: LaneRow[]): string[] {
  const out: string[] = [];
  for (let i = 1; i < rows.length; i++) {
    const above = bottoms(rows[i - 1]);
    const here = tops(rows[i]);
    for (const t of here) {
      if (!above.includes(t)) out.push(`row ${i} enters col ${t}, nothing left there`);
    }
    for (const b of above) {
      if (!here.includes(b)) out.push(`row ${i - 1} left col ${b}, row ${i} receives nothing`);
    }
  }
  return out;
}

/** FIDELITY: every real parent edge is carried by one lane all the way to the
 *  parent's dot, and every dot consumes exactly one lane per child.
 *
 *  THIS is the gate. Continuity alone PASSES the buggy version that reuses an
 *  existing lane for a parent — the lines join up, one row too early. */
function fidelityProblems(commits: GraphCommit[], rows: LaneRow[]): string[] {
  const out: string[] = [];
  const rowOf = new Map(commits.map((c, i) => [c.sha, i]));

  const childEdges = new Map<string, number>();
  for (const c of commits) {
    for (const p of c.parents) childEdges.set(p, (childEdges.get(p) ?? 0) + 1);
  }

  for (let m = 0; m < rows.length; m++) {
    const want = childEdges.get(rows[m].sha) ?? 0;
    // A link that arrives from above and does NOT continue below is one that
    // ENDS at this dot. Do NOT compute this as "columns in tops but not in
    // bottoms": an ordinary commit continuing in its own column contributes an
    // incoming link AND an outgoing link on the SAME column, so that
    // formulation counts zero and reports a false problem on a straight line.
    const got = rows[m].links.filter((l) => l.top != null && l.bottom === null).length;
    if (want !== got) {
      out.push(`${rows[m].sha}: ${want} child edge(s) but ${got} lane(s) end at its dot`);
    }
  }

  for (let i = 0; i < commits.length; i++) {
    for (const p of commits[i].parents) {
      const m = rowOf.get(p);
      if (m === undefined) continue; // truncated tail: runs off the bottom, correct
      const carried = bottoms(rows[i]).some((j) => {
        for (let k = i + 1; k < m; k++) {
          if (!tops(rows[k]).includes(j) || !bottoms(rows[k]).includes(j)) return false;
        }
        return tops(rows[m]).includes(j);
      });
      if (!carried) out.push(`edge ${commits[i].sha}->${p}: absorbed before reaching its parent`);
    }
  }
  return out;
}

describe("assignLanes — worked cases", () => {
  it("an empty history yields no rows", () => {
    expect(assignLanes([])).toEqual([]);
  });

  it("a straight line uses one column", () => {
    const g = [C("A", "B"), C("B", "C"), C("C")];
    const rows = assignLanes(g);
    expect(rows.map((r) => r.lane)).toEqual([0, 0, 0]);
    expect(Math.max(...rows.map((r) => r.columns))).toBe(1);
    expect(continuityProblems(rows)).toEqual([]);
    expect(fidelityProblems(g, rows)).toEqual([]);
  });

  it("a fork keeps the second branch in its own column until the parent", () => {
    const g = [C("A", "C"), C("B", "C"), C("C")];
    const rows = assignLanes(g);
    expect(rows.map((r) => r.lane)).toEqual([0, 1, 0]);
    // B's line must still be alive at C's row. This is the assertion that fails
    // if step 3 reuses A's lane for C.
    expect(tops(rows[2]).sort()).toEqual([0, 1]);
    expect(fidelityProblems(g, rows)).toEqual([]);
  });

  it("a two-parent merge opens a second column", () => {
    const g = [C("M", "X", "Y"), C("X", "Z"), C("Y", "Z"), C("Z")];
    const rows = assignLanes(g);
    expect(rows[0].lane).toBe(0);
    expect(bottoms(rows[0]).sort()).toEqual([0, 1]);
    expect(tops(rows[3]).sort()).toEqual([0, 1]);
    expect(continuityProblems(rows)).toEqual([]);
    expect(fidelityProblems(g, rows)).toEqual([]);
  });

  it("three concurrent branches use three columns", () => {
    const g = [C("A", "R"), C("B", "R"), C("C", "R"), C("R")];
    const rows = assignLanes(g);
    expect(Math.max(...rows.map((r) => r.columns))).toBe(3);
    expect(tops(rows[3]).sort()).toEqual([0, 1, 2]);
    expect(fidelityProblems(g, rows)).toEqual([]);
  });

  it("handles a merge whose second parent is an ancestor of its first", () => {
    const g = [C("M", "A", "B"), C("A", "B"), C("B")];
    const rows = assignLanes(g);
    expect(continuityProblems(rows)).toEqual([]);
    expect(fidelityProblems(g, rows)).toEqual([]);
  });

  it("handles an octopus merge", () => {
    const g = [C("M", "A", "B", "C"), C("A", "R"), C("B", "R"), C("C", "R"), C("R")];
    const rows = assignLanes(g);
    expect(bottoms(rows[0]).sort()).toEqual([0, 1, 2]);
    expect(fidelityProblems(g, rows)).toEqual([]);
  });

  it("a truncated tail leaves its lane open", () => {
    const g = [C("A", "B")]; // B is not in the set
    const rows = assignLanes(g);
    expect(rows).toHaveLength(1);
    expect(bottoms(rows[0])).toEqual([0]);
    expect(fidelityProblems(g, rows)).toEqual([]);
  });
});

describe("assignLanes — fidelity over generated graphs", () => {
  // Seeded, NOT Math.random: a failure has to be reproducible from its seed.
  function randomDag(n: number, seed: number): GraphCommit[] {
    let s = seed;
    const rnd = () => (s = (s * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
    const shas = Array.from({ length: n }, (_, i) => `c${i}`);
    return shas.map((sha, i) => {
      const available = n - i - 1;
      if (available === 0) return { sha, parents: [] };
      const want = Math.min(available, rnd() < 0.2 ? 2 : 1);
      const parents = new Set<string>();
      while (parents.size < want) parents.add(shas[i + 1 + Math.floor(rnd() * available)]);
      return { sha, parents: [...parents] };
    });
  }

  it("is continuous and faithful over 2000 generated DAGs", () => {
    const failures: string[] = [];
    for (let seed = 1; seed <= 2000; seed++) {
      const g = randomDag(14, seed);
      const rows = assignLanes(g);
      const problems = [...continuityProblems(rows), ...fidelityProblems(g, rows)];
      if (problems.length) failures.push(`seed ${seed}: ${problems[0]}`);
    }
    expect(failures).toEqual([]);
  });
});
