import { describe, expect, it } from "vitest";

import { MAX_DIFF_LINES, lineDiff, refineRuns, tooLargeToDiff, wordDiff } from "./diff";

describe("wordDiff", () => {
  it("marks only the changed word", () => {
    const { del, add } = wordDiff(
      "We will recruit 40 patients from the biobank.",
      "We will recruit 120 patients from the biobank.",
    );
    expect(del.filter((s) => s.tag === "del").map((s) => s.text)).toEqual(["40"]);
    expect(add.filter((s) => s.tag === "add").map((s) => s.text)).toEqual(["120"]);
  });

  it("round-trips: the spans rejoin to the original strings", () => {
    const before = "alpha beta gamma delta";
    const after = "alpha BETA gamma epsilon delta";
    const { del, add } = wordDiff(before, after);
    expect(del.map((s) => s.text).join("")).toBe(before);
    expect(add.map((s) => s.text).join("")).toBe(after);
  });

  it("refines a change inside a 1172-character paragraph", () => {
    // The gate as spec §11 states it. The live draft is paragraph-per-line
    // markdown whose longest line is 1172 characters; the failure this prevents
    // is a two-character edit rendering as a whole-paragraph rewrite.
    const filler = "lorem ipsum dolor sit amet ".repeat(43); // ~1161 chars
    const before = `We will recruit 40 patients. ${filler}`;
    const after = `We will recruit 120 patients. ${filler}`;
    expect(before.length).toBeGreaterThan(1100);

    const { del, add } = wordDiff(before, after);
    // BOTH unchanged and changed spans in the same row — not one whole-line row.
    expect(del.some((s) => s.tag === "same")).toBe(true);
    expect(del.filter((s) => s.tag === "del").map((s) => s.text)).toEqual(["40"]);
    expect(add.filter((s) => s.tag === "add").map((s) => s.text)).toEqual(["120"]);
  });

  it("merges neighbouring spans of the same tag", () => {
    const { del } = wordDiff("one two three", "one three");
    // "two" and the space beside it are one del span, not two.
    expect(del.filter((s) => s.tag === "del")).toHaveLength(1);
  });
});

describe("refineRuns", () => {
  it("pairs contiguous RUNS by index, not adjacent rows", () => {
    // THE gate. lineDiff prefers deletion on an LCS tie, so a two-line
    // replacement emits del,del,add,add. A rule that paired "a del immediately
    // followed by an add" would pair `bravo` with `ALPHA` — the wrong pair.
    const rows = lineDiff("alpha\nbravo\nzulu", "ALPHA\nBRAVO\nzulu");
    expect(rows.map((r) => r.tag)).toEqual(["del", "del", "add", "add", "same"]);

    const out = refineRuns(rows);
    expect(out.map((r) => r.tag)).toEqual(["del", "del", "add", "add", "same"]);
    // del[0] "alpha" refined against add[0] "ALPHA": entirely changed.
    expect(out[0].spans?.map((s) => s.tag)).toEqual(["del"]);
    expect(out[0].spans?.map((s) => s.text).join("")).toBe("alpha");
    // del[1] "bravo" refined against add[1] "BRAVO" — NOT against "ALPHA".
    expect(out[1].spans?.map((s) => s.text).join("")).toBe("bravo");
    expect(out[3].spans?.map((s) => s.text).join("")).toBe("BRAVO");
  });

  it("refines the one-line case (the degenerate del,add)", () => {
    const out = refineRuns(lineDiff("recruit 40 patients", "recruit 120 patients"));
    const del = out.find((r) => r.tag === "del");
    expect(del?.spans?.filter((s) => s.tag === "del").map((s) => s.text)).toEqual(["40"]);
  });

  it("leaves an unpaired row whole", () => {
    // Three deletions, one addition: dels[0] pairs, dels[1] and dels[2] do not.
    const rows: ReturnType<typeof lineDiff> = [
      { tag: "del", text: "one" },
      { tag: "del", text: "two" },
      { tag: "del", text: "three" },
      { tag: "add", text: "ONE" },
    ];
    const out = refineRuns(rows);
    expect(out[0].spans).toBeDefined();
    expect(out[1].spans).toBeUndefined();
    expect(out[2].spans).toBeUndefined();
  });

  it("leaves same rows untouched", () => {
    const out = refineRuns(lineDiff("a\nb", "a\nb"));
    expect(out.every((r) => r.tag === "same" && r.spans === undefined)).toBe(true);
  });

  it("preserves row order and count", () => {
    const rows = lineDiff("a\nb\nc\nd", "a\nB\nC\nd");
    expect(refineRuns(rows).map((r) => r.text)).toEqual(rows.map((r) => r.text));
  });
});

describe("the word-matrix cap", () => {
  it("leaves a pair whole rather than diffing an unbounded token matrix", () => {
    // The 2,000-LINE ceiling does not bound this: `wordDiff` allocates a
    // (tokens+1)x(tokens+1) matrix PER PAIRED LINE, so a one-line document
    // passes tooLargeToDiff at any size. Measured on this exact algorithm:
    // 4,000 tokens = 1.1 s, 8,000 tokens = 4.3 s of blocked main thread, and
    // 16,000 exhausts a 4 GB heap. Markdown serialises a pasted paragraph to
    // ONE line, so this is reachable from the editor.
    const huge = Array.from({ length: 1200 }, (_, i) => `w${i}`).join(" ");
    const rows = refineRuns([
      { tag: "del", text: huge },
      { tag: "add", text: huge.replace("w5 ", "W5 ") },
    ]);
    expect(rows).toHaveLength(2);
    expect(rows[0].spans).toBeUndefined();
    expect(rows[1].spans).toBeUndefined();
  });

  it("still refines a pair just under the cap", () => {
    const ok = Array.from({ length: 300 }, (_, i) => `w${i}`).join(" ");
    const rows = refineRuns([
      { tag: "del", text: ok },
      { tag: "add", text: ok.replace("w5 ", "W5 ") },
    ]);
    expect(rows[0].spans).toBeDefined();
    expect(rows[0].spans?.map((s) => s.text).join("")).toBe(ok);
  });
});

describe("tooLargeToDiff", () => {
  it("2,000 lines still diffs and 2,001 does not — the boundary is inclusive", () => {
    const ok = Array(MAX_DIFF_LINES).fill("x").join("\n");
    const tooBig = Array(MAX_DIFF_LINES + 1).fill("x").join("\n");
    expect(tooLargeToDiff(ok, ok)).toBe(false);
    expect(tooLargeToDiff(tooBig, ok)).toBe(true);
    expect(tooLargeToDiff(ok, tooBig)).toBe(true);
  });
});
