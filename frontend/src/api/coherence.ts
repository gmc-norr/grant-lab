import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";

export type CoherenceRun = {
  id: string;
  status: "queued" | "running" | "completed" | "failed";
  n_proposals: number;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
};

/** GET latest coherence run (or null). Polls while queued/running. */
export function useLatestCoherence(id: string) {
  return useQuery({
    queryKey: ["coherence-latest", id],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/coherence-runs/latest", {
          params: { path: { workspace_id: id } },
        }),
      ) as CoherenceRun | null,
    refetchInterval: (q) => {
      const s = (q.state.data as CoherenceRun | null)?.status;
      return s === "queued" || s === "running" ? 1500 : false;
    },
  });
}

/** POST a coherence run (create-or-return-active). Refresh both inboxes when it completes. */
export function useStartCoherence(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/coherence-runs", {
          params: { path: { workspace_id: id } },
        }),
      ) as CoherenceRun,
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: ["coherence-latest", id] });
      if (run.status === "completed") {
        qc.invalidateQueries({ queryKey: ["proposed-changes", id, "spec"] });
        qc.invalidateQueries({ queryKey: ["proposed-changes", id, "draft"] });
      }
    },
  });
}
