import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";

// ---------------------------------------------------------------------------
// Hand-written DTOs (the endpoints return `-> dict`; the generated types are
// loose — same approach as evaluator.ts which casts `as EvaluatorRun | null`).
// ---------------------------------------------------------------------------

export type ReviewItemKind =
  | "fit_check"
  | "persona_domain_skeptic"
  | "persona_methodology_critic"
  | "persona_translation_skeptic"
  | "persona_devils_advocate"
  | "consolidation";

export type ReviewItemStatus =
  | "queued"
  | "running"
  | "done"
  | "failed"
  | "cancelled"
  | "partial";

export type ReviewRunItem = {
  kind: ReviewItemKind;
  status: ReviewItemStatus;
  error_message: string | null;
  estimated_dollars: string | null;
};

export type ReviewRunBudget = {
  runs_used: number;
  runs_cap: number;
  dollars_used: string;
  dollars_cap: string;
};

export type ReviewRunStatus = "queued" | "running" | "done" | "failed" | "cancelled" | "partial";

export type ReviewRunDTO = {
  id: string;
  status: ReviewRunStatus;
  triggered_at: string | null;
  completed_at: string | null;
  draft_sha: string | null;
  fit_check_sha: string | null;
  rubric_review_sha: string | null;
  estimated_dollars: string | null;
  items: ReviewRunItem[];
  stale: boolean;
  budget: ReviewRunBudget;
};

export type ReviewFileDTO = {
  name: string;
  sha: string;
  markdown: string;
};

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
      ) as ReviewRunDTO | null,
    refetchInterval: (q) => {
      const s = (q.state.data as ReviewRunDTO | null)?.status;
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
      ) as ReviewRunDTO,
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
      ) as ReviewFileDTO,
  });
}
