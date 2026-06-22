import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

// ---------------------------------------------------------------------------
// DTOs are the generated server contract. The /review-runs endpoints now carry
// a response_model, so openapi-typescript emits these schemas — and tsc checks
// the Panel-review tab against the real wire shape (the previously-missing
// response_model is what let the dollar-as-string crash through). Deriving the
// literal unions from the generated component types keeps them from ever
// drifting from the backend enums (e.g. item status is `skipped`, not the
// run-only `partial`).
// ---------------------------------------------------------------------------

export type ReviewRunDTO = components["schemas"]["ReviewRunOut"];
export type ReviewFileDTO = components["schemas"]["ReviewFileOut"];
export type ReviewRunItem = components["schemas"]["ReviewRunItemOut"];
export type ReviewRunBudget = components["schemas"]["ReviewBudgetOut"];
export type ReviewItemKind = ReviewRunItem["kind"];
export type ReviewItemStatus = ReviewRunItem["status"];
export type ReviewRunStatus = ReviewRunDTO["status"];

// ---------------------------------------------------------------------------
// Query key factory
// ---------------------------------------------------------------------------
const reviewLatestKey = (id: string) => ["review-latest", id] as const;
const reviewFileKey = (id: string, runId: string, name: string) =>
  ["review-file", id, runId, name] as const;

// ---------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------

/** GET latest review run (or null). Polls at 4 s while queued/running. */
export function useLatestReviewRun(id: string) {
  return useQuery({
    queryKey: reviewLatestKey(id),
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/review-runs/latest", {
          params: { path: { workspace_id: id } },
        }),
      ),
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === "queued" || s === "running" ? 4000 : false;
    },
  });
}

/** POST to start a review run. Invalidates the latest-run query on success. */
export function useStartReviewRun(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/review-runs", {
          params: { path: { workspace_id: id } },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: reviewLatestKey(id) });
    },
  });
}

/** GET a single review artifact file by name. Only enabled when runId + sha are set. */
export function useReviewFile(id: string, runId: string | null, name: string) {
  return useQuery({
    queryKey: reviewFileKey(id, runId ?? "", name),
    enabled: runId != null && runId !== "",
    queryFn: async () =>
      unwrap(
        await api.GET(
          "/api/workspaces/{workspace_id}/review-runs/{run_id}/files/{name}",
          {
            params: { path: { workspace_id: id, run_id: runId!, name } },
          },
        ),
      ),
  });
}
