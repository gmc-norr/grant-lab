import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";

/** GET /api/workspaces/{id}/settings — OWNER-GATED. Only the owner opens the
 *  settings modal; a non-owner would 403, so do not retry. */
export function useSettings(id: string) {
  return useQuery({
    queryKey: ["settings", id],
    enabled: !!id,
    retry: false,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/settings", {
          params: { path: { workspace_id: id } },
        }),
      ),
  });
}

export function useUpdateSettings(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: {
      title: string;
      status: string;
      deadline: string | null;
      citation_style: string;
    }) =>
      unwrap(
        await api.PATCH("/api/workspaces/{workspace_id}/settings", {
          params: { path: { workspace_id: id } },
          body: vars,
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["settings", id] });
      qc.invalidateQueries({ queryKey: ["cockpit-meta", id] });
    },
  });
}

export function useAddMember(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (email: string) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/members", {
          params: { path: { workspace_id: id } },
          body: { email },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["members", id] });
      qc.invalidateQueries({ queryKey: ["cockpit-meta", id] });
    },
  });
}

export function useRemoveMember(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (userId: number) =>
      unwrap(
        await api.DELETE("/api/workspaces/{workspace_id}/members/{user_id}", {
          params: { path: { workspace_id: id, user_id: userId } },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["members", id] });
      qc.invalidateQueries({ queryKey: ["cockpit-meta", id] });
    },
  });
}
