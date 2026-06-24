import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import { turnStreamUrl, useTurns } from "./turns";

afterEach(() => {
  vi.restoreAllMocks();
});

function wrap() {
  const qc = new QueryClient();
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

it("useTurns fetches the read-model", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { turns: [], can_send: true, needs_disclosure: false, phase: "active", running_job_id: null },
    error: undefined,
  } as never);
  const { result } = renderHook(() => useTurns("ws-1"), { wrapper: wrap() });
  await waitFor(() => expect(result.current.data?.can_send).toBe(true));
});

it("turnStreamUrl builds the SSE path", () => {
  expect(turnStreamUrl("ws-1", "job-9")).toBe("/api/workspaces/ws-1/turns/job-9/stream");
});
