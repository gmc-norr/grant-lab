import { QueryClient } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { queryWrapper as wrapper } from "../test-utils";
import * as clientMod from "./client";
import { useAddTriple, useAdminGoldenSet, useDeleteTriple } from "./adminGoldenSet";

afterEach(() => {
  vi.restoreAllMocks();
});

it("useAdminGoldenSet GETs the golden set", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: { triples: [], candidates: [] }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useAdminGoldenSet(), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith("/api/admin/golden-set", {});
});

it("useAddTriple POSTs the body and invalidates", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 1 }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useAddTriple(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ call_id: 3, profile_id: 4, expected_score: 65, note: "x" });
  expect(post).toHaveBeenCalledWith("/api/admin/golden-set", {
    body: { call_id: 3, profile_id: 4, expected_score: 65, note: "x" },
  });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-golden-set"] });
});

it("useDeleteTriple DELETEs the path param and invalidates the golden set", async () => {
  const del = vi.spyOn(clientMod.api, "DELETE").mockResolvedValue({ data: undefined, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useDeleteTriple(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ triple_id: 7 });
  expect(del).toHaveBeenCalledWith("/api/admin/golden-set/{triple_id}", {
    params: { path: { triple_id: 7 } },
  });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-golden-set"] });
});
