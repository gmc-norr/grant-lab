import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { Artifact } from "./proposals";

export type VersionRow = {
  commit_sha: string;
  committed_at: string;
  subject: string;
  /** Parent shas, first-parent first. Empty for a root commit and for the
   *  oldest row of a truncated history. Feeds versionLanes. */
  parents: string[];
  created_by: string | null;
  triggering_turn_id: string | null;
  /** The user's words from the turn that caused this version. Null wherever
   *  there is no turn — every `reconciled` row, and every commit with no DB row
   *  at all. Absence is ordinary. */
  turn_prompt: string | null;
};

export type VersionList = {
  versions: VersionRow[];
  truncated: boolean;
  history_available: boolean;
};

export type VersionContent = {
  commit_sha: string;
  artifact: string;
  markdown: string;
};

/** GET /api/workspaces/{id}/history?artifact=… — the git-sourced version list. */
export function useVersions(id: string, artifact: Artifact) {
  return useQuery({
    queryKey: ["versions", id, artifact],
    queryFn: async (): Promise<VersionList> => {
      const data = unwrap(
        await api.GET("/api/workspaces/{workspace_id}/history", {
          params: { path: { workspace_id: id }, query: { artifact } },
        }),
      ) as VersionList | undefined;
      // Never return undefined — React Query rejects it. `history_available:
      // false` is the honest default for "we got nothing back".
      return data ?? { versions: [], truncated: false, history_available: false };
    },
  });
}

/** GET /api/workspaces/{id}/history/{sha}?artifact=… — one version's markdown.
 *  Disabled while no sha is selected, so selecting nothing costs no request. */
export function useVersion(id: string, artifact: Artifact, sha: string | null) {
  return useQuery({
    enabled: sha != null,
    queryKey: ["version", id, artifact, sha],
    queryFn: async (): Promise<VersionContent | null> => {
      if (sha == null) return null;
      const data = unwrap(
        await api.GET("/api/workspaces/{workspace_id}/history/{commit_sha}", {
          params: {
            path: { workspace_id: id, commit_sha: sha },
            query: { artifact },
          },
        }),
      ) as VersionContent | undefined;
      return data ?? null;
    },
  });
}
