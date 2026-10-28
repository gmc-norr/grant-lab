import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";
import { invalidateArtifact } from "./invalidateArtifact";

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
    //
    // The last key used to be ["proposals", id], which NO query in this
    // frontend registers — the proposals list is ["proposed-changes", id,
    // artifact] (api/proposals.ts), and ["proposals", id] appeared in exactly
    // one place in the whole tree: this line. So the Inbox pane kept its
    // pre-insert rows and the inserted claim still read as pending
    // (review 2026-10-26 A8). Two segments, not three, so one call covers the
    // draft and spec lists the cockpit mounts together.
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["annotations", id] });
      void invalidateArtifact(qc, id, "draft");
      void qc.invalidateQueries({ queryKey: ["citation-audit", id] });
      void qc.invalidateQueries({ queryKey: ["proposed-changes", id] });
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
