import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type AnnotationsResponse = components["schemas"]["AnnotationsOut"];
export type ClaimSection = components["schemas"]["ClaimSectionOut"];
export type ClaimFinding = components["schemas"]["ClaimFindingOut"];

/** GET /api/workspaces/{id}/annotations — Claims read-model. Polls every 3s
 *  ONLY while some section has a pending analysis job, else not at all. */
export function useAnnotations(id: string) {
  return useQuery({
    queryKey: ["annotations", id],
    enabled: !!id,
    refetchInterval: (query) => {
      const data = query.state.data as AnnotationsResponse | undefined;
      const pending = data?.sections.some((s) => s.job.status === "pending");
      return pending ? 3000 : false;
    },
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/annotations", {
          params: { path: { workspace_id: id } },
        }),
      ),
  });
}

export function useInsertAnnotation(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (annotationId: number) =>
      unwrap(
        await api.POST(
          "/api/workspaces/{workspace_id}/annotations/{annotation_id}/insert",
          { params: { path: { workspace_id: id, annotation_id: annotationId } } },
        ),
      ),
    // Insert changed the document: refetch annotations + the draft + the
    // citation/proposed-change views (prefix-match invalidation).
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["annotations", id] });
      void qc.invalidateQueries({ queryKey: ["draft", id] });
      void qc.invalidateQueries({ queryKey: ["citation-audit", id] });
      void qc.invalidateQueries({ queryKey: ["proposals", id] });
    },
  });
}

export function useDismissAnnotation(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: { annotationId: number; reason: string }) =>
      unwrap(
        await api.POST(
          "/api/workspaces/{workspace_id}/annotations/{annotation_id}/dismiss",
          {
            params: { path: { workspace_id: id, annotation_id: vars.annotationId } },
            body: { reason: vars.reason },
          },
        ),
      ),
    onSettled: () => qc.invalidateQueries({ queryKey: ["annotations", id] }),
  });
}

export function useReanalyzeSection(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (sectionIndex: number) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/sections/{n}/reanalyze", {
          params: { path: { workspace_id: id, n: sectionIndex } },
        }),
      ),
    onSettled: () => qc.invalidateQueries({ queryKey: ["annotations", id] }),
  });
}
