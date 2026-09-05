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

// Hide/un-hide are PER PROFILE. `profile_id` is required server-side (422
// without it), because a decision row cannot exist without a profile — so both
// hooks take `{ id, profileId }` rather than a bare id, and a caller with no
// profile in scope must not offer the action at all.
export type DecisionArgs = { id: number; profileId: number };

// Idempotent hide. Invalidates the dashboard list cache; the CALLER navigates
// back to "/" (keeps this hook free of Router context, so it unit-tests with
// only a QueryClientProvider).
export function useHideCall() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, profileId }: DecisionArgs) =>
      unwrap(
        await api.POST("/api/calls/{call_id}/hide", {
          params: { path: { call_id: id }, query: { profile_id: profileId } },
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["calls"] }),
  });
}

// Idempotent unhide — succeeds even when the call was not hidden on this profile.
export function useUnhideCall() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, profileId }: DecisionArgs) =>
      unwrap(
        await api.DELETE("/api/calls/{call_id}/hide", {
          params: { path: { call_id: id }, query: { profile_id: profileId } },
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["calls"] }),
  });
}
