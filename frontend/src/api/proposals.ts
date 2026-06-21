import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";

export type Artifact = "draft" | "spec";

export type ProposalRow = {
  id: string;
  status: "pending" | "accepted" | "rejected" | "superseded" | "stale";
  target_artifact: Artifact;
  section_index: number;
  kind: string;
  origin_kind: string;
  new_text: string | null;
  resulting_commit_sha: string | null;
  created_at: string | null;
  resolved_by: number | null;
  resolved_at: string | null;
};

/** GET /api/workspaces/{id}/proposed-changes?target_artifact=… — pending rows. */
export function useProposedChanges(id: string, artifact: Artifact) {
  return useQuery({
    queryKey: ["proposed-changes", id, artifact],
    queryFn: async () => {
      const data = unwrap(
        await api.GET("/api/workspaces/{workspace_id}/proposed-changes", {
          params: { path: { workspace_id: id }, query: { target_artifact: artifact } },
        }),
      ) as { items?: ProposalRow[] } | undefined;
      // Tolerate a missing/absent `items` (defensive: never let the query
      // function return undefined, which React Query rejects).
      return data?.items ?? [];
    },
  });
}

/** POST accept. Invalidates the proposals list AND the artifact content query. */
export function useAcceptProposal(id: string, artifact: Artifact) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (pcId: string) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/proposed-changes/{pc_id}/accept", {
          params: { path: { workspace_id: id, pc_id: pcId } },
        }),
      ) as ProposalRow,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["proposed-changes", id, artifact] });
      qc.invalidateQueries({ queryKey: [artifact, id] });
    },
  });
}

/** POST reject. Invalidates the proposals list. */
export function useRejectProposal(id: string, artifact: Artifact) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (pcId: string) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/proposed-changes/{pc_id}/reject", {
          params: { path: { workspace_id: id, pc_id: pcId } },
        }),
      ) as ProposalRow,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["proposed-changes", id, artifact] }),
  });
}
