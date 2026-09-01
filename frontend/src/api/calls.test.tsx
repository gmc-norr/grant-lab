import { QueryClient } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { queryWrapper as wrapper } from "../test-utils";
import * as clientMod from "./client";
import { useCallDetail, useHideCall } from "./calls";

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

it("useHideCall POSTs the hide path and invalidates ['calls']", async () => {
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { hidden: true }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useHideCall(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync(5);
  expect(post).toHaveBeenCalledWith("/api/calls/{call_id}/hide", {
    params: { path: { call_id: 5 } },
  });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["calls"] });
});
