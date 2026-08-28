import { describe, expect, it } from "vitest";

import { type DiffRow, lineDiff, pairRows, refineRuns, withinWordCap, wordDiff } from "./diff";

/** The refineRuns implementation as it stood BEFORE the blocks() extraction,
 *  copied verbatim from diff.ts at commit f3cb082c. It exists only to prove the
 *  extraction changed nothing, over generated input rather than the 16
 *  hand-picked cases in diff.test.ts and diff.refine.test.ts. */
function refineRunsBefore(rows: DiffRow[]): DiffRow[] {
  const out: DiffRow[] = [];
  let i = 0;
  while (i < rows.length) {
    if (rows[i].tag !== "del") {
      out.push(rows[i]);
      i++;
      continue;
    }
    const delStart = i;
    while (i < rows.length && rows[i].tag === "del") i++;
    const addStart = i;
    while (i < rows.length && rows[i].tag === "add") i++;
    const dels = rows.slice(delStart, addStart);
    const adds = rows.slice(addStart, i);
    const paired = Math.min(dels.length, adds.length);
    // One wordDiff per pair, not two: it used to be computed twice, once for
    // `.del` and once for `.add`, doubling the cost of the matrix capped below.
    // A pair over the cap yields `null` and stays two whole-line rows.
    const refined = dels
      .slice(0, paired)
      .map((d, k) => (withinWordCap(d.text, adds[k].text) ? wordDiff(d.text, adds[k].text) : null));
    for (let k = 0; k < dels.length; k++) {
      const r = k < paired ? refined[k] : null;
      out.push(r ? { ...dels[k], spans: r.del } : dels[k]);
    }
    for (let k = 0; k < adds.length; k++) {
      const r = k < paired ? refined[k] : null;
      out.push(r ? { ...adds[k], spans: r.add } : adds[k]);
    }
  }
  return out;
}

