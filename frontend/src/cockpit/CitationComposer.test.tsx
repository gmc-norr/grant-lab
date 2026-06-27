import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { CitationComposer } from "./CitationComposer";
import { toCodePointOffset } from "./selection";

// ---------------------------------------------------------------------------
// DOM Range multi-node measurement — unit test for the jsdom behaviour the
// captureSelection() path in CitationComposer relies on.
// ---------------------------------------------------------------------------
describe("Range.toString() measurement across multiple text nodes", () => {
  it("measures the cumulative UTF-16 offset from <pre> start through a later text node", () => {
    // Build:  <pre>[text1: "hello "][span > text: "world"][text3: " end"]</pre>
    // Selection endpoint: text3 at offset 3 (" en").
    // Expected cumulative length from pre start: "hello " (6) + "world" (5) + " en" (3) = 14.
    const pre = document.createElement("pre");
    const text1 = document.createTextNode("hello ");
    const span = document.createElement("span");
    const text2 = document.createTextNode("world");
    span.appendChild(text2);
    const text3 = document.createTextNode(" end");
    pre.appendChild(text1);
    pre.appendChild(span);
    pre.appendChild(text3);
    document.body.appendChild(pre);

    const range = document.createRange();
    range.setStart(pre, 0);        // before first child of pre
    range.setEnd(text3, 3);        // 3 chars into " end" (" en")

    // This is the exact measurement captureSelection() performs:
    const measured = range.toString().length;
    // "hello " (6) + "world" (5) + " en" (3) = 14
    expect(measured).toBe(14);

    document.body.removeChild(pre);
  });
});


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

  it("computes quote_start/end correctly when the <pre> contains MULTIPLE text nodes and selection is in a later node", async () => {
    // This is the key cross-node test. B17 replaced `range.startOffset` (which only
    // gives the offset WITHIN the containing node) with a Range measurement from pre
    // start, precisely to handle the case where the text node holding the selection
    // anchor is NOT the first child of <pre>. All prior tests mock firstChild — this
    // one does not.
    //
    // Chunk text: "prefix text" (11 chars) + " suffix part" (12 chars) = "prefix text suffix part"
    // We split the <pre> content into two text nodes: "prefix text" and " suffix part".
    // Selection: start in node2 at offset 1 (" su"), end in node2 at offset 8 (" suffix ").
    // Expected absolute UTF-16 start = 11 + 1 = 12; end = 11 + 8 = 19.
    // All ASCII so code-point offsets equal UTF-16 offsets.
    const chunkText = "prefix text suffix part";
    vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
      if (path.endsWith("/chunks")) {
        return Promise.resolve({ data: { reference_id: "r1", title: "Smith", chunks: [
          { chunk_index: 0, section_path: null, text: chunkText },
        ] }, error: undefined });
      }
      return Promise.resolve({ data: [{ id: "r1", citekey: "smith2021", title: "Smith", kind: "paper", text_status: "done" }], error: undefined });
    }) as never);
    const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { ev_id: "ev-2", citekey: "smith2021", commit_sha: "a".repeat(40) }, error: undefined } as never);
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <CitationComposer workspaceId="ws-1" claim={claim} onDone={() => {}} onCancel={() => {}} />
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /Smith/ }));
    const pre = await screen.findByText(chunkText);

    // Manually split the <pre>'s single text node into two to simulate the multi-node
    // scenario (e.g. React re-renders or citation-pill markup splitting the text).
    // pre.firstChild is a text node with "prefix text suffix part".
    // We split at offset 11 → node1="prefix text", node2=" suffix part".
    const node1 = pre.firstChild as Text;
    const node2 = node1.splitText(11); // "prefix text" | " suffix part"

    // Selection: start in node2 at offset 1 (" s"), end in node2 at offset 8 (" suffix ").
    // Old code: quote_start = node2.startOffset = 1 (wrong — node-local, not pre-absolute).
    // New code: Range from pre start to (node2, 1) → "prefix text " = 12 chars → correct.
    vi.spyOn(window, "getSelection").mockReturnValue({
      rangeCount: 1,
      getRangeAt: () => ({
        startOffset: 1,
        endOffset: 8,
        startContainer: node2,
        endContainer: node2,
        toString: () => "suffix ",
      }),
      toString: () => "suffix ",
    } as never);
    fireEvent.mouseUp(pre);
    fireEvent.click(await screen.findByRole("button", { name: /add citation/i }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    const body = (post.mock.calls[0][1] as { body: Record<string, unknown> }).body;
    // Absolute offsets: start = 11 + 1 = 12, end = 11 + 8 = 19.
    expect(body).toMatchObject({ evidence_quote: "suffix ", quote_start: 12, quote_end: 19 });
  });
});
