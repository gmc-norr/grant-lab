import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import { useCitationSource } from "./citations";

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

const SOURCE = {
  evidence_id: "ev-abc123def456",
  reference_id: "r1",
  source_line: "smith2021 · The Smith 2021 Study",
  claim: null,
  stance: "supports",
  confidence: null,
  index: 1,
  total: 1,
  prev_ev: null,
  next_ev: null,
  tombstoned: false,
  no_text: false,
  note: null,
  text: "The cat sat.",
  highlight: [0, 12],
};

describe("useCitationSource", () => {
  it("returns the unwrapped source when an ev id is selected", async () => {
    const spy = vi
      .spyOn(clientMod.api, "GET")
      .mockResolvedValue({ data: SOURCE, error: undefined } as never);
    const qc = new QueryClient();
    const { result } = renderHook(() => useCitationSource("ws-1", "ev-abc123def456"), {
      wrapper: wrapper(qc),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(SOURCE);
    expect(spy).toHaveBeenCalledOnce();
  });

  it("does not fetch while ev id is null", () => {
    const spy = vi.spyOn(clientMod.api, "GET");
    const qc = new QueryClient();
    const { result } = renderHook(() => useCitationSource("ws-1", null), { wrapper: wrapper(qc) });
    expect(result.current.fetchStatus).toBe("idle");
    expect(spy).not.toHaveBeenCalled();
  });
});
