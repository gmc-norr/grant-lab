import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "../api/client";
import { invalidateArtifact } from "../api/invalidateArtifact";

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
    // The checkpoint IS a new version, so the version list must refresh with
    // the artifact. See api/invalidateArtifact.ts.
    onSuccess: () => invalidateArtifact(qc, id, artifact),
  });
}
