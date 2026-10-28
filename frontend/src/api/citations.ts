import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, api, unwrap } from "./client";
import { invalidateArtifact } from "./invalidateArtifact";

/** GET /api/workspaces/{id}/citations/{evId}/source — the cited source for the
 *  citation peek. Disabled until an evId is selected (clicking a pill sets it).
 *  evId is part of the queryKey so navigating prev/next refetches per citation. */
export function useCitationSource(id: string, evId: string | null) {
  return useQuery({
    queryKey: ["citation-source", id, evId],
    enabled: evId !== null,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/citations/{evidence_id}/source", {
          params: { path: { workspace_id: id, evidence_id: evId as string } },
        }),
      ),
  });
}

export type AddCitationVars = {
  section_index: number;
  base_commit_sha: string;
  base_section_sha: string;
  anchor: string;
  reference_id: string;
  chunk_index: number;
  evidence_quote: string;
  quote_start: number;
  quote_end: number;
  stance: string;
};

/** GET /api/workspaces/{id}/references — references for the cite picker. */
export function useReferences(id: string) {
  return useQuery({
    queryKey: ["references", id],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/references", {
          params: { path: { workspace_id: id } },
        }),
      ),
  });
}

/** GET /api/workspaces/{id}/references/{refId}/chunks — disabled until a ref is picked. */
export function useReferenceChunks(id: string, refId: string | null) {
  return useQuery({
    queryKey: ["reference-chunks", id, refId],
    enabled: refId !== null,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/references/{reference_id}/chunks", {
          params: { path: { workspace_id: id, reference_id: refId as string } },
        }),
      ),
  });
}

/** POST /api/workspaces/{id}/citations — human select-to-cite (direct commit). */
export function useAddCitation(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: AddCitationVars) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/citations", {
          params: { path: { workspace_id: id } },
          body: vars,
        }),
      ),
    // The audit too: ["citation-audit", id] is what the cockpit's "Citations N"
    // badge reads (api/audit.ts), and adding a citation is precisely a change to
    // it. Until this landed the only hook that invalidated the key was
    // useInsertAnnotation, so after a select-to-cite the badge kept its
    // pre-citation count until the user found the manual Re-run
    // (review 2026-10-26 A8).
    onSuccess: () =>
      Promise.all([
        invalidateArtifact(qc, id, "draft"),
        qc.invalidateQueries({ queryKey: ["citation-audit", id] }),
      ]),
    onError: (err) => {
      // 409 = the cited section's bytes changed underneath the user; refresh the
      // draft so they re-select against current text (spec). A 422 (bad quote /
      // anchor / chunk) leaves the section unchanged and the composer is preserved,
      // so we do NOT refetch on it.
      if (err instanceof ApiError && err.code === "conflict") {
        void invalidateArtifact(qc, id, "draft");
      }
    },
  });
}
