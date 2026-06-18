import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import {
  useAddCitation,
  useCitationSource,
  useReferenceChunks,
  useReferences,
} from "./citations";

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

describe("useReferences / useReferenceChunks / useAddCitation", () => {
  it("useReferences returns the unwrapped list", async () => {
    const refs = [{ id: "r1", citekey: "smith2021", title: "Smith", kind: "paper", text_status: "done" }];
    vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: refs, error: undefined } as never);
    const qc = new QueryClient();
    const { result } = renderHook(() => useReferences("ws-1"), { wrapper: wrapper(qc) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(refs);
  });

  it("useReferenceChunks is disabled until a reference id is chosen", () => {
    const spy = vi.spyOn(clientMod.api, "GET");
    const qc = new QueryClient();
    const { result } = renderHook(() => useReferenceChunks("ws-1", null), { wrapper: wrapper(qc) });
    expect(result.current.fetchStatus).toBe("idle");
    expect(spy).not.toHaveBeenCalled();
  });

  it("useReferenceChunks fetches once a reference id is set", async () => {
    const chunks = {
      reference_id: "r1",
      title: "Smith",
      chunks: [{ chunk_index: 0, section_path: null, text: "the azure sky" }],
    };
    vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: chunks, error: undefined } as never);
    const qc = new QueryClient();
    const { result } = renderHook(() => useReferenceChunks("ws-1", "r1"), { wrapper: wrapper(qc) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(chunks);
  });

  it("useAddCitation POSTs the body and resolves the result", async () => {
    const out = { ev_id: "ev-1", citekey: "smith2021", commit_sha: "a".repeat(40) };
    const spy = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: out, error: undefined } as never);
    const qc = new QueryClient();
    const { result } = renderHook(() => useAddCitation("ws-1"), { wrapper: wrapper(qc) });
    const body = {
      section_index: 1, base_commit_sha: "a".repeat(40), base_section_sha: "s",
      anchor: "claim.", reference_id: "r1", chunk_index: 0,
      evidence_quote: "azure", quote_start: 0, quote_end: 5, stance: "supports",
    };
    await expect(result.current.mutateAsync(body)).resolves.toEqual(out);
    expect(spy).toHaveBeenCalledOnce();
  });

  it("useAddCitation invalidates the draft query on success", async () => {
    const out = { ev_id: "ev-1", citekey: "smith2021", commit_sha: "a".repeat(40) };
    vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: out, error: undefined } as never);
    const qc = new QueryClient();
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    const { result } = renderHook(() => useAddCitation("ws-1"), { wrapper: wrapper(qc) });
    await result.current.mutateAsync({
      section_index: 1, base_commit_sha: "a".repeat(40), base_section_sha: "s",
      anchor: "claim.", reference_id: "r1", chunk_index: 0,
      evidence_quote: "azure", quote_start: 0, quote_end: 5, stance: "supports",
    });
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ["draft", "ws-1"] }),
    );
  });
});
