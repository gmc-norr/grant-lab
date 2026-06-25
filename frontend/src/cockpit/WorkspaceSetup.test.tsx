import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders } from "../test-utils";
import { WorkspaceSetup } from "./WorkspaceSetup";

const h = vi.hoisted(() => ({ setup: vi.fn(), rubric: vi.fn(), activate: vi.fn(), meta: vi.fn() }));
vi.mock("../api/rubric", () => ({
  useSetupStatus: () => h.setup(),
  useRubric: () => h.rubric(),
  useActivate: () => ({ mutate: h.activate, isPending: false }),
}));
vi.mock("../api/workspace", () => ({ useCockpitMeta: () => h.meta() }));

beforeEach(() => {
  h.activate.mockReset();
  h.meta.mockReturnValue({ data: { role: "owner" } });
  h.rubric.mockReturnValue({ data: { criteria: [{ name: "A", description: "", weight: 1, scale_max: 5 }], funder_notes: "n", updated_at: "x" } });
});
afterEach(() => { vi.restoreAllMocks(); });

it("owner sees the editor and activates", async () => {
  h.setup.mockReturnValue({ data: { status: "waiting_for_rubric_review", last_error: null } });
  renderWithProviders(<WorkspaceSetup id="ws1" />);
  await waitFor(() => expect(screen.getByLabelText(/name for a/i)).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: /confirm & activate/i }));
  expect(h.activate).toHaveBeenCalled();
});

it("viewer sees a waiting message, no editor", () => {
  h.setup.mockReturnValue({ data: { status: "waiting_for_rubric_review", last_error: null } });
  h.meta.mockReturnValue({ data: { role: "member" } });
  renderWithProviders(<WorkspaceSetup id="ws1" />);
  expect(screen.getByText(/waiting for the owner/i)).toBeInTheDocument();
  expect(screen.queryByLabelText(/name for a/i)).toBeNull();
});

it("failed shows the error to the owner", () => {
  h.setup.mockReturnValue({ data: { status: "failed", last_error: "boom" } });
  renderWithProviders(<WorkspaceSetup id="ws1" />);
  expect(screen.getByText(/boom/)).toBeInTheDocument();
});
