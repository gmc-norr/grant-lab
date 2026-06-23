import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type UncitedClaimsRun = components["schemas"]["UncitedClaimsRunOut"];
export type UncitedClaimsLatest = components["schemas"]["UncitedClaimsLatestOut"];

const latestKey = (id: string) => ["uncited-claims-latest", id] as const;

/** GET latest uncited-claims run (or null). Polls at 4s while queued/running. */
export function useLatestUncitedClaimsRun(id: string) {
  return useQuery({
    queryKey: latestKey(id),
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/uncited-claims-runs/latest", {
          params: { path: { workspace_id: id } },
        }),
      ),
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === "queued" || s === "running" ? 4000 : false;
    },
  });
}

/** POST to start a run (create-or-return-active). Invalidates problems + latest. */
export function useStartUncitedClaimsRun(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/uncited-claims-runs", {
          params: { path: { workspace_id: id } },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: latestKey(id) });
      qc.invalidateQueries({ queryKey: ["problems", id] });
    },
  });
}

/** POST dismiss one claim by hash. Invalidates problems so the row disappears. */
export function useDismissClaim(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (claimHash: string) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/uncited-claims/{claim_hash}/dismiss", {
          params: { path: { workspace_id: id, claim_hash: claimHash } },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["problems", id] });
    },
  });
}
