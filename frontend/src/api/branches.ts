import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";

export type BranchList = { branches: string[]; current: string };

export function useBranches(id: string | null, opts?: { enabled?: boolean }) {
  return useQuery({
    queryKey: ["branches", id],
    enabled: (opts?.enabled ?? true) && !!id,
    queryFn: async () => {
      const data = unwrap(
        await api.GET("/api/workspaces/{workspace_id}/branches", {
          params: { path: { workspace_id: id! } },
        }),
      );
      return data as BranchList;
    },
  });
}

/** Everything that changes a branch invalidates the SAME set, because a switch
 *  changes the document, its history and its status bar at once. Listing them
 *  here rather than at each call site is what stops one of them being forgotten. */
function useBranchMutation(id: string | null, run: (name: string) => Promise<void>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: run,
    onSuccess: async () => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["branches", id] }),
        qc.invalidateQueries({ queryKey: ["draft", id] }),
        qc.invalidateQueries({ queryKey: ["spec", id] }),
        qc.invalidateQueries({ queryKey: ["versions", id] }),
      ]);
    },
  });
}

export function useCreateBranch(id: string | null) {
  return useBranchMutation(id, async (name) => {
    unwrap(
      await api.POST("/api/workspaces/{workspace_id}/branches", {
        params: { path: { workspace_id: id! } },
        body: { name },
      }),
    );
  });
}

export function useSwitchBranch(id: string | null) {
  return useBranchMutation(id, async (name) => {
    unwrap(
      await api.POST("/api/workspaces/{workspace_id}/branches/switch", {
        params: { path: { workspace_id: id! } },
        body: { name },
      }),
    );
  });
}

export function useDeleteBranch(id: string | null) {
  return useBranchMutation(id, async (name) => {
    unwrap(
      await api.DELETE("/api/workspaces/{workspace_id}/branches/{name}", {
        params: { path: { workspace_id: id!, name } },
      }),
    );
  });
}
