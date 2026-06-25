import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../test-utils";
import { DraftPanel } from "./DraftPanel";

const h = vi.hoisted(() => ({ download: vi.fn() }));

vi.mock("../api/editor", () => ({
  useDraft: () => ({
    isLoading: false,
    isError: false,
    data: { head: "h", can_edit: true, sections: [] },
    refetch: vi.fn(),
  }),
  useSaveSection: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
vi.mock("../editor/ArtifactSectionEditor", () => ({
  ArtifactSectionEditor: () => <div>editor</div>,
}));
vi.mock("../api/export", () => ({ downloadExport: h.download }));

afterEach(() => {
  h.download.mockReset();
});

describe("DraftPanel export menu", () => {
  it("downloads raw markdown", async () => {
    h.download.mockResolvedValue({ ok: true });
    renderWithProviders(<DraftPanel id="ws1" />);
    fireEvent.click(screen.getByRole("button", { name: /export/i }));
    fireEvent.click(await screen.findByText(/^markdown$/i));
    await waitFor(() => expect(h.download).toHaveBeenCalledWith("ws1", "raw"));
  });

  it("surfaces the blocked-refs message", async () => {
    h.download.mockResolvedValue({
      ok: false,
      kind: "blocked",
      blocked: [{ citekey: "smith2021", reason: "metadata_unapproved" }],
    });
    renderWithProviders(<DraftPanel id="ws1" />);
    fireEvent.click(screen.getByRole("button", { name: /export/i }));
    fireEvent.click(await screen.findByText(/markdown \+ citations/i));
    expect(await screen.findByText(/smith2021/)).toBeInTheDocument();
  });
});
