import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, redirectToLogin, unwrap } from "./client";
import type { components } from "./schema.gen";

type IngestResult = components["schemas"]["IngestResultOut"];

async function postMultipart(url: string, fd: FormData): Promise<IngestResult> {
  const res = await fetch(url, { method: "POST", body: fd, credentials: "same-origin" });
  if (res.status === 401) {
    // Mirror unwrap(): a session that expired mid-upload redirects to login
    // rather than failing silently in the UI.
    redirectToLogin();
    throw new ApiError("unauthorized", "session expired");
  }
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as
      | { error?: { code?: string; message?: string } }
      | null;
    throw new ApiError(body?.error?.code ?? "error", body?.error?.message ?? res.statusText);
  }
  return (await res.json()) as IngestResult;
}

function invalidateRefs(id: string) {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: ["references", id] });
}

export function useUploadReference(id: string) {
  const onSettled = invalidateRefs(id);
  return useMutation({
    mutationFn: ({ file, kind, autoClassify }: { file: File; kind: string; autoClassify: boolean }) => {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("kind", kind);
      fd.append("auto_classify", String(autoClassify));
      return postMultipart(`/api/workspaces/${id}/references`, fd);
    },
    onSettled,
  });
}

export function useFetchReference(id: string) {
  const onSettled = invalidateRefs(id);
  return useMutation({
    mutationFn: async ({ input, kind }: { input: string; kind: string }) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/references/fetch", {
          params: { path: { workspace_id: id } },
          body: { input, kind },
        }),
      ),
    onSettled,
  });
}

export function useApproveMetadata(id: string) {
  const onSettled = invalidateRefs(id);
  return useMutation({
    mutationFn: async (v: {
      referenceId: string;
      title: string;
      authors: string[];
      year?: string | null;
      venue?: string | null;
      doi?: string | null;
    }) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/references/{reference_id}/metadata", {
          params: { path: { workspace_id: id, reference_id: v.referenceId } },
          body: {
            title: v.title,
            authors: v.authors,
            year: v.year ?? null,
            venue: v.venue ?? null,
            doi: v.doi ?? null,
          },
        }),
      ),
    onSettled,
  });
}

export function useToggleWaiver(id: string) {
  const onSettled = invalidateRefs(id);
  return useMutation({
    mutationFn: async (v: { referenceId: string; waiver: boolean; reason?: string | null }) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/references/{reference_id}/waiver", {
          params: { path: { workspace_id: id, reference_id: v.referenceId } },
          body: { waiver: v.waiver, reason: v.reason ?? null },
        }),
      ),
    onSettled,
  });
}

export function useUpdateKind(id: string) {
  const onSettled = invalidateRefs(id);
  return useMutation({
    mutationFn: async (v: { referenceId: string; kind: string }) =>
      unwrap(
        await api.PATCH("/api/workspaces/{workspace_id}/references/{reference_id}/kind", {
          params: { path: { workspace_id: id, reference_id: v.referenceId } },
          body: { kind: v.kind },
        }),
      ),
    onSettled,
  });
}

export function useDeleteReference(id: string) {
  const onSettled = invalidateRefs(id);
  return useMutation({
    mutationFn: async (referenceId: string) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/references/{reference_id}/delete", {
          params: { path: { workspace_id: id, reference_id: referenceId } },
        }),
      ),
    onSettled,
  });
}

export function useReprocessReference(id: string) {
  const onSettled = invalidateRefs(id);
  return useMutation({
    mutationFn: async (referenceId: string) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/references/{reference_id}/reprocess", {
          params: { path: { workspace_id: id, reference_id: referenceId } },
        }),
      ),
    onSettled,
  });
}
