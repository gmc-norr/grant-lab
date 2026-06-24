import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../test-utils";
import { WorkspaceSettingsModal } from "./WorkspaceSettingsModal";

const h = vi.hoisted(() => ({ update: vi.fn(), settings: vi.fn() }));
vi.mock("../api/settings", () => ({
  useSettings: () => h.settings(),
  useUpdateSettings: () => ({ mutate: h.update, isPending: false, isError: false }),
}));

beforeEach(() => {
  h.update.mockReset();
  h.settings.mockReturnValue({
    data: { title: "Proj", status: "drafting", deadline: "2026-09-01", citation_style: "vancouver" },
  });
});
afterEach(() => { vi.restoreAllMocks(); });

describe("WorkspaceSettingsModal", () => {
  it("renders current values and saves a PATCH", async () => {
    renderWithProviders(<WorkspaceSettingsModal id="ws1" opened onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByLabelText(/title/i)).toHaveValue("Proj"));
    fireEvent.change(screen.getByLabelText(/title/i), { target: { value: "Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(h.update).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Renamed", citation_style: "vancouver" }),
      expect.anything(),
    );
  });
});
