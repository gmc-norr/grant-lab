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
