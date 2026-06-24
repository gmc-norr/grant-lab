import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";

export function useTurns(id: string) {
  return useQuery({
    queryKey: ["turns", id],
    enabled: !!id,
    queryFn: async () =>
      unwrap(await api.GET("/api/workspaces/{workspace_id}/turns", { params: { path: { workspace_id: id } } })),
  });
}

export function useSubmitTurn(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: { content: string; reference_ids?: string[] }) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/turns", {
          params: { path: { workspace_id: id } },
          body: { content: vars.content, reference_ids: vars.reference_ids ?? [] },
        }),
      ),
    // onSettled (not onSuccess): a backstop 4xx/5xx (drafting-disabled race,
    // in-flight, busy) must ALSO refetch the read-model so the footer re-renders
    // the now-correct gate — the read-model is the authoritative gate surface.
    onSettled: () => qc.invalidateQueries({ queryKey: ["turns", id] }),
  });
}

export function useAckDisclosure(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/api/workspaces/{workspace_id}/disclosure", { params: { path: { workspace_id: id } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["turns", id] }),
  });
}

export function turnStreamUrl(id: string, jobId: string): string {
  return `/api/workspaces/${id}/turns/${jobId}/stream`;
}
