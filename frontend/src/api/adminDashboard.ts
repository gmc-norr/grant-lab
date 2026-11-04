import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type AdminDashboard = components["schemas"]["AdminDashboardOut"];

export function useAdminDashboard() {
  return useQuery({
    queryKey: ["admin-dashboard"],
    queryFn: async () => unwrap(await api.GET("/api/admin/dashboard", {})),
    retry: false,
  });
}

export function useRerank() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/admin/rerank", {})),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-dashboard"] }),
  });
}

export function useSendDigest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/admin/digest/send", {})),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-dashboard"] }),
  });
}

export function useReenableDigestUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { user_id: number }) =>
      unwrap(await api.POST("/api/admin/digest/reenable", { body: { user_id: v.user_id } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-dashboard"] }),
  });
}

export function useFetchSource() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { slug: string }) =>
      unwrap(await api.POST("/api/admin/sources/{slug}/fetch", { params: { path: { slug: v.slug } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-dashboard"] }),
  });
}
