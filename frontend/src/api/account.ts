import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type AccountSettings = components["schemas"]["AccountSettingsOut"];

export function useAccountSettings() {
  return useQuery({
    queryKey: ["me-settings"],
    queryFn: async () => unwrap(await api.GET("/api/me/settings", {})),
    // House pattern (session.ts, calls.ts, editor.ts, …): no retry so an
    // unauthenticated load redirects via unwrap immediately, not after 3 retries.
    retry: false,
  });
}

export function useUpdateName() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (name: string) => unwrap(await api.POST("/api/me/name", { body: { name } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["me-settings"] }),
  });
}

export function useChangePassword() {
  return useMutation({
    mutationFn: async (vars: { current_password: string; new_password: string }) =>
      unwrap(await api.POST("/api/me/password", { body: vars })),
  });
}

export function useSaveSubscriptions() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (profile_ids: number[]) =>
      unwrap(await api.PUT("/api/me/subscriptions", { body: { profile_ids } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["me-settings"] }),
  });
}
