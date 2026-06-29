import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import { turnStreamUrl, useTurns, useSubmitTurn } from "./turns";

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

it("forwards selection_context in the POST body", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { job_id: "j" }, error: undefined } as never);
  const { result } = renderHook(() => useSubmitTurn("ws-1"), { wrapper: wrap() });
  await act(async () => {
    await result.current.mutateAsync({
      content: "Shorten", selection_context: {
        request_type: "modify", target_artifact: "draft", section_index: 1,
        base_commit_sha: "a".repeat(40), base_section_sha: "s1",
        anchor: "x", from_offset: 0, to_offset: 1,
      },
    });
  });
  const body = (post.mock.calls[0][1] as { body: Record<string, unknown> }).body;
  expect(body.selection_context).toMatchObject({ request_type: "modify", section_index: 1 });
});
