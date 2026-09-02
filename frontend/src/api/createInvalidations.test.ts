/**
 * Review 2026-09-02 A7/F4: creating a workspace or a profile invalidated
 * nothing. `useCreateWorkspace` even carried a comment claiming "the list
 * refetches on next mount", which is false inside the app's 20 s `staleTime`
 * with `refetchOnWindowFocus: false` — remounting an observer over a fresh
 * cache entry serves the cache and fires no request.
 *
 * The two user-visible symptoms: a new workspace missing from the rail list,
 * and — worse — saving your FIRST profile and returning to /profiles to be told
 * "No profiles yet", under a "Create your first profile" button.
 */
import { QueryClient } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./client";
import { useCreateProfile } from "./profiles";
import { useCreateWorkspace } from "./workspace";
import { harnessFor } from "./testHarness";

afterEach(() => {
  vi.restoreAllMocks();
});

/** Seed a key, run the mutation, report whether the key was invalidated. */
async function invalidatedBy(
  seed: unknown[][],
  run: (qc: QueryClient) => Promise<void>,
): Promise<Record<string, boolean>> {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  for (const key of seed) qc.setQueryData(key, { seeded: true });
  await run(qc);
  return Object.fromEntries(
    seed.map((key) => [JSON.stringify(key), qc.getQueryState(key)?.isInvalidated === true]),
  );
}

describe("create mutations refresh the list they just changed", () => {
  it("useCreateWorkspace invalidates every workspaces filter", async () => {
    vi.spyOn(api, "POST").mockResolvedValue({ data: { id: "w9" } } as never);
    // useWorkspaces keys by status filter, so the PREFIX must match all of them.
    const seen = await invalidatedBy(
      [["workspaces", ""], ["workspaces", "active"]],
      async (qc) => {
        const { result } = renderHook(() => useCreateWorkspace(), { wrapper: harnessFor(qc) });
        await result.current.mutateAsync({ call_id: 1, title: "Brand new" });
      },
    );
    expect(seen['["workspaces",""]']).toBe(true);
    expect(seen['["workspaces","active"]']).toBe(true);
  });

  it("useCreateProfile invalidates the list and the dashboard facet", async () => {
    vi.spyOn(api, "POST").mockResolvedValue({ data: { id: 7 } } as never);
    const seen = await invalidatedBy([["profiles"], ["calls", {}]], async (qc) => {
      const { result } = renderHook(() => useCreateProfile(), { wrapper: harnessFor(qc) });
      await result.current.mutateAsync({ name: "Genomics" } as never);
    });
    expect(seen['["profiles"]'], "the profiles list stayed pre-create").toBe(true);
    // CallsFilters builds its Profile facet from the calls response.
    expect(seen['["calls",{}]'], "the dashboard profile facet stayed pre-create").toBe(true);
  });

  it("a failed create invalidates nothing", async () => {
    vi.spyOn(api, "POST").mockResolvedValue({
      error: { error: { code: "conflict", message: "taken" } },
    } as never);
    const seen = await invalidatedBy([["profiles"]], async (qc) => {
      const { result } = renderHook(() => useCreateProfile(), { wrapper: harnessFor(qc) });
      await result.current.mutateAsync({ name: "dupe" } as never).catch(() => {});
      await waitFor(() => expect(result.current.isError).toBe(true));
    });
    expect(seen['["profiles"]']).toBe(false);
  });
});
