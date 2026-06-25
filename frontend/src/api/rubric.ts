import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { components } from "./schema.gen";
import { api, unwrap } from "./client";

export type RubricCriterion = components["schemas"]["CriterionOut"];
export type RubricUpdate = { criteria: RubricCriterion[]; funder_notes: string | null };

/** GET /api/workspaces/{id}/rubric — OWNER-GATED (non-owner 403; do not retry). */
export function useRubric(id: string) {
  return useQuery({
    queryKey: ["rubric", id],
    enabled: !!id,
    retry: false,
    queryFn: async () =>
      unwrap(await api.GET("/api/workspaces/{workspace_id}/rubric", { params: { path: { workspace_id: id } } })),
  });
}

/** GET /api/workspaces/{id}/setup-status — member-gated; polls until terminal so
 *  viewers auto-advance when the owner activates. */
export function useSetupStatus(id: string, enabled = true) {
  return useQuery({
    queryKey: ["setup-status", id],
    enabled: !!id && enabled,
    refetchInterval: (q) => {
      const s = (q.state.data as { status?: string } | undefined)?.status;
      return s && s !== "done" && s !== "failed" ? 2500 : false;
    },
    queryFn: async () =>
      unwrap(await api.GET("/api/workspaces/{workspace_id}/setup-status", { params: { path: { workspace_id: id } } })),
  });
}

export function useActivate(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: RubricUpdate) =>
      unwrap(await api.POST("/api/workspaces/{workspace_id}/activate", { params: { path: { workspace_id: id } }, body: vars })),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["cockpit-meta", id] });
      qc.invalidateQueries({ queryKey: ["turns", id] });
      qc.invalidateQueries({ queryKey: ["setup-status", id] });
    },
  });
}

export function useUpdateRubric(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: RubricUpdate) =>
      unwrap(await api.PUT("/api/workspaces/{workspace_id}/rubric", { params: { path: { workspace_id: id } }, body: vars })),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["rubric", id] });
      qc.invalidateQueries({ queryKey: ["scorecard", id] });
      qc.invalidateQueries({ queryKey: ["problems", id] });
    },
  });
}
