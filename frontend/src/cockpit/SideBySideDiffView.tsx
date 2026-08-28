import { Fragment } from "react";

import type { DiffPair, DiffRow } from "./diff";
import styles from "./SideBySideDiffView.module.css";

function Cell({
  row,
  side,
  pair,
}: {
  row: DiffRow | null;
  side: "left" | "right";
  /** The pair's index. Both cells of a pair carry the same value, fillers
   *  included, so a layout check can compare the two halves of a REAL pair.
   *  Matching cells by position in two filtered lists silently misaligns after
   *  the first insertion or deletion. */
  pair: number;
}) {
  // No counterpart on this side. The cell still occupies the grid row — that
  // blank space is what keeps the following rows in step, and it carries no
  // add/del tint because nothing happened HERE; the change is opposite.
  if (!row) {
    return <div className={styles.filler} data-side={side} data-pair={pair} data-filler="" />;
  }
  return (
    <div
      className={styles.cell}
      data-side={side}
      data-pair={pair}
      data-cell=""
      data-diff={row.tag}
    >
      {row.spans
        ? row.spans.map((s, k) => (
            <span key={`${s.tag}:${k}`} data-span={s.tag}>
              {s.text}
            </span>
          ))
        : row.text}
    </div>
  );
}

export function SideBySideDiffView({
  pairs,
  olderLabel,
  newerLabel,
}: {
  pairs: DiffPair[];
  olderLabel: string;
  newerLabel: string;
}) {
  // Content-aware keys, matching DiffView's precedent: pairs are stateless and
  // recomputed each render, so an array index would be a foot-gun if the diff
  // ever reorders, and repeated identical lines are ordinary in prose.
  const seen: Record<string, number> = {};
  return (
    <div className={styles.scroll} data-testid="side-by-side-diff">
      <div className={styles.grid}>
        <div className={styles.header} data-side="left" data-testid="side-header-older">
          older · {olderLabel}
        </div>
        <div className={styles.header} data-side="right" data-testid="side-header-newer">
          newer · {newerLabel}
        </div>
        {pairs.map((p, i) => {
          const base = `${p.left?.tag ?? "-"}:${p.left?.text ?? ""}|${p.right?.tag ?? "-"}:${p.right?.text ?? ""}`;
          const n = (seen[base] = (seen[base] ?? 0) + 1);
          return (
            <Fragment key={`${base}#${n}`}>
              <Cell row={p.left} side="left" pair={i} />
              <Cell row={p.right} side="right" pair={i} />
            </Fragment>
          );
        })}
      </div>
    </div>
  );
}
