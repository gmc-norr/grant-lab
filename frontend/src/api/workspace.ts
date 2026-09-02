import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";

/** GET /api/workspaces/{id}/cockpit-meta — header read-model for the status bar +
 *  title breadcrumb. Gated: the always-mounted StatusBar/TitleBar pass "" off-cockpit. */
export function useCockpitMeta(id: string) {
  return useQuery({
    queryKey: ["cockpit-meta", id],
    enabled: !!id,
    // A workspace that was soft-deleted or unshared answers 404 and will keep
    // answering 404: three retries only delay the error arm by several seconds
    // of spinner. Same reasoning as useMembers' expected 403 above.
    retry: false,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/cockpit-meta", {
          params: { path: { workspace_id: id } },
        }),
      ),
  });
}

/** GET /api/workspaces/{id}/members — OWNER-GATED. A non-owner member gets a 403,
 *  which is expected (they fall back to the member_count) — so do NOT retry it. */
export function useMembers(id: string) {
  return useQuery({
    queryKey: ["members", id],
    enabled: !!id,
    retry: false,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/members", {
          params: { path: { workspace_id: id } },
        }),
      ),
  });
}

/** GET /api/workspaces — the workspace list for the React front door (/cockpit).
 *  status: null = all; otherwise the lifecycle filter. */
export function useWorkspaces(status: string | null) {
  return useQuery({
    queryKey: ["workspaces", status ?? ""],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces", {
          params: { query: status ? { status } : {} },
        }),
      ),
  });
}

/** POST /api/workspaces — from-call create. Returns { id }.
 *
 *  This used to say "no invalidation: the list refetches on next mount", which
 *  is false inside the app's 20 s `staleTime` (App.tsx) with
 *  `refetchOnWindowFocus: false`: remounting an observer over a fresh cache
 *  entry serves the cache and fires no request. The user created a workspace,
 *  went back to the list, and it was not there. Prefix key on purpose —
 *  `useWorkspaces` keys by status filter, and every filter is now stale. */
export function useCreateWorkspace() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: {
      call_id: number;
      title: string;
      deadline?: string | null;
      profile_id?: number | null;
    }) => unwrap(await api.POST("/api/workspaces", { body: vars })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["workspaces"] }),
  });
}
