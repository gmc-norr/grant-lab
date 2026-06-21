import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import { useStartCoherence } from "./coherence";

afterEach(() => {
  vi.restoreAllMocks();
});

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

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
