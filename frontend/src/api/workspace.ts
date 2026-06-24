import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";

/** GET /api/workspaces/{id}/cockpit-meta — header read-model for the status bar +
 *  title breadcrumb. Gated: the always-mounted StatusBar/TitleBar pass "" off-cockpit. */
export function useCockpitMeta(id: string) {
  return useQuery({
    queryKey: ["cockpit-meta", id],
    enabled: !!id,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/cockpit-meta", {
          params: { path: { workspace_id: id } },
        }),
      ),
  });
}

/** GET /api/workspaces/{id}/versions — recent commits for the Versions group. */
export function useVersions(id: string) {
  return useQuery({
    queryKey: ["versions", id],
    enabled: !!id,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/versions", {
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
