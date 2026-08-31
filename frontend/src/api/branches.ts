import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";

export type BranchList = {
  branches: string[];
  current: string;
  /** Branch name -> the full sha its tip points at. Feeds the version graph's
   *  branch pills: a pill is drawn on the row whose sha a tip names. */
  tips: Record<string, string>;
};

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

export type CreateBranchArgs = {
  name: string;
  /** Start point. Omitted means HEAD, which is what the branch bar sends; the
   *  version graph's "Branch from here…" sends the row's sha. */
  fromSha?: string;
};

/** NOT built on `useBranchMutation`: create takes an object, switch and delete
 *  take a bare name. The four invalidations are repeated rather than abstracted
 *  — forgetting one of them is the failure this list exists to prevent, and a
 *  four-line duplication is cheaper to see than an indirection. */
export function useCreateBranch(id: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ name, fromSha }: CreateBranchArgs) => {
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/branches", {
          params: { path: { workspace_id: id! } },
          body: { name, from_sha: fromSha ?? null },
        }),
      );
    },
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
