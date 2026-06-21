import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import { useAcceptProposal, useProposedChanges, useRejectProposal } from "./proposals";

afterEach(() => {
  vi.restoreAllMocks();
});

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

it("useProposedChanges GETs with the target_artifact query", async () => {
  const get = vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: { items: [] }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useProposedChanges("ws-1", "spec"), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith("/api/workspaces/{workspace_id}/proposed-changes", {
    params: { path: { workspace_id: "ws-1" }, query: { target_artifact: "spec" } },
  });
});

it("useAcceptProposal POSTs and invalidates spec + proposals", async () => {
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { status: "accepted" },
    error: undefined,
  } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useAcceptProposal("ws-1", "spec"), { wrapper: wrapper(qc) });
  await result.current.mutateAsync("pc-1");
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["proposed-changes", "ws-1", "spec"] });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["spec", "ws-1"] });
});

it("useAcceptProposal invalidates spec + proposals on a 409 error too", async () => {
  // A spec-lock race returns the /api conflict envelope; unwrap throws ApiError.
  // The hook must still invalidate so `locked` refreshes and Accept re-disables.
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: undefined,
    error: { error: { code: "conflict", message: "spec is locked — unlock to accept" } },
  } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useAcceptProposal("ws-1", "spec"), { wrapper: wrapper(qc) });
  await expect(result.current.mutateAsync("pc-1")).rejects.toThrow(/conflict/);
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["proposed-changes", "ws-1", "spec"] });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["spec", "ws-1"] });
});

it("useRejectProposal POSTs and invalidates the list", async () => {
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { status: "rejected" },
    error: undefined,
  } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useRejectProposal("ws-1", "spec"), { wrapper: wrapper(qc) });
  await result.current.mutateAsync("pc-1");
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["proposed-changes", "ws-1", "spec"] });
});
