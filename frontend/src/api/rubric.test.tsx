import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import * as clientMod from "./client";
import { useActivate, useRubric } from "./rubric";

afterEach(() => { vi.restoreAllMocks(); });

function wrap() {
  const qc = new QueryClient();
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

it("useRubric fetches the rubric", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { criteria: [{ name: "A", description: "", weight: 1, scale_max: 5 }], funder_notes: null, updated_at: "2026-01-01T00:00:00Z" },
    error: undefined,
  } as never);
  const { result } = renderHook(() => useRubric("ws-1"), { wrapper: wrap() });
  await waitFor(() => expect(result.current.data?.criteria[0].name).toBe("A"));
});

it("useActivate POSTs the rubric", async () => {
  const spy = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { criteria: [], funder_notes: null, updated_at: "x" }, error: undefined } as never);
  const { result } = renderHook(() => useActivate("ws-1"), { wrapper: wrap() });
  result.current.mutate({ criteria: [{ name: "A", description: "", weight: 1, scale_max: 5 }], funder_notes: null });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(spy).toHaveBeenCalled();
});
