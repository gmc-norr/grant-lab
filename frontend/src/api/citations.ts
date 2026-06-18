import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";

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
    onSuccess: () => qc.invalidateQueries({ queryKey: ["draft", id] }),
  });
}
