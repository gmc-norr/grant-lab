// `spans` is OPTIONAL and added by refineRuns, so lineDiff's own output and
// both existing callers are unchanged.
export type DiffRow = { tag: "same" | "add" | "del"; text: string; spans?: Span[] };

// Minimal LCS line diff (dependency-free). Good enough for short prose sections;
// richer word-diff is deferred.
export function lineDiff(current: string, proposed: string): DiffRow[] {
  const a = current.split("\n");
  const b = proposed.split("\n");
  const n = a.length;
  const m = b.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const out: DiffRow[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ tag: "same", text: a[i] });
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      out.push({ tag: "del", text: a[i++] });
    } else {
      out.push({ tag: "add", text: b[j++] });
    }
  }
  while (i < n) out.push({ tag: "del", text: a[i++] });
  while (j < m) out.push({ tag: "add", text: b[j++] });
  return out;
}

// ---------------------------------------------------------------------------
// Word-level refinement.
//
// The corpus this serves is paragraph-per-line markdown: measured on the live
// draft, 37 lines with a longest line of 1172 characters. A pure line diff
// strikes through a whole paragraph to show a two-character edit.
// ---------------------------------------------------------------------------

export type Span = { tag: "same" | "add" | "del"; text: string };

/** Split into words AND the whitespace between them, so the spans rejoin to
 *  the original string exactly. `split` with a capturing group keeps the
 *  separators; the filter drops the empty strings it produces at the edges. */
function tokens(s: string): string[] {
  return s.split(/(\s+)/).filter((t) => t !== "");
}

function pushSpan(out: Span[], tag: Span["tag"], text: string): void {
  const last = out[out.length - 1];
  if (last && last.tag === tag) last.text += text;
  else out.push({ tag, text });
}

/** The same LCS as lineDiff, over word tokens instead of lines. Returns one
 *  span list for each side: the `del` list reconstructs `before`, the `add`
 *  list reconstructs `after`. */
export function wordDiff(before: string, after: string): { del: Span[]; add: Span[] } {
  const a = tokens(before);
  const b = tokens(after);
  const n = a.length;
  const m = b.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const del: Span[] = [];
  const add: Span[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      pushSpan(del, "same", a[i]);
      pushSpan(add, "same", b[j]);
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      pushSpan(del, "del", a[i++]);
    } else {
      pushSpan(add, "add", b[j++]);
    }
  }
  while (i < n) pushSpan(del, "del", a[i++]);
  while (j < m) pushSpan(add, "add", b[j++]);
  return { del, add };
}

/** Attach word-level spans to replaced lines.
 *
 *  Pairs contiguous RUNS by index, NOT adjacent rows. lineDiff prefers deletion
 *  on an LCS tie, so a two-line replacement emits del,del,add,add — a rule that
 *  paired "a del immediately followed by an add" would pair the SECOND deletion
 *  with the FIRST addition. Measured, not assumed:
 *
 *    lineDiff("alpha\nbravo\nzulu", "ALPHA\nBRAVO\nzulu")
 *      -> del:alpha del:bravo add:ALPHA add:BRAVO same:zulu
 *
 *  Rows are returned in the same order and count; only `spans` is added. A
 *  leftover row, where the runs are unequal in length, stays whole. */
/** Cap on `wordDiff`'s token matrix, per paired line.
 *
 *  `MAX_DIFF_LINES` bounds the LINE matrix, which is a different quadratic:
 *  `wordDiff` allocates `(tokens+1) x (tokens+1)` for EACH paired line, so a
 *  one-line document passes `tooLargeToDiff` at any size — and markdown
 *  serialises a pasted paragraph to one line. Measured on this algorithm:
 *  4,000 tokens = 1.1 s, 8,000 = 4.3 s of blocked main thread, 16,000 = heap
 *  exhaustion. 1e6 caps a pair at roughly 1,000 x 1,000, ~100 ms, which is
 *  ~850x the longest line in the live corpus (1,172 characters ≈ 190 tokens).
 *
 *  Over the cap the pair simply stays two whole-line rows — exactly the
 *  rendering that existed before word refinement, so the fallback needs no new
 *  state and no new UI. */
export const MAX_WORD_MATRIX = 1_000_000;

/** Whether a pair is small enough to refine to word level. */
export function withinWordCap(before: string, after: string): boolean {
  return tokens(before).length * tokens(after).length <= MAX_WORD_MATRIX;
}

export function refineRuns(rows: DiffRow[]): DiffRow[] {
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

/** lineDiff allocates a full (n+1)x(m+1) number matrix, quadratic in the line
 *  counts. Two 2,000-line documents is a 4M-entry matrix — the largest this
 *  approach should attempt. Today's largest document is 37 lines, so this is
 *  unreachable from real data and exists for the pathological case. Inclusive:
 *  exactly 2,000 still diffs. */
export const MAX_DIFF_LINES = 2000;

export function tooLargeToDiff(a: string, b: string): boolean {
  return a.split("\n").length > MAX_DIFF_LINES || b.split("\n").length > MAX_DIFF_LINES;
}
