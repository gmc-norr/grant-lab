import { type ProblemRow, useProblems } from "../api/problems";
import type { NavTarget, OnNavigate } from "./nav";

const CATEGORY_LABEL: Record<ProblemRow["category"], string> = {
  broken_citation: "Broken citations",
  criterion_gap: "Criteria gaps",
  over_length: "Over-length sections",
  coherence_drift: "Coherence",
};
const CATEGORY_ORDER: ProblemRow["category"][] = [
  "broken_citation",
  "criterion_gap",
  "over_length",
  "coherence_drift",
];

function toNav(target: ProblemRow["target"]): NavTarget {
  return {
    top_tab: target?.top_tab ?? undefined,
    bottom_tab: target?.bottom_tab ?? undefined,
  };
}

export function ProblemsPanel({ id, onNavigate }: { id: string; onNavigate: OnNavigate }) {
  const q = useProblems(id);
  if (q.isError) {
    return (
      <p role="status" className="problems-error">
        Could not load problems.
      </p>
    );
  }
  if (q.isLoading) {
    return <p className="problems-loading">Loading…</p>;
  }
  const data = q.data;
  const rows = data?.problems ?? [];
  if (rows.length === 0) {
    return <p className="problems-empty">No problems detected.</p>;
  }
  const stale = (data?.scorecard_stale ?? []).length > 0;
  const groups = CATEGORY_ORDER.map((cat) => ({
    cat,
    items: rows.filter((r) => r.category === cat),
  })).filter((g) => g.items.length > 0);

  return (
    <div className="problems-panel">
      {groups.map((g) => (
        <section key={g.cat} className="problems-group" aria-label={CATEGORY_LABEL[g.cat]}>
          <h3 className="problems-group-title">
            {CATEGORY_LABEL[g.cat]}
            {g.cat === "criterion_gap" && stale && (
              <span className="problems-stale"> · may be outdated</span>
            )}
          </h3>
          <ul>
            {g.items.map((r, i) => (
              <li key={`${r.category}-${i}`} className={`problem-row severity-${r.severity}`}>
                <span className="problem-sev" aria-hidden="true">
                  {r.severity === "error" ? "⛔" : "⚠"}
                </span>
                <span className="problem-title">{r.title}</span>
                <span className="problem-detail">{r.detail}</span>
                {r.target && (
                  <button
                    type="button"
                    className="problem-act"
                    onClick={() => onNavigate(toNav(r.target))}
                  >
                    Go to {r.title}
                  </button>
                )}
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
