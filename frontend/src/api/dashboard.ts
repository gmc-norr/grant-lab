import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import type { components } from "./schema.gen";

export type DashboardOut = components["schemas"]["DashboardOut"];
export type CallItem = components["schemas"]["CallOut"];

import { api, unwrap } from "./client";

export type DashboardParams = {
  source?: string;
  sort?: "newest" | "deadline";
  profile?: string;
  page?: number;
  show_closed?: boolean;
};

export function dashboardParamsFrom(sp: URLSearchParams): DashboardParams {
  return {
    source: sp.get("source") ?? undefined,
    sort: (sp.get("sort") as "newest" | "deadline" | null) ?? undefined,
    profile: sp.get("profile") ?? undefined,
    page: sp.get("page") ? Number(sp.get("page")) : 1,
    show_closed: sp.get("show_closed") === "1",
  };
}

export function useDashboard(params: DashboardParams) {
  const qc = useQueryClient();
  const prevPending = useRef<number | null>(null);
  const q = useQuery({
    queryKey: ["calls", params],
    queryFn: async () => unwrap(await api.GET("/api/calls", { params: { query: params } })),
    placeholderData: keepPreviousData,
    // Poll ONLY while ranking is pending.
    refetchInterval: (query) => {
      const pending = (query.state.data as { ranking?: { pending?: number } } | undefined)?.ranking
        ?.pending;
      return pending && pending > 0 ? 5000 : false;
    },
  });
  // Drain refetch: when pending transitions >0 -> 0, refresh so new scores appear.
  useEffect(() => {
    const pending = (q.data as { ranking?: { pending?: number } } | undefined)?.ranking?.pending ?? 0;
    if (prevPending.current && prevPending.current > 0 && pending === 0) {
      qc.invalidateQueries({ queryKey: ["calls"] });
    }
    prevPending.current = pending;
  }, [q.data, qc]);
  return q;
}

// Explicit preference write (Plan 1: POST /api/me/default-profile). A TanStack
// mutation — awaited by the caller, invalidates ["me"] on success so the cached
// session reflects the new sticky default. NOT fire-and-forget.
export function useSetDefaultProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (profileId: number | null) =>
      unwrap(await api.POST("/api/me/default-profile", { body: { profile_id: profileId } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["me"] }),
  });
}
