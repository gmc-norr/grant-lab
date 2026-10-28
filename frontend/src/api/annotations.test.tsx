import { QueryClient } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { queryWrapper as wrapper } from "../test-utils";
import * as clientMod from "./client";
import { useInsertAnnotation } from "./annotations";

afterEach(() => {
  vi.restoreAllMocks();
});

// Review 2026-10-26 (A8). `useInsertAnnotation` invalidated `["proposals", id]`
// -- a key NO query in the frontend registers. The proposals list lives under
// ["proposed-changes", id, artifact] (api/proposals.ts), so the Inbox pane kept
// the pre-insert rows and the inserted claim still showed as pending.
//
// BEHAVIOURAL on purpose: a `toHaveBeenCalledWith({queryKey: [...]})` assertion
// is exactly what made the original defect invisible -- it passes for a key
// nothing matches. These seed the real key and require fresh -> stale.
describe("inserting an annotation refreshes the views it changed", () => {
  const stale = (qc: QueryClient, key: unknown[]) => qc.getQueryState(key)?.isInvalidated;

  function primed() {
    const qc = new QueryClient();
    qc.setQueryData(["proposed-changes", "ws-1", "draft"], []);
    qc.setQueryData(["proposed-changes", "ws-1", "spec"], []);
    qc.setQueryData(["annotations", "ws-1"], { sections: [] });
    qc.setQueryData(["citation-audit", "ws-1"], { error_count: 1, issues: [] });
    return qc;
  }

  it("marks the proposed-changes list stale under BOTH artifacts", async () => {
    vi.spyOn(clientMod.api, "POST").mockResolvedValue({
      data: { commit_sha: "b".repeat(40) },
      error: undefined,
    } as never);
    const qc = primed();
    expect(stale(qc, ["proposed-changes", "ws-1", "draft"])).toBe(false);
    expect(stale(qc, ["proposed-changes", "ws-1", "spec"])).toBe(false);
    const { result } = renderHook(() => useInsertAnnotation("ws-1"), { wrapper: wrapper(qc) });
    await result.current.mutateAsync(42);
    // ["proposed-changes", id] is the PREFIX; TanStack matches by prefix, so one
    // call covers the draft and spec lists the cockpit mounts side by side.
    await waitFor(() => expect(stale(qc, ["proposed-changes", "ws-1", "draft"])).toBe(true));
    expect(stale(qc, ["proposed-changes", "ws-1", "spec"])).toBe(true);
  });

  it("still marks the annotations read-model and the citation audit stale", async () => {
    // The regression half: the key swap must not drop what already worked.
    vi.spyOn(clientMod.api, "POST").mockResolvedValue({
      data: { commit_sha: "b".repeat(40) },
      error: undefined,
    } as never);
    const qc = primed();
    const { result } = renderHook(() => useInsertAnnotation("ws-1"), { wrapper: wrapper(qc) });
    await result.current.mutateAsync(42);
    await waitFor(() => expect(stale(qc, ["annotations", "ws-1"])).toBe(true));
    expect(stale(qc, ["citation-audit", "ws-1"])).toBe(true);
  });
});
