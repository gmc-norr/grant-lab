import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../test-utils";
import { WorkspaceSettingsModal } from "./WorkspaceSettingsModal";

const h = vi.hoisted(() => ({ update: vi.fn(), settings: vi.fn(), del: vi.fn(), navigate: vi.fn(), delError: false }));
vi.mock("../api/settings", () => ({
  useSettings: () => h.settings(),
  useUpdateSettings: () => ({ mutate: h.update, isPending: false, isError: false }),
  useDeleteWorkspace: () => ({ mutate: h.del, isPending: false, isError: h.delError }),
}));
vi.mock("react-router-dom", async (orig) => ({
  ...(await orig<typeof import("react-router-dom")>()),
  useNavigate: () => h.navigate,
}));

beforeEach(() => {
  h.update.mockReset();
  h.del.mockReset();
  h.navigate.mockReset();
  h.delError = false;
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

  it("shows an error note and does NOT navigate when the delete fails", async () => {
    h.delError = true;
    renderWithProviders(<WorkspaceSettingsModal id="ws1" opened onClose={vi.fn()} />);
    expect(await screen.findByText(/could not delete workspace/i)).toBeInTheDocument();
    expect(h.navigate).not.toHaveBeenCalled();
  });

  it("does not claim deleting removes uploaded files", async () => {
    renderWithProviders(<WorkspaceSettingsModal id="ws1" opened onClose={vi.fn()} />);
    const body = await screen.findByText(/deletes the workspace/i);
    expect(body.textContent).not.toMatch(/scrub/i);
    expect(body.textContent).toMatch(/kept|retained|remain/i);
  });

  it("the confirmation prompt says the same thing as the description", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderWithProviders(<WorkspaceSettingsModal id="ws1" opened onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: /delete workspace/i }));
    const prompt = confirmSpy.mock.calls[0][0] as string;
    expect(prompt).not.toMatch(/scrub/i);
    expect(prompt).toMatch(/kept|retained|remain/i);
  });

  it("body copy and confirm dialog say an admin can restore — deletion is not a permanent loss", async () => {
    // Slice A wrote "you cannot undo this" when restore did not exist. Now that
    // restore is live, both strings must tell the owner that an administrator
    // can restore it — without this test, reverting the copy change would leave
    // the suite green while silently reinstating a false guarantee.
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderWithProviders(<WorkspaceSettingsModal id="ws1" opened onClose={vi.fn()} />);
    const body = await screen.findByText(/deletes the workspace/i);
    // The old absolute claim must be gone from the body.
    expect(body.textContent).not.toMatch(/you cannot undo this\./i);
    // The replacement must say an admin can restore.
    expect(body.textContent).toMatch(/administrator can restore/i);

    // Same check on the confirm dialog.
    fireEvent.click(await screen.findByRole("button", { name: /delete workspace/i }));
    const prompt = confirmSpy.mock.calls[0][0] as string;
    expect(prompt).not.toMatch(/you cannot undo this\./i);
    expect(prompt).toMatch(/administrator can restore/i);
  });
});
