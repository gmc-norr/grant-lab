export type DiffRow = { tag: "same" | "add" | "del"; text: string };

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
