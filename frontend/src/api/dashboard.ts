import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import type { components } from "./schema.gen";

export type DashboardOut = components["schemas"]["DashboardOut"];
export type CallItem = components["schemas"]["CallOut"];

import { api, unwrap } from "./client";

// Which slice of the calls list the server should return. `archive` is the
// server's own default and is what the FULL edition asks for by never setting
// the parameter — Dashboard.tsx passes dashboardParamsFrom(sp) straight through
// and its URL never carries ?view=, so `view` stays undefined there and the wire
// request is byte-identical to what it was before this field existed.
export type DashboardView = "archive" | "inbox" | "saved" | "hidden";

const VIEWS: readonly DashboardView[] = ["archive", "inbox", "saved", "hidden"];

// THE SERVER'S OWN SET, not a guess: src/grant_lab/web/api/calls.py `get_calls`
// declares `sort: Literal["newest", "deadline"] = Query(default="newest")`, and
// the generated client mirrors it as `sort?: "newest" | "deadline"`
// (schema.gen.ts). Anything else is a 422.
type DashboardSort = "newest" | "deadline";
const SORTS: readonly DashboardSort[] = ["newest", "deadline"];

export type DashboardParams = {
  source?: string;
  sort?: "newest" | "deadline";
  profile?: string;
  page?: number;
  show_closed?: boolean;
  q?: string;
  min_score?: number;
  view?: DashboardView;
};

export function dashboardParamsFrom(sp: URLSearchParams): DashboardParams {
  // Sanitize q/min_score here so the query key and controls only ever see
  // canonical values (a malformed deep link can't poison client state).
  const rawQ = (sp.get("q") ?? "").trim();
  const rawScore = sp.get("min_score");
  // THE ONLY PLACE `view` IS SANITISED, and it is sanitised rather than passed
  // through because FastAPI validates it against a Literal: `?view=inbo` off a
  // mistyped deep link would be a 422 the index cannot render around, so the
  // whole screen would dead-end. (`sort` below used to be the counter-example
  // this sentence pointed at; 2026-10-28 made it follow the same rule.) Dropping the
  // unknown value degrades to the server default instead — the same
  // degrade-don't-error choice `min_score` already makes here. A second copy of
  // this rule in the feed would be a sanitiser nothing reaches.
  const rawView = sp.get("view");
  const parsedScore = rawScore != null ? Number(rawScore) : NaN;
  // `sort` AND `page` are sanitised here too, for exactly the reason `view`
  // already was. Both were passed straight through until 2026-10-28, and the
  // review measured what that cost: `?sort=foo` off a stale bookmark is a 422
  // the index cannot render around, and `?page=abc` went on the wire LITERALLY
  // as `page=NaN` (Number("abc")). Each took four requests and ~7.5 s to reach a
  // dead end with no retry. Degrade to the server's own default instead.
  const rawSort = sp.get("sort");
  const rawPage = sp.get("page");
  const parsedPage = rawPage != null && rawPage !== "" ? Number(rawPage) : NaN;
  return {
    source: sp.get("source") ?? undefined,
    sort: SORTS.includes(rawSort as DashboardSort) ? (rawSort as DashboardSort) : undefined,
    profile: sp.get("profile") ?? undefined,
    // The server's bound is `ge=1` on an `int`, so 0, a negative, a float and a
    // non-number are all 422s. isSafeInteger rather than isInteger: past
    // 2**53 the value no longer round-trips as the integer it was written as,
    // so it is not a page anyone asked for.
    page: Number.isSafeInteger(parsedPage) && parsedPage >= 1 ? parsedPage : 1,
    show_closed: sp.get("show_closed") === "1",
    q: rawQ || undefined,
    min_score:
      Number.isFinite(parsedScore) && parsedScore >= 0 && parsedScore <= 100
        ? parsedScore
        : undefined,
    view: VIEWS.includes(rawView as DashboardView) ? (rawView as DashboardView) : undefined,
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