/** Seeded PRNG so a failure reproduces exactly. Deliberately not Math.random. */
function mulberry32(seed: number): () => number {
  return () => {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** A tiny alphabet is the point: with four words and short documents, runs of
 *  every shape actually occur — pure insertions, pure deletions, and runs
 *  longer on one side than the other. A large alphabet would make almost every
 *  line unique and exercise only one branch. */
function randomDoc(rand: () => number): string {
  const words = ["alpha", "bravo", "charlie", "delta"];
  const lineCount = 1 + Math.floor(rand() * 8);
  const lines: string[] = [];
  for (let i = 0; i < lineCount; i++) {
    const wordCount = 1 + Math.floor(rand() * 3);
    const line: string[] = [];
    for (let w = 0; w < wordCount; w++) line.push(words[Math.floor(rand() * words.length)]);
    lines.push(line.join(" "));
  }
  return lines.join("\n");
}

describe("the blocks() extraction is inert", () => {
  it("matches the pre-refactor refineRuns over 300 generated document pairs", () => {
    const rand = mulberry32(20260828);
    for (let n = 0; n < 300; n++) {
      const a = randomDoc(rand);
      const b = randomDoc(rand);
      const rows = lineDiff(a, b);
      expect(refineRuns(rows)).toEqual(refineRunsBefore(rows));
    }
  });

  it("covers every block shape across those documents", () => {
    // A guard on the generator itself: if it stopped producing insertions or
    // deletions, the test above would still pass while proving much less.
    const rand = mulberry32(20260828);
    const seen = new Set<string>();
    for (let n = 0; n < 300; n++) {
      const rows = lineDiff(randomDoc(rand), randomDoc(rand));
      for (let i = 0; i < rows.length; i++) {
        if (rows[i].tag === "del") seen.add(rows[i + 1]?.tag === "add" ? "replace" : "delete");
        else if (rows[i].tag === "add" && rows[i - 1]?.tag !== "del") seen.add("insert");
        else if (rows[i].tag === "same") seen.add("same");
      }
    }
    expect([...seen].sort()).toEqual(["delete", "insert", "replace", "same"]);
  });
});

describe("pairRows", () => {
  it("puts an unchanged line on both sides", () => {
    const pairs = pairRows([{ tag: "same", text: "alpha" }]);
    expect(pairs).toEqual([
      { left: { tag: "same", text: "alpha" }, right: { tag: "same", text: "alpha" } },
    ]);
  });

  it("puts a rewritten line's old text left and new text right, on one pair", () => {
    const pairs = pairRows(refineRuns(lineDiff("recruit 40 patients", "recruit 120 patients")));
    expect(pairs).toHaveLength(1);
    expect(pairs[0].left?.text).toBe("recruit 40 patients");
    expect(pairs[0].right?.text).toBe("recruit 120 patients");
  });

  it("carries the word spans through untouched", () => {
    const pairs = pairRows(refineRuns(lineDiff("recruit 40 patients", "recruit 120 patients")));
    expect(pairs[0].left?.spans?.filter((s) => s.tag === "del").map((s) => s.text)).toEqual(["40"]);
    expect(pairs[0].right?.spans?.filter((s) => s.tag === "add").map((s) => s.text)).toEqual(["120"]);
  });

  it("leaves the right side empty for a deletion with no partner", () => {
    // Three deletions, one addition: dels[0] pairs; dels[1] and dels[2] do not.
    const pairs = pairRows([
      { tag: "del", text: "one" },
      { tag: "del", text: "two" },
      { tag: "del", text: "three" },
      { tag: "add", text: "ONE" },
    ]);
    expect(pairs).toEqual([
      { left: { tag: "del", text: "one" }, right: { tag: "add", text: "ONE" } },
      { left: { tag: "del", text: "two" }, right: null },
      { left: { tag: "del", text: "three" }, right: null },
    ]);
  });

  it("leaves the left side empty for an addition with no partner", () => {
    const pairs = pairRows([
      { tag: "del", text: "one" },
      { tag: "add", text: "ONE" },
      { tag: "add", text: "TWO" },
    ]);
    expect(pairs).toEqual([
      { left: { tag: "del", text: "one" }, right: { tag: "add", text: "ONE" } },
      { left: null, right: { tag: "add", text: "TWO" } },
    ]);
  });

  it("treats a pure insertion as right-side-only, not as a rewrite", () => {
    // No `del` precedes these adds, so they are singles, not a change-block.
    const pairs = pairRows(refineRuns(lineDiff("alpha\ndelta", "alpha\nbravo\ncharlie\ndelta")));
    expect(pairs.map((p) => [p.left?.text ?? null, p.right?.text ?? null])).toEqual([
      ["alpha", "alpha"],
      [null, "bravo"],
      [null, "charlie"],
      ["delta", "delta"],
    ]);
  });

  it("returns nothing for an empty diff", () => {
    expect(pairRows([])).toEqual([]);
  });
});

describe("the pairing invariant from spec section 4.1", () => {
  it("never word-highlights a row it did not place opposite a partner", () => {
    const rand = mulberry32(19700101);
    for (let n = 0; n < 300; n++) {
      const refined = refineRuns(lineDiff(randomDoc(rand), randomDoc(rand)));
      const pairs = pairRows(refined);
      for (const row of refined) {
        if (!row.spans) continue;
        // Identity, not equality: pairRows passes the same objects through.
        const pair = pairs.find((p) => p.left === row || p.right === row);
        expect(pair, `a spanned row was not in any pair: ${JSON.stringify(row)}`).toBeDefined();
        expect(pair!.left).not.toBeNull();
        expect(pair!.right).not.toBeNull();
      }
    }
  });

  it("accounts for every row exactly once", () => {
    const rand = mulberry32(19700102);
    for (let n = 0; n < 100; n++) {
      const refined = refineRuns(lineDiff(randomDoc(rand), randomDoc(rand)));
      const pairs = pairRows(refined);
      const placed: DiffRow[] = [];
      for (const p of pairs) {
        if (p.left) placed.push(p.left);
        // A `same` row is the SAME object on both sides — count it once.
        if (p.right && p.right !== p.left) placed.push(p.right);
      }
      expect(placed).toHaveLength(refined.length);
      for (const row of refined) expect(placed).toContain(row);
    }
  });
});
