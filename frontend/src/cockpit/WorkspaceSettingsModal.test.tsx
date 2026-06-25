import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../test-utils";
import { WorkspaceSettingsModal } from "./WorkspaceSettingsModal";

const h = vi.hoisted(() => ({ update: vi.fn(), settings: vi.fn(), del: vi.fn(), navigate: vi.fn() }));
vi.mock("../api/settings", () => ({
  useSettings: () => h.settings(),
  useUpdateSettings: () => ({ mutate: h.update, isPending: false, isError: false }),
  useDeleteWorkspace: () => ({ mutate: h.del, isPending: false, isError: false }),
}));
vi.mock("react-router-dom", async (orig) => ({
  ...(await orig<typeof import("react-router-dom")>()),
  useNavigate: () => h.navigate,
}));

beforeEach(() => {
  h.update.mockReset();
  h.del.mockReset();
  h.navigate.mockReset();
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

  it("deletes after confirm, closes, and navigates to /cockpit", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    h.del.mockImplementation((_vars: unknown, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.());
    const onClose = vi.fn();
    renderWithProviders(<WorkspaceSettingsModal id="ws1" opened onClose={onClose} />);
    fireEvent.click(await screen.findByRole("button", { name: /delete workspace/i }));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("Proj"));
    expect(h.del).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
    expect(h.navigate).toHaveBeenCalledWith("/cockpit");
  });

  it("does nothing when the delete confirm is declined", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    renderWithProviders(<WorkspaceSettingsModal id="ws1" opened onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: /delete workspace/i }));
    expect(h.del).not.toHaveBeenCalled();
    expect(h.navigate).not.toHaveBeenCalled();
  });
});
