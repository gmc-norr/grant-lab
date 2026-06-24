import {
  useDismissClaim,
  useLatestUncitedClaimsRun,
  useStartUncitedClaimsRun,
} from "../api/uncitedClaims";
import { type ProblemRow, useProblems } from "../api/problems";
import type { NavTarget, OnNavigate } from "./nav";

const CATEGORY_LABEL: Record<ProblemRow["category"], string> = {
  broken_citation: "Broken citations",
  criterion_gap: "Criteria gaps",
  over_length: "Over-length sections",
  coherence_drift: "Coherence",
  uncited_claim: "Uncited claims",
};
const CATEGORY_ORDER: ProblemRow["category"][] = [
  "broken_citation",
  "criterion_gap",
  "over_length",
  "coherence_drift",
  "uncited_claim",
];

function toNav(target: ProblemRow["target"]): NavTarget {
  return {
    top_tab: target?.top_tab ?? undefined,
    bottom_tab: target?.bottom_tab ?? undefined,
    section_index: target?.section_index ?? undefined,
  };
}

export function ProblemsPanel({ id, onNavigate }: { id: string; onNavigate: OnNavigate }) {
  const q = useProblems(id);
  const latest = useLatestUncitedClaimsRun(id);
  const start = useStartUncitedClaimsRun(id);
  const dismiss = useDismissClaim(id);
  const runStatus = latest.data?.status;
  const running = runStatus === "queued" || runStatus === "running";

  const runButton = (
    <button
      type="button"
      className="problems-run-uncited"
      disabled={running || start.isPending}
      onClick={() => start.mutate()}
    >
      {running ? "Checking…" : "Check uncited claims"}
    </button>
  );

  if (q.isError) {
    return (
      <div className="problems-panel">
        <div className="problems-toolbar">{runButton}</div>
        <p role="status" className="problems-error">
          Could not load problems.
        </p>
      </div>
    );
  }
  if (q.isLoading) {
    return (
      <div className="problems-panel">
        <div className="problems-toolbar">{runButton}</div>
        <p className="problems-loading">Loading…</p>
      </div>
    );
  }
  const data = q.data;
  const rows = data?.problems ?? [];
  const scorecardStale = (data?.scorecard_stale ?? []).length > 0;
  const uncitedStale = data?.uncited_claims_stale ?? false;
  const groups = CATEGORY_ORDER.map((cat) => ({
    cat,
    items: rows.filter((r) => r.category === cat),
  })).filter((g) => g.items.length > 0);

  return (
    <div className="problems-panel">
      <div className="problems-toolbar">{runButton}</div>
      {rows.length === 0 ? (
        <p className="problems-empty">No problems detected.</p>
      ) : (
        groups.map((g) => (
          <section key={g.cat} className="problems-group" aria-label={CATEGORY_LABEL[g.cat]}>
            <h3 className="problems-group-title">
              {CATEGORY_LABEL[g.cat]}
              {g.cat === "criterion_gap" && scorecardStale && (
                <span className="problems-stale"> · may be outdated</span>
              )}
              {g.cat === "uncited_claim" && uncitedStale && (
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
                  {r.category === "uncited_claim" && r.claim_hash && (
                    <button
                      type="button"
                      className="problem-dismiss"
                      disabled={dismiss.isPending}
                      onClick={() => dismiss.mutate(r.claim_hash as string)}
                    >
                      Dismiss
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </section>
        ))
      )}
    </div>
  );
}
