import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";

export type SaveSectionVars = {
  index: number;
  content: string;
  base_commit_sha: string;
  base_section_sha: string;
};

/** GET /api/workspaces/{id}/draft — one read for Outline + editor + pills. */
export function useDraft(id: string) {
  return useQuery({
    queryKey: ["draft", id],
    queryFn: async () =>
      unwrap(await api.GET("/api/workspaces/{workspace_id}/draft", { params: { path: { workspace_id: id } } })),
  });
}

/** POST /api/workspaces/{id}/sections/{index}. Invalidates the draft on success
 *  so the editor + Outline re-read the committed bytes. A 409 throws an ApiError
 *  with code "conflict" — the caller (Task 8) catches it to drive the
 *  buffer-preserving conflict prompt (Reload / Keep editing). */
export function useSaveSection(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: SaveSectionVars) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/sections/{index}", {
          params: { path: { workspace_id: id, index: vars.index } },
          body: {
            content: vars.content,
            base_commit_sha: vars.base_commit_sha,
            base_section_sha: vars.base_section_sha,
          },
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["draft", id] }),
  });
}

export type SaveSpecSectionVars = {
  index: number;
  content: string;
  base_commit_sha: string;
  base_section_sha: string;
};

/** GET /api/workspaces/{id}/spec — outline + section bytes, citation-free. */
export function useSpec(id: string) {
  return useQuery({
    queryKey: ["spec", id],
    queryFn: async () =>
      unwrap(await api.GET("/api/workspaces/{workspace_id}/spec", { params: { path: { workspace_id: id } } })),
  });
}

/** POST /api/workspaces/{id}/spec/sections/{index}. Invalidates ['spec', id]. */
export function useSaveSpecSection(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: SaveSpecSectionVars) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/spec/sections/{index}", {
          params: { path: { workspace_id: id, index: vars.index } },
          body: {
            content: vars.content,
            base_commit_sha: vars.base_commit_sha,
            base_section_sha: vars.base_section_sha,
          },
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["spec", id] }),
  });
}

/** POST /api/workspaces/{id}/spec — idempotent skeleton init. Invalidates ['spec', id]. */
export function useInitSpec(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/api/workspaces/{workspace_id}/spec", { params: { path: { workspace_id: id } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["spec", id] }),
  });
}
