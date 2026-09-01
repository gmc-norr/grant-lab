import { QueryClient } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { queryWrapper as wrapper } from "../test-utils";
import * as clientMod from "./client";
import { useStartCoherence } from "./coherence";

afterEach(() => {
  vi.restoreAllMocks();
});

it("useStartCoherence POSTs and invalidates both inboxes on completed", async () => {
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { id: "j1", status: "completed", n_proposals: 1, error: null,
            started_at: null, completed_at: null },
    error: undefined,
  } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useStartCoherence("ws-1"), { wrapper: wrapper(qc) });
  await result.current.mutateAsync();
  await waitFor(() => {
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["proposed-changes", "ws-1", "spec"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["proposed-changes", "ws-1", "draft"] });
  });
});

it("useStartCoherence skips inbox invalidation when run is not completed", async () => {
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { id: "j2", status: "running", n_proposals: 0, error: null,
            started_at: null, completed_at: null },
    error: undefined,
  } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useStartCoherence("ws-1"), { wrapper: wrapper(qc) });
  await result.current.mutateAsync();
  await waitFor(() => {
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["coherence-latest", "ws-1"] });
  });
  expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ["proposed-changes", "ws-1", "spec"] });
  expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ["proposed-changes", "ws-1", "draft"] });
});
