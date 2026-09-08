import { QueryClient } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { queryWrapper as wrapper } from "../test-utils";
import * as clientMod from "./client";
import { useCallDetail, useHideCall, useSetDecisionNote } from "./calls";

afterEach(() => {
  vi.restoreAllMocks();
});

const DETAIL = {
  call: {
    id: 5, title: "T", source_name: "Vinnova", programme: null, external_id: "X",
    published_at: null, deadline: null, budget_min: null, budget_max: null,
    currency: null, url: "https://x", keywords: [], summary: null,
  },
  ranking: null,
  criteria: [],
  best_profile_id: null,
};

it("useCallDetail GETs the detail path with the profile query and unwraps", async () => {
  const get = vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: DETAIL, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useCallDetail(5, 7), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data).toEqual(DETAIL);
  expect(get).toHaveBeenCalledWith("/api/calls/{call_id}", {
    params: { path: { call_id: 5 }, query: { profile: "7" } },
  });
});

it("useCallDetail omits the profile query when none is given", async () => {
  const get = vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: DETAIL, error: undefined } as never);
  const qc = new QueryClient();
  renderHook(() => useCallDetail(5, null), { wrapper: wrapper(qc) });
  await waitFor(() =>
    expect(get).toHaveBeenCalledWith("/api/calls/{call_id}", {
      params: { path: { call_id: 5 }, query: {} },
    }),
  );
});

it("useHideCall POSTs the hide path WITH profile_id and invalidates ['calls'] AND ['decision-counts']", async () => {
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { hidden: true }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useHideCall(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ id: 5, profileId: 7 });
  // `profile_id` is REQUIRED server-side (422 without it): a decision row
  // cannot exist without a profile.
  expect(post).toHaveBeenCalledWith("/api/calls/{call_id}/hide", {
    params: { path: { call_id: 5 }, query: { profile_id: 7 } },
  });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["calls"] });
  // Hook-level on purpose: a per-call onSuccess is dropped by TanStack v5 if the
  // caller unmounts before the response (final review, F1). The rail reads this key.
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["decision-counts"] });
});

it("useSetDecisionNote PUTs the note and invalidates ['calls'] but NOT ['decision-counts']", async () => {
  const put = vi
    .spyOn(clientMod.api, "PUT")
    .mockResolvedValue({ data: { decision_note: "why" }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useSetDecisionNote(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({
    id: 5,
    profileId: 7,
    note: "wrong field",
    expectedState: "hidden",
  });
  // `expected_state` on the wire, not just in the args type: the server 409s
  // with it and writes unconditionally without it, so dropping it here silently
  // reopens the two-tab race the endpoint exists to close.
  expect(put).toHaveBeenCalledWith("/api/calls/{call_id}/note", {
    params: { path: { call_id: 5 }, query: { profile_id: 7 } },
    body: { note: "wrong field", expected_state: "hidden" },
  });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["calls"] });
  // The NEGATIVE half, and it is the point. A note changes no count and moves
  // no call between views, so invalidating the rail would refetch three counts
  // per note for no observable change. Nothing else pins that: deleting this
  // hook's whole onSuccess left all 148 files green (measured 2026-09-07), and
  // "making it consistent" with invalidateDecisions is the natural wrong fix.
  expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ["decision-counts"] });
});
