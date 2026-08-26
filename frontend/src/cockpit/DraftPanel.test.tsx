import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../test-utils";
import { DraftPanel } from "./DraftPanel";

// Mutable flag — lets each test control whether collab is enabled.
// Defaults to false so the two pre-existing tests (collab-OFF path) stay green.
const flags = vi.hoisted(() => ({ collab: false }));
const h = vi.hoisted(() => ({ download: vi.fn() }));

vi.mock("../api/editor", () => ({
  useDraft: () => ({
    isLoading: false,
    isError: false,
    data: {
      head: "h",
      can_edit: true,
      can_write: true,
      collab_enabled: flags.collab,
      sections: [],
    },
    refetch: vi.fn(),
  }),
  useSaveSection: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
vi.mock("../editor/ArtifactSectionEditor", () => ({
  ArtifactSectionEditor: () => <div data-testid="legacy-editor">editor</div>,
}));
vi.mock("../collab/CollabEditor", () => ({
  CollabEditor: (p: { actions?: React.ReactNode }) => <div>{p.actions}</div>,
}));
vi.mock("../api/export", () => ({ downloadExport: h.download }));

afterEach(() => {
  h.download.mockReset();
  flags.collab = false;
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

describe("DraftPanel — Export placement", () => {
  it("offers Export when collab is ON (inside the toolbar)", () => {
    flags.collab = true;
    renderWithProviders(<DraftPanel id="ws1" />);
    expect(screen.getByText(/Export/)).toBeInTheDocument();
  });

  it("STILL offers Export when collab is OFF (legacy editor)", () => {
    // Regression guard: the action row must be present in both branches.
    // Passing exportActions only as CollabEditor's `actions` would silently
    // drop it from the fallback path.
    flags.collab = false;
    renderWithProviders(<DraftPanel id="ws1" />);
    expect(screen.getByTestId("legacy-editor")).toBeInTheDocument();
    expect(screen.getByText(/Export/)).toBeInTheDocument();
  });
});
