import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import {
  type ScorecardCriterion,
  useLatestEvaluator,
  useScorecard,
  useStartEvaluator,
} from "../api/evaluator";

const BAND_CLASS: Record<ScorecardCriterion["band"], string> = {
  strong: "band-strong",
  adequate: "band-adequate",
  at_risk: "band-at-risk",
  gap: "band-gap",
};
const BAND_LABEL: Record<ScorecardCriterion["band"], string> = {
  strong: "Strong",
  adequate: "Adequate",
  at_risk: "At-risk",
  gap: "Gap",
};

export function ScorecardPanel({
  id,
  onNavigate,
}: {
  id: string;
  onNavigate: (artifact: "draft" | "spec", sectionIndex: number) => void;
}) {
  const latest = useLatestEvaluator(id);
  const start = useStartEvaluator(id);
  const scorecard = useScorecard(id);
  const status = latest.data?.status;
  const running = status === "queued" || status === "running";
  const qc = useQueryClient();
  useEffect(() => {
    if (status === "completed") qc.invalidateQueries({ queryKey: ["scorecard", id] });
  }, [status, id, qc]);

  const data = scorecard.data;
  const criteria = data
    ? [...data.scorecard.criteria].sort((a, b) => b.effective_weight - a.effective_weight)
    : [];

  return (
    <main className="scorecard-shell">
      <div className="scorecard-header">
        <h1>Scorecard</h1>
        {data && (
          <span className={`band-pill ${BAND_CLASS[data.scorecard.overall_band]}`}>
            {BAND_LABEL[data.scorecard.overall_band]}
          </span>
        )}
        <button type="button" disabled={running || start.isPending} onClick={() => start.mutate()}>
          {running ? "Evaluating…" : "Re-evaluate"}
        </button>
        {status === "failed" && (
          <span className="scorecard-error" role="status">
            {latest.data?.error ?? "Evaluation failed."}
          </span>
        )}
        {start.isError && (
          <span className="scorecard-error" role="status">
            {(start.error as Error)?.message ?? "Could not start evaluation."}
          </span>
        )}
      </div>

      {data && (
        <p className="scorecard-meta">
          evaluated {data.completed_at ? new Date(data.completed_at).toLocaleString() : ""}
          {data.stale && (
            <span className="scorecard-stale">
              {" "}
              · ⚠ changed since: {data.stale_reasons.join(", ")}
            </span>
          )}
        </p>
      )}

      {data?.scorecard.summary && (
        <p className="scorecard-summary">{data.scorecard.summary}</p>
      )}

      {!data && (
        <p className="scorecard-empty">No evaluation yet — Re-evaluate to generate a scorecard.</p>
      )}
      {data && criteria.length === 0 && (
        <p className="scorecard-empty">This call has no extracted criteria to evaluate.</p>
      )}

      <ul className="scorecard-list">
        {criteria.map((c) => (
          <li key={c.index} className="scorecard-row">
            <span className={`band-pill ${BAND_CLASS[c.band]}`}>{BAND_LABEL[c.band]}</span>
            <span className="scorecard-name">{c.name}</span>
            <span className="scorecard-weight">w {c.weight}</span>
            {c.deep_link && (
              <button
                type="button"
                className="scorecard-link"
                onClick={() => onNavigate(c.deep_link!.artifact, c.deep_link!.section_index)}
              >
                Go to §{c.deep_link.section_index}
              </button>
            )}
            <p className="scorecard-note">{c.note}</p>
          </li>
        ))}
      </ul>
    </main>
  );
}
