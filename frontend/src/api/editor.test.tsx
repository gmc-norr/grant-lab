import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import { useDraft, useSaveSection, useSpec, useSaveSpecSection, useInitSpec } from "./editor";

afterEach(() => {
  vi.restoreAllMocks();
});

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

const DRAFT = {
  head: "a".repeat(40),
  can_edit: true,
  sections: [{ index: 0, heading: "", text: "preamble\n", sha: "x" }],
  citations: {},
};

it("useDraft returns the unwrapped draft", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: DRAFT, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useDraft("ws-1"), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data).toEqual(DRAFT);
});

it("useSaveSection invalidates ['draft', id] on success", async () => {
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: {
      commit_sha: "b".repeat(40),
      head: "b".repeat(40),
      section_sha: "s1",
      issues: [],
      job_enqueued: false,
      changed: true,
    },
    error: undefined,
  } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useSaveSection("ws-1"), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({
    index: 0,
    content: "## A\nx\n",
    base_commit_sha: "a".repeat(40),
    base_section_sha: "s",
  });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["draft", "ws-1"] });
});

it("useSpec returns the unwrapped spec (no citations field)", async () => {
  const SPEC = {
    head: "a".repeat(40),
    can_edit: true,
    sections: [{ index: 0, heading: "", text: "", sha: "x" }],
  };
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: SPEC, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useSpec("ws-1"), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data).toEqual(SPEC);
});

it("useInitSpec invalidates ['spec', id] on success", async () => {
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { head: "b".repeat(40), can_edit: true, sections: [] },
    error: undefined,
  } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useInitSpec("ws-1"), { wrapper: wrapper(qc) });
  await result.current.mutateAsync();
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["spec", "ws-1"] });
});

it("useSaveSpecSection posts content to the spec route + invalidates ['spec', id]", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: {
      commit_sha: "b".repeat(40),
      head: "b".repeat(40),
      section_sha: "s1",
      issues: [],
      job_enqueued: false,
      changed: true,
    },
    error: undefined,
  } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useSaveSpecSection("ws-1"), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({
    index: 1,
    content: "## Aims\nx\n",
    base_commit_sha: "a".repeat(40),
    base_section_sha: "s",
  });
  expect(post).toHaveBeenCalledWith(
    "/api/workspaces/{workspace_id}/spec/sections/{index}",
    expect.objectContaining({
      params: { path: { workspace_id: "ws-1", index: 1 } },
      body: { content: "## Aims\nx\n", base_commit_sha: "a".repeat(40), base_section_sha: "s" },
    }),
  );
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["spec", "ws-1"] });
});

it("a 409 save surfaces an ApiError with code 'conflict'", async () => {
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: undefined,
    error: { error: { code: "conflict", message: "stale" } },
  } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useSaveSection("ws-1"), { wrapper: wrapper(qc) });
  await expect(
    result.current.mutateAsync({
      index: 0,
      content: "## A\nx\n",
      base_commit_sha: "a".repeat(40),
      base_section_sha: "s",
    }),
  ).rejects.toMatchObject({ name: "ApiError", code: "conflict" });
});
