import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type ActivityRow = components["schemas"]["ActivityRowOut"];
export type ActivityFeed = components["schemas"]["ActivityFeedOut"];

/** GET /api/workspaces/{id}/activity — curated reverse-chronological feed.
 *  Polled every 20s (lower urgency than Problems' 15s); recomputed per GET. */
export function useActivity(id: string) {
  return useQuery({
    queryKey: ["activity", id],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/activity", {
          params: { path: { workspace_id: id } },
        }),
      ) as ActivityFeed,
    refetchInterval: 20000,
  });
}
