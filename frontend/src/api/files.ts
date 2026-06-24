import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, redirectToLogin, unwrap } from "./client";
import type { components } from "./schema.gen";

type FileOut = components["schemas"]["FileOut"];

async function postFile(url: string, file: File): Promise<FileOut> {
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch(url, { method: "POST", body: fd, credentials: "same-origin" });
  if (res.status === 401) {
    redirectToLogin();
    throw new ApiError("unauthorized", "session expired");
  }
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as
      | { error?: { code?: string; message?: string } }
      | null;
    throw new ApiError(body?.error?.code ?? "error", body?.error?.message ?? res.statusText);
  }
  return (await res.json()) as FileOut;
}

export function useFiles(id: string) {
  return useQuery({
    queryKey: ["files", id],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/files", {
          params: { path: { workspace_id: id } },
        }),
      ),
  });
}

export function useUploadFile(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => postFile(`/api/workspaces/${id}/files`, file),
    onSettled: () => qc.invalidateQueries({ queryKey: ["files", id] }),
  });
}

export function useDeleteFile(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (attachmentId: string) =>
      unwrap(
        await api.DELETE("/api/workspaces/{workspace_id}/files/{attachment_id}", {
          params: { path: { workspace_id: id, attachment_id: attachmentId } },
        }),
      ),
    onSettled: () => qc.invalidateQueries({ queryKey: ["files", id] }),
  });
}
