import { useQuery } from "@tanstack/react-query";

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
