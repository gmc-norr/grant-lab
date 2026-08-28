import styles from "./CockpitPanels.module.css";
import type { DiffRow } from "./diff";

/** The one diff renderer. Was duplicated verbatim in ProposalReview.tsx and
 *  InlineProposalReview.tsx; a third copy was about to appear in the version
 *  compare view.
 *
 *  Keys are content-aware (tag + text + occurrence), taken from the better of
 *  the two originals: diff rows are stateless and recomputed each render, so an
 *  array index would be a foot-gun if the diff ever reorders, and repeated
 *  identical lines are ordinary in prose. */
export function DiffView({ rows }: { rows: DiffRow[] }) {
  const seen: Record<string, number> = {};
  return (
    <pre className={styles.proposalDiff}>
      {rows.map((r) => {
        const base = `${r.tag}:${r.text}`;
        const n = (seen[base] = (seen[base] ?? 0) + 1);
        const prefix = r.tag === "add" ? "+ " : r.tag === "del" ? "- " : "  ";
        return (
          <div key={`${base}#${n}`} data-diff={r.tag}>
            {prefix}
            {r.spans
              ? r.spans.map((s, k) => (
                  <span key={`${s.tag}:${k}`} data-span={s.tag}>
                    {s.text}
                  </span>
                ))
              : r.text}
          </div>
        );
      })}
    </pre>
  );
}
