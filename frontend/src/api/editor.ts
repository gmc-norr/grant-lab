import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import { invalidateArtifact } from "./invalidateArtifact";

export type SaveSectionVars = {
  index: number;
  content: string;
  base_commit_sha: string;
  base_section_sha: string;
  base_branch: string;
};

/** GET /api/workspaces/{id}/draft — one read for Outline + editor + pills. */
export function useDraft(id: string, opts?: { enabled?: boolean }) {
  return useQuery({
    queryKey: ["draft", id],
    retry: false,
    // Default true keeps every existing caller unchanged; the status bar passes
    // false so it never fetches an artifact off the cockpit, or one the middle
    // is not showing. Same queryKey, so when it IS enabled React Query dedupes
    // it with the panel's own query — no second request.
    enabled: opts?.enabled ?? true,
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
            base_branch: vars.base_branch,
          },
        }),
      ),
    onSuccess: () => invalidateArtifact(qc, id, "draft"),
  });
}

export type SaveSpecSectionVars = {
  index: number;
  content: string;
  base_commit_sha: string;
  base_section_sha: string;
  base_branch: string;
};

/** GET /api/workspaces/{id}/spec — outline + section bytes, citation-free. */
export function useSpec(id: string, opts?: { enabled?: boolean }) {
  return useQuery({
    queryKey: ["spec", id],
    retry: false,
    // Default true keeps every existing caller unchanged; the status bar passes
    // false so it never fetches an artifact off the cockpit, or one the middle
    // is not showing. Same queryKey, so when it IS enabled React Query dedupes
    // it with the panel's own query — no second request.
    enabled: opts?.enabled ?? true,
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
            base_branch: vars.base_branch,
          },
        }),
      ),
    onSuccess: () => invalidateArtifact(qc, id, "spec"),
  });
}

/** POST /api/workspaces/{id}/spec — idempotent skeleton init. Invalidates ['spec', id]. */
export function useInitSpec(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/api/workspaces/{workspace_id}/spec", { params: { path: { workspace_id: id } } })),
    onSuccess: () => invalidateArtifact(qc, id, "spec"),
  });
}

/** POST /api/workspaces/{id}/spec/lock — owner locks the spec. Invalidates ['spec', id]. */
export function useLockSpec(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/api/workspaces/{workspace_id}/spec/lock", { params: { path: { workspace_id: id } } })),
    onSuccess: () => invalidateArtifact(qc, id, "spec"),
  });
}

/** POST /api/workspaces/{id}/spec/unlock — owner unlocks the spec. Invalidates ['spec', id]. */
export function useUnlockSpec(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/api/workspaces/{workspace_id}/spec/unlock", { params: { path: { workspace_id: id } } })),
    onSuccess: () => invalidateArtifact(qc, id, "spec"),
  });
}
