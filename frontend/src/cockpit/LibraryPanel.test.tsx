import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LibraryPanel } from "./LibraryPanel";

// Hoisted mock fns (NOT plain factories) so a single Task-10 test can override
// one hook's return via h.useX.mockReturnValue(...). ESM-safe: no require().
const h = vi.hoisted(() => ({
  refs: {
    data: [
      {
        id: "r1",
        title: "",
        citekey: null,
        kind: "paper",
        text_status: "failed",
        metadata_status: "pending",
        metadata_waiver: false,
        text_error: "Boom",
        filename: "paper.pdf",
      },
    ],
  },
  useReferences: vi.fn(),
  useUpdateKind: vi.fn(),
  useDeleteReference: vi.fn(),
  useReprocessReference: vi.fn(),
  useToggleWaiver: vi.fn(),
  useApproveMetadata: vi.fn(),
  useUploadReference: vi.fn(),
  useFetchReference: vi.fn(),
}));

vi.mock("../api/citations", () => ({ useReferences: h.useReferences }));
vi.mock("../api/references", () => ({
  useUpdateKind: h.useUpdateKind,
  useDeleteReference: h.useDeleteReference,
  useReprocessReference: h.useReprocessReference,
  useToggleWaiver: h.useToggleWaiver,
  useApproveMetadata: h.useApproveMetadata,
  useUploadReference: h.useUploadReference,
  useFetchReference: h.useFetchReference,
}));

beforeEach(() => {
  h.useReferences.mockReturnValue(h.refs);
  for (const fn of [
    h.useUpdateKind,
    h.useDeleteReference,
    h.useReprocessReference,
    h.useToggleWaiver,
    h.useApproveMetadata,
    h.useUploadReference,
    h.useFetchReference,
  ]) {
    fn.mockReturnValue({ mutate: vi.fn(), isPending: false });
  }
});

function wrap(ui: ReactNode) {
  return render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
}

describe("LibraryPanel references table", () => {
  it("falls back to the filename when title and citekey are empty", () => {
    wrap(<LibraryPanel id="ws1" />);
    expect(screen.getByText("paper.pdf")).toBeInTheDocument();
  });

  it("shows the text_status and metadata_status", () => {
    wrap(<LibraryPanel id="ws1" />);
    expect(screen.getByText(/failed/i)).toBeInTheDocument();
    expect(screen.getByText(/pending/i)).toBeInTheDocument();
  });
});

import { fireEvent } from "@testing-library/react";

describe("LibraryPanel actions", () => {
  it("uploads a chosen file via the picker", () => {
    const upload = vi.fn();
    h.useUploadReference.mockReturnValue({ mutate: upload, isPending: false });
    wrap(<LibraryPanel id="ws1" />);
    const input = screen.getByLabelText(/add reference file/i) as HTMLInputElement;
    const file = new File([new Uint8Array([1, 2, 3])], "a.pdf", { type: "application/pdf" });
    fireEvent.change(input, { target: { files: [file] } });
    expect(upload).toHaveBeenCalled();
  });

  it("fetches a reference by identifier", () => {
    const fetchRef = vi.fn();
    h.useFetchReference.mockReturnValue({ mutate: fetchRef, isPending: false });
    wrap(<LibraryPanel id="ws1" />);
    fireEvent.change(screen.getByLabelText(/doi.*pmid.*url/i), { target: { value: "10.1/x" } });
    fireEvent.click(screen.getByRole("button", { name: /^fetch$/i }));
    expect(fetchRef).toHaveBeenCalledWith({ input: "10.1/x", kind: expect.any(String) });
  });

  it("reprocesses a failed reference", () => {
    const reprocess = vi.fn();
    h.useReprocessReference.mockReturnValue({ mutate: reprocess, isPending: false });
    wrap(<LibraryPanel id="ws1" />);
    fireEvent.click(screen.getByRole("button", { name: /reprocess/i }));
    expect(reprocess).toHaveBeenCalledWith("r1");
  });

  it("drop queue starts at most MAX (2) uploads concurrently", () => {
    // mutate never invokes onSuccess/onError, so each upload stays 'in flight'
    // and the queue must hold the remaining files until a settle.
    const mutate = vi.fn();
    h.useUploadReference.mockReturnValue({ mutate, isPending: false });
    wrap(<LibraryPanel id="ws1" />);
    const dz = screen.getByText(/drop pdfs here/i);
    const files = [0, 1, 2, 3, 4].map(
      (i) => new File([new Uint8Array([i])], `f${i}.pdf`, { type: "application/pdf" }),
    );
    fireEvent.drop(dz, { dataTransfer: { files } });
    expect(mutate).toHaveBeenCalledTimes(2); // MAX=2; the other 3 wait
  });
});
