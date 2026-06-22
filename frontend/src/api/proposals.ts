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
  // Refresh both the proposals list and the artifact content query. The content
  // query carries `locked`, so invalidating it after a 409 (spec locked, or row
  // no longer pending) re-disables the Accept button instead of leaving the user
  // re-clicking into the same conflict. Run on BOTH success and error: an accept
  // that errored means our cached view is stale exactly the same way.
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["proposed-changes", id, artifact] });
    qc.invalidateQueries({ queryKey: [artifact, id] });
    qc.invalidateQueries({ queryKey: ["problems", id] });
  };
  return useMutation({
    mutationFn: async (pcId: string) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/proposed-changes/{pc_id}/accept", {
          params: { path: { workspace_id: id, pc_id: pcId } },
        }),
      ) as ProposalRow,
    onSuccess: refresh,
    onError: refresh,
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
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["proposed-changes", id, artifact] });
      qc.invalidateQueries({ queryKey: ["problems", id] });
    },
  });
}

/** Visible prose_edit proposals across both artifacts — the Inbox tab badge. */
export function useInboxCount(id: string): number {
  const draft = useProposedChanges(id, "draft");
  const spec = useProposedChanges(id, "spec");
  const n = (rows?: ProposalRow[]) => (rows ?? []).filter((p) => p.kind === "prose_edit").length;
  return n(draft.data) + n(spec.data);
}
