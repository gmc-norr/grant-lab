import { useMutation, useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type AuditIssue = components["schemas"]["AuditIssueOut"];
export type CitationAudit = components["schemas"]["CitationAuditOut"];
export type DeepCheck = components["schemas"]["DeepCheckOut"];

/** GET /api/workspaces/{id}/citation-audit — deterministic, free, member-gated. */
export function useCitationAudit(id: string) {
  return useQuery({
    queryKey: ["citation-audit", id],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/citation-audit", {
          params: { path: { workspace_id: id } },
        }),
      ),
  });
}

/** POST /api/workspaces/{id}/citation-audit/deep — owner-only semantic judge. */
export function useDeepCheck(id: string) {
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/citation-audit/deep", {
          params: { path: { workspace_id: id } },
        }),
      ),
  });
}
