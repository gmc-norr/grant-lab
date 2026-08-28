import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import { useVersion, useVersions } from "./versions";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("useVersions", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("returns the list payload", async () => {
    vi.spyOn(clientMod.api, "GET").mockResolvedValue({
      data: {
        versions: [
          {
            commit_sha: "abc1234",
            committed_at: "2026-08-28T10:00:00+00:00",
            subject: "draft 1",
            created_by: "user",
            triggering_turn_id: null,
            turn_prompt: null,
          },
        ],
        truncated: false,
        history_available: true,
      },
      error: undefined,
      response: new Response(),
    } as never);

    const { result } = renderHook(() => useVersions("ws1", "draft"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.versions[0].subject).toBe("draft 1");
    expect(result.current.data?.history_available).toBe(true);
  });

  it("never returns undefined when the payload is empty", async () => {
    vi.spyOn(clientMod.api, "GET").mockResolvedValue({
      data: undefined,
      error: undefined,
      response: new Response(),
    } as never);

    const { result } = renderHook(() => useVersions("ws1", "draft"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({
      versions: [],
      truncated: false,
      history_available: false,
    });
  });
});

describe("useVersion", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("does not fetch when no sha is selected", async () => {
    const spy = vi.spyOn(clientMod.api, "GET");
    const { result } = renderHook(() => useVersion("ws1", "draft", null), { wrapper });
    await waitFor(() => expect(result.current.fetchStatus).toBe("idle"));
    expect(spy).not.toHaveBeenCalled();
  });

  it("fetches the markdown when a sha is selected", async () => {
    vi.spyOn(clientMod.api, "GET").mockResolvedValue({
      data: { commit_sha: "abc1234", artifact: "draft", markdown: "# hi\n" },
      error: undefined,
      response: new Response(),
    } as never);

    const { result } = renderHook(() => useVersion("ws1", "draft", "abc1234"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.markdown).toBe("# hi\n");
  });
});
