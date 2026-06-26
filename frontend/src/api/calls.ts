import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type CallDetail = components["schemas"]["CallDetailOut"];

// Detail read-model for /calls/:id. profile is the dashboard's effective profile,
// carried via ?profile= so the ranking shown matches the list. Keyed by both so
// switching profile refetches.
export function useCallDetail(id: number, profile?: number | null) {
  return useQuery({
    queryKey: ["call", id, profile ?? null],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/calls/{call_id}", {
          params: {
            path: { call_id: id },
            query: profile != null ? { profile: String(profile) } : {},
          },
        }),
      ),
    retry: 0,
  });
}

// Idempotent hide. Invalidates the dashboard list cache; the CALLER navigates
// back to "/" (keeps this hook free of Router context, so it unit-tests with
// only a QueryClientProvider).
export function useHideCall() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: number) =>
      unwrap(await api.POST("/api/calls/{call_id}/hide", { params: { path: { call_id: id } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["calls"] }),
  });
}
