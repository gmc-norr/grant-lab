import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";

export type ProblemTarget = {
  top_tab: "draft" | "spec" | "scorecard" | null;
  bottom_tab: "inbox" | "problems" | null;
  section_index: number | null;
};

export type ProblemRow = {
  category: "broken_citation" | "over_length" | "criterion_gap" | "coherence_drift";
  severity: "error" | "warning";
  title: string;
  detail: string;
  target: ProblemTarget | null;
};

export type ProblemsResponse = {
  problems: ProblemRow[];
  counts: { error: number; warning: number };
  scorecard_stale: string[];
};

/** GET /api/workspaces/{id}/problems — the read-only health dashboard.
 *  POLLED every 15s (refetchInterval) + on window focus, so the badge/panel
 *  self-heal after background coherence/evaluator completion and draft/spec edits
 *  without wiring every write path. Accept/reject ALSO invalidate ["problems", id]
 *  (api/proposals.ts) for immediate feedback on the most common action. The endpoint
 *  recomputes from scratch on each GET; at single-proposal scale this is cheap. */
export function useProblems(id: string) {
  return useQuery({
    queryKey: ["problems", id],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/problems", {
          params: { path: { workspace_id: id } },
        }),
      ) as ProblemsResponse,
    refetchInterval: 15000,
  });
}
