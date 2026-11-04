import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type DecisionCounts = components["schemas"]["CallDecisionCountsOut"];

// The three views release 1's index offers. A SUBSET of the wire's four
// (`DashboardView` in dashboard.ts): `archive` is the server's default, and an
// "everything ever ingested" tab is the firehose spec §5 exists to replace.
export type TriageView = "inbox" | "saved" | "hidden";

// Same shape as calls.ts's DecisionArgs, and for the same reason: a decision row
// cannot exist without a profile (422 without `profile_id`), so a caller with no
// profile in scope must not offer the action at all.
export type DecisionArgs = { id: number; profileId: number };

// The filters the counts endpoint accepts, WITH THE LIST'S NAMES. It takes
// `source`, `q` and `show_closed` so the rail and the feed agree under a filter,
// but deliberately NOT `min_score` — see the counts route's own docstring
// (D18). A min_score-filtered feed can therefore show fewer calls than the rail
// counts report; nothing can observe that, because no screen offers min_score.
export type CountsFilters = { source?: string; q?: string; show_closed?: boolean };

// NOTE THE NAMING SPLIT, and it is a WIRE FACT, not a choice made here (D14):
// the LIST takes `?profile=` (a string, degraded to null when unknown) while
// every decision route takes `?profile_id=` (an int, 422 when absent). The two
// response shapes differ for the same reason — `POST /hide` answers
// `CallHideOut{hidden}` because it predates the state column, `/save` answers
// `CallDecisionOut{state}`. Both names and both shapes are carried verbatim; do
// not "harmonise" them client-side.
export function decisionCountsKey(profileId: number | null, filters: CountsFilters = {}) {
  return ["decision-counts", profileId, filters] as const;
}

// ONE place that says what a decision invalidates, because it is FOUR caches and
// forgetting one is invisible: the feed refetches, the rail does not, and the
// counts sit one triage behind for the rest of the session (App.tsx's staleTime
// is 20 s). The third is the call page, keyed ["call", id, profile] — which
// ["calls"] does NOT prefix-match — so without it a call re-opened within 20 s
// of deciding showed the pre-decision state and offered Save/Hide again
// (review 2026-10-06 M9).
//
// The FOURTH is the board approval queue (review 2026-10-26 A8), and it is the
// easiest to miss because it is not a view of the deciding user's inbox at all:
// the queue applies the PROFILE OWNER's decisions (boards/queue.py passes
// `_decision_predicate("archive", user_id=owner_id, …)`), so a hide taken in
// triage removes that call from the admin's candidate list and from
// `waiting_count`. Nothing refreshed it, and the admin went on checklisting a
// call that had already left the set. ["board-queue"] is the PREFIX because the
// hook keys per board id and the deciding side does not know which boards the
// profile feeds.
//
// It is load-bearing for SAVE too, not only for hide — an earlier version of
// this comment said otherwise and was wrong (caught by the Task 19 review,
// 2026-10-28). `save_call` is an UPSERT whose conflict branch sets
// `state: "saved"` (web/api/calls.py, "a flip is a new decision (D9)"), and the
// archive predicate excludes exactly `state == "hidden"`
// (web/dashboard.py: `Call.id.notin_(... CallDecision.state == "hidden")`). So a
// save taken over an existing HIDE flips the row out of "hidden" and the call
// RE-ENTERS the admin's candidate set and `waiting_count`. That flip is
// reachable from the UI: CallDecisionActions renders Save whenever
// `state !== "saved"`, hidden calls included.
//
// The only hook here that genuinely cannot move the set is `useUnsaveCall`,
// which DELETEs a row whose state is "saved" — never "hidden". Its invalidation
// is an inexpensive no-op (the queue lives on /admin boards, not the triage
// feed), and that is the right price for one list. One list of what a decision
// invalidates is the point; a per-hook subset is how the fourth cache got
// forgotten in the first place.
//
// Every decision hook calls this in its OWN `onSuccess` — `useSaveCall` and
// `useUnsaveCall` below, and `useHideCall`/`useUnhideCall` in api/calls.ts, which
// import it. It is
// deliberately not passed per-call at a mutate() site: TanStack v5 runs
// mutate-level callbacks only while the observer still has listeners
// (query-core mutationObserver.js: `if (this.#mutateOptions &&
// this.hasListeners())`), so a decision followed by a navigation before the
// request lands silently loses them. A new decision hook needs this line; a new
// CALLER of an existing one needs nothing.
export function invalidateDecisions(qc: QueryClient) {
  return Promise.all([
    qc.invalidateQueries({ queryKey: ["calls"] }),
    qc.invalidateQueries({ queryKey: ["call"] }),
    qc.invalidateQueries({ queryKey: ["decision-counts"] }),
    qc.invalidateQueries({ queryKey: ["board-queue"] }),
  ]);
}

// Where inbox-zero comes from. `enabled` is load-bearing rather than cosmetic:
// `profile_id` is a REQUIRED int on this route, so firing it with null is a
// guaranteed 422 — and unwrap() turns a 401 into a full-page /login navigation,
// so a query that cannot succeed is not a harmless one.
export function useDecisionCounts(profileId: number | null, filters: CountsFilters = {}) {
  return useQuery({
    queryKey: decisionCountsKey(profileId, filters),
    enabled: profileId != null,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/calls/decisions/counts", {
          params: { query: { profile_id: profileId as number, ...filters } },
        }),
      ),
  });
}

// Save — the new half of the pair. DO UPDATE server-side, so saving a HIDDEN
// call moves it to saved rather than silently no-op'ing.
export function useSaveCall() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, profileId }: DecisionArgs) =>
      unwrap(
        await api.POST("/api/calls/{call_id}/save", {
          params: { path: { call_id: id }, query: { profile_id: profileId } },
        }),
      ),
    onSuccess: () => invalidateDecisions(qc),
  });
}

// Un-save: back to undecided, which is the ABSENCE of a row. Idempotent — 200
// even when there was nothing saved.
export function useUnsaveCall() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, profileId }: DecisionArgs) =>
      unwrap(
        await api.DELETE("/api/calls/{call_id}/save", {
          params: { path: { call_id: id }, query: { profile_id: profileId } },
        }),
      ),
    onSuccess: () => invalidateDecisions(qc),
  });
}
