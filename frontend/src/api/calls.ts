import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import { invalidateDecisions } from "./decisions";
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

// Hide is repeatable, but NOT a no-op the second time: the server upserts with
// DO UPDATE, so re-hiding an already-hidden call re-stamps `decided_at` and the
// call moves back to the TOP of the Hidden list (which is ordered by most recent
// decision). The CALLER navigates back to "/" (keeps this hook free of Router
// context, so it unit-tests with only a QueryClientProvider).
//
// BOTH caches, HERE, via the one `invalidateDecisions` (api/decisions.ts) —
// never as a per-call `onSuccess` at the mutate() site. TanStack v5 runs
// mutate-level callbacks only while the observer still has listeners
// (query-core mutationObserver.js: `if (this.#mutateOptions &&
// this.hasListeners())`), and triage is a keyboard loop: press `h`, click into
// the call before the POST lands, and the rail's invalidation was simply
// dropped — the counts then read one triage behind for the whole of App.tsx's
// 20 s staleTime. A hook-level onSuccess lives on the Mutation itself and needs
// no observer. In the FULL edition, where CallDetail also mounts this hook,
// there is no rail and no ["decision-counts"] observer at all, so that half is
// an inexpensive no-op rather than a wasted fetch.
export function useHideCall() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, profileId }: DecisionArgs) =>
      unwrap(
        await api.POST("/api/calls/{call_id}/hide", {
          params: { path: { call_id: id }, query: { profile_id: profileId } },
        }),
      ),
    onSuccess: () => invalidateDecisions(qc),
  });
}

// Idempotent unhide — succeeds even when the call was not hidden on this
// profile. Invalidates both caches at hook level, for the reason above.
export function useUnhideCall() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, profileId }: DecisionArgs) =>
      unwrap(
        await api.DELETE("/api/calls/{call_id}/hide", {
          params: { path: { call_id: id }, query: { profile_id: profileId } },
        }),
      ),
    onSuccess: () => invalidateDecisions(qc),
  });
}

// The state the caller BELIEVED it was annotating. The server 409s if the
// decision moved under it -- one user with two tabs is enough to hit this
// (spec D11), and the consequence of omitting it is a reason about hiding
// filed against a save.
export type NoteArgs = {
  id: number;
  profileId: number;
  note: string | null;
  expectedState: "saved" | "hidden";
};

// NOT invalidateDecisions, and that is the whole difference between this hook
// and the four above it: a note changes no count and moves no call between
// views, so invalidating ["decision-counts"] here would refetch three counts on
// every committed note for no observable change. ["calls"] IS invalidated --
// the Saved and Hidden rows carry `decision_note` -- and so is ["call"], the
// call page, which carries the note too and which ["calls"] does not
// prefix-match (review 2026-10-06 M9).
export function useSetDecisionNote() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, profileId, note, expectedState }: NoteArgs) =>
      unwrap(
        await api.PUT("/api/calls/{call_id}/note", {
          params: { path: { call_id: id }, query: { profile_id: profileId } },
          body: { note, expected_state: expectedState },
        }),
      ),
    onSuccess: () =>
      Promise.all([
        qc.invalidateQueries({ queryKey: ["calls"] }),
        qc.invalidateQueries({ queryKey: ["call"] }),
      ]),
  });
}
