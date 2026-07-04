import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "../api/client";

export function useSaveVersion(id: string, artifact: "draft" | "spec") {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST(
          "/api/workspaces/{workspace_id}/collab/{artifact}/checkpoint",
          { params: { path: { workspace_id: id, artifact } } },
        ),
      ),
    // Refetch the artifact so the Outline reflects the checkpointed version (§7.4).
    onSuccess: () => qc.invalidateQueries({ queryKey: [artifact, id] }),
  });
}
