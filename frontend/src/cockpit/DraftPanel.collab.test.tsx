/**
 * DraftPanel collab-branch test.
 * Mocks useDraft to return collab_enabled:true/false, and stubs CollabEditor
 * so the test stays focused on the conditional-mount branch logic.
 */
import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../test-utils";

vi.mock("../api/editor", () => ({
  useDraft: vi.fn(),
  useSaveSection: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
vi.mock("../editor/ArtifactSectionEditor", () => ({
  ArtifactSectionEditor: () => <div data-testid="artifact-section-editor" />,
}));
vi.mock("../collab/CollabEditor", () => ({
  CollabEditor: () => <div data-testid="collab-editor" />,
}));
vi.mock("../api/export", () => ({ downloadExport: vi.fn() }));

import { useDraft } from "../api/editor";
import { DraftPanel } from "./DraftPanel";

const BASE_DATA = {
  head: "h",
  branch: "main",
  can_edit: false,
  sections: [],
  citations: {},
};

describe("DraftPanel conditional mount", () => {
  it("renders CollabEditor when collab_enabled:true", () => {
    vi.mocked(useDraft).mockReturnValue({
      isLoading: false,
      isError: false,
      data: { ...BASE_DATA, collab_enabled: true, can_write: true },
      refetch: vi.fn(),
    } as never);
    renderWithProviders(<DraftPanel id="ws1" />);
    expect(screen.getByTestId("collab-editor")).toBeInTheDocument();
    expect(screen.queryByTestId("artifact-section-editor")).not.toBeInTheDocument();
  });

  it("renders ArtifactSectionEditor when collab_enabled is falsy", () => {
    vi.mocked(useDraft).mockReturnValue({
      isLoading: false,
      isError: false,
      data: { ...BASE_DATA, collab_enabled: false, can_write: false },
      refetch: vi.fn(),
    } as never);
    renderWithProviders(<DraftPanel id="ws1" />);
    expect(screen.getByTestId("artifact-section-editor")).toBeInTheDocument();
    expect(screen.queryByTestId("collab-editor")).not.toBeInTheDocument();
  });
});
