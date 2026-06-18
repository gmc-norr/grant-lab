import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { CitationComposer } from "./CitationComposer";
import { toCodePointOffset } from "./selection";

describe("toCodePointOffset", () => {
  it("counts code points before a UTF-16 index (non-BMP aware)", () => {
    expect(toCodePointOffset("🚀 azure", 0)).toBe(0); // before the emoji
    expect(toCodePointOffset("🚀 azure", 2)).toBe(1); // after the 2-unit emoji -> 1 code point
    expect(toCodePointOffset("🚀 azure", 4)).toBe(3); // "🚀 a" -> 3 code points
  });
});

describe("CitationComposer", () => {
  const claim = {
    section_index: 1, base_commit_sha: "a".repeat(40), base_section_sha: "s", anchor: "claim.",
  };

  function mockApi() {
    vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
      if (path.endsWith("/chunks")) {
        return Promise.resolve({
          data: { reference_id: "r1", title: "Smith", chunks: [
            { chunk_index: 0, section_path: null, text: "the azure sky" },
          ] },
          error: undefined,
        });
      }
      return Promise.resolve({
        data: [{ id: "r1", citekey: "smith2021", title: "Smith", kind: "paper", text_status: "done" }],
        error: undefined,
      });
    }) as never);
  }

  it("picks a reference, selects a span and POSTs the citation", async () => {
    mockApi();
    const post = vi
      .spyOn(clientMod.api, "POST")
      .mockResolvedValue({ data: { ev_id: "ev-1", citekey: "smith2021", commit_sha: "a".repeat(40) }, error: undefined } as never);
    const onDone = vi.fn();
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <CitationComposer workspaceId="ws-1" claim={claim} onDone={onDone} onCancel={() => {}} />
      </QueryClientProvider>,
      { wrapper: undefined },
    );
    // pick the reference
    fireEvent.click(await screen.findByRole("button", { name: /Smith/ }));
    const pre = await screen.findByText("the azure sky");
    // stub a DOM selection of "azure" (utf16 4..9) within the chunk node
    const range = { startOffset: 4, endOffset: 9, toString: () => "azure" };
    vi.spyOn(window, "getSelection").mockReturnValue({
      rangeCount: 1,
      getRangeAt: () => ({ ...range, startContainer: pre.firstChild, endContainer: pre.firstChild }),
      toString: () => "azure",
    } as never);
    fireEvent.mouseUp(pre);
    // confirm
    fireEvent.click(await screen.findByRole("button", { name: /add citation/i }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    const body = (post.mock.calls[0][1] as { body: Record<string, unknown> }).body;
    expect(body).toMatchObject({
      reference_id: "r1", chunk_index: 0, evidence_quote: "azure",
      quote_start: 4, quote_end: 9, anchor: "claim.", section_index: 1,
    });
    await waitFor(() => expect(onDone).toHaveBeenCalled());
  });

  it("shows the server error (code-stripped) when the POST fails", async () => {
    mockApi();
    // unwrap() reads res.error.error.{code,message} (the Plan-1 nested envelope) and
    // throws ApiError(code, message); the catch then strips the "<code>: " prefix.
    vi.spyOn(clientMod.api, "POST").mockResolvedValue({
      data: undefined,
      error: { error: { code: "conflict", message: "this section changed — reopen and re-cite" } },
    } as never);
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <CitationComposer workspaceId="ws-1" claim={claim} onDone={() => {}} onCancel={() => {}} />
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /Smith/ }));
    const pre = await screen.findByText("the azure sky");
    vi.spyOn(window, "getSelection").mockReturnValue({
      rangeCount: 1,
      getRangeAt: () => ({
        startOffset: 4, endOffset: 9,
        startContainer: pre.firstChild, endContainer: pre.firstChild,
        toString: () => "azure",
      }),
      toString: () => "azure",
    } as never);
    fireEvent.mouseUp(pre);
    fireEvent.click(await screen.findByRole("button", { name: /add citation/i }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("this section changed — reopen and re-cite");
    expect(alert).not.toHaveTextContent("conflict:");
  });

  it("converts non-BMP UTF-16 selection offsets to code-point offsets in the POST", async () => {
    vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
      if (path.endsWith("/chunks")) {
        return Promise.resolve({ data: { reference_id: "r1", title: "Smith", chunks: [
          { chunk_index: 0, section_path: null, text: "🚀 azure sky" },
        ] }, error: undefined });
      }
      return Promise.resolve({ data: [{ id: "r1", citekey: "smith2021", title: "Smith", kind: "paper", text_status: "done" }], error: undefined });
    }) as never);
    const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { ev_id: "ev-1", citekey: "smith2021", commit_sha: "a".repeat(40) }, error: undefined } as never);
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <CitationComposer workspaceId="ws-1" claim={claim} onDone={() => {}} onCancel={() => {}} />
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /Smith/ }));
    const pre = await screen.findByText("🚀 azure sky");
    // "🚀"=2 UTF-16 units (indices 0,1), " "=2, "azure"=UTF-16 [3,8] -> code-point [2,7].
    vi.spyOn(window, "getSelection").mockReturnValue({
      rangeCount: 1,
      getRangeAt: () => ({
        startOffset: 3, endOffset: 8,
        startContainer: pre.firstChild, endContainer: pre.firstChild,
        toString: () => "azure",
      }),
      toString: () => "azure",
    } as never);
    fireEvent.mouseUp(pre);
    fireEvent.click(await screen.findByRole("button", { name: /add citation/i }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    const body = (post.mock.calls[0][1] as { body: Record<string, unknown> }).body;
    expect(body).toMatchObject({ evidence_quote: "azure", quote_start: 2, quote_end: 7 });
  });
});
