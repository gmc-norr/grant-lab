import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type AdminUser = components["schemas"]["AdminUserOut"];
export type AdminPendingUser = components["schemas"]["AdminPendingUserOut"];
export type DeletedWorkspaceRow = components["schemas"]["DeletedWorkspaceRow"];
export type DeletedWorkspaceDetail = components["schemas"]["DeletedWorkspaceDetailOut"];

export function useAdminUsers(page: number) {
  return useQuery({
    queryKey: ["admin-users", page],
    queryFn: async () => unwrap(await api.GET("/api/admin/users", { params: { query: { page } } })),
    retry: false,
  });
}

export function useAdminPendingUsers(page: number) {
  return useQuery({
    queryKey: ["admin-pending", page],
    queryFn: async () =>
      unwrap(await api.GET("/api/admin/users/pending", { params: { query: { page } } })),
    retry: false,
  });
}

export const DELETED_PER_PAGE = 50;

export function useAdminDeletedWorkspaces(page: number) {
  return useQuery({
    // page is in the key, or page 2 renders page 1's cache
    queryKey: ["admin-deleted-workspaces", page],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/admin/workspaces/deleted", {
          params: {
            query: { limit: DELETED_PER_PAGE, offset: (page - 1) * DELETED_PER_PAGE },
          },
        }),
      ),
    retry: false,
  });
}

export function useAdminDeletedWorkspace(workspaceId: string) {
  return useQuery({
    queryKey: ["admin-deleted-workspace", workspaceId],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/admin/workspaces/deleted/{workspace_id}", {
          params: { path: { workspace_id: workspaceId } },
        }),
      ),
    retry: false,
  });
}

export function useApproveUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { id: number; role?: "member" | "admin"; initial_password?: string }) =>
      unwrap(
        await api.POST("/api/admin/users/{user_id}/approve", {
          params: { path: { user_id: v.id } },
          body: { role: v.role ?? "member", initial_password: v.initial_password },
        }),
      ),
    // approve moves a user pending->active: refresh both queues.
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-pending"] });
      qc.invalidateQueries({ queryKey: ["admin-users"] });
    },
  });
}

export function useDenyUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { id: number }) =>
      unwrap(await api.POST("/api/admin/users/{user_id}/deny", { params: { path: { user_id: v.id } } })),
    // deny moves a user pending->disabled: refresh both queues.
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-pending"] });
      qc.invalidateQueries({ queryKey: ["admin-users"] });
    },
  });
}

export function useDisableUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { id: number }) =>
      unwrap(await api.POST("/api/admin/users/{user_id}/disable", { params: { path: { user_id: v.id } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-users"] }),
  });
}

export function useEnableUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { id: number }) =>
      unwrap(await api.POST("/api/admin/users/{user_id}/enable", { params: { path: { user_id: v.id } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-users"] }),
  });
}

export function useChangeRole() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { id: number; role: "member" | "admin" }) =>
      unwrap(
        await api.POST("/api/admin/users/{user_id}/role", {
          params: { path: { user_id: v.id } },
          body: { role: v.role },
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-users"] }),
  });
}

export function useResetPassword() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { id: number; new_password: string }) =>
      unwrap(
        await api.POST("/api/admin/users/{user_id}/reset-password", {
          params: { path: { user_id: v.id } },
          body: { new_password: v.new_password },
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-users"] }),
  });
}
