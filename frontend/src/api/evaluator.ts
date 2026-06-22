import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";

export type EvaluatorRun = {
  id: string;
  status: "queued" | "running" | "completed" | "failed";
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
};

export type ScorecardCriterion = {
  index: number;
  name: string;
  band: "strong" | "adequate" | "at_risk" | "gap";
  note: string;
  deep_link: { artifact: "draft" | "spec"; section_index: number } | null;
  weight: number;
  effective_weight: number;
};

export type ScorecardResponse = {
  scorecard: {
    criteria: ScorecardCriterion[];
    overall_band: "strong" | "adequate" | "at_risk" | "gap";
    summary: string;
  };
  evaluated_draft_sha: string | null;
  evaluated_spec_sha: string | null;
  completed_at: string | null;
  stale: boolean;
  stale_reasons: string[];
};

/** GET latest evaluator run (or null). Polls while queued/running. */
export function useLatestEvaluator(id: string) {
  return useQuery({
    queryKey: ["evaluator-latest", id],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/evaluator-runs/latest", {
          params: { path: { workspace_id: id } },
        }),
      ) as EvaluatorRun | null,
    refetchInterval: (q) => {
      const s = (q.state.data as EvaluatorRun | null)?.status;
      return s === "queued" || s === "running" ? 1500 : false;
    },
  });
}

/** GET the latest completed scorecard (or null). */
export function useScorecard(id: string) {
  return useQuery({
    queryKey: ["scorecard", id],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/scorecard", {
          params: { path: { workspace_id: id } },
        }),
      ) as ScorecardResponse | null,
  });
}

/** POST an evaluator run (create-or-return-active). Refresh the scorecard on completion. */
export function useStartEvaluator(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/evaluator-runs", {
          params: { path: { workspace_id: id } },
        }),
      ) as EvaluatorRun,
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: ["evaluator-latest", id] });
      if (run.status === "completed") {
        qc.invalidateQueries({ queryKey: ["scorecard", id] });
      }
    },
  });
}
