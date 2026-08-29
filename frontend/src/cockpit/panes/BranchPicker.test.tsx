import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { BranchPicker } from "./BranchPicker";

const state = {
  data: { branches: ["main", "v2"], current: "main" } as const,
  switchTo: vi.fn(),
  remove: vi.fn(),
};

vi.mock("../../api/branches", () => ({
  useBranches: () => ({ data: state.data, isPending: false }),
  useCreateBranch: () => ({ mutate: vi.fn(), isPending: false }),
  useSwitchBranch: () => ({ mutate: state.switchTo, isPending: false }),
  useDeleteBranch: () => ({ mutate: state.remove, isPending: false }),
}));

function renderIt() {
  return render(<BranchPicker id="w1" />);
}

describe("BranchPicker", () => {
  it("shows every branch and marks the current one", () => {
    renderIt();
    expect(screen.getByRole("button", { name: /main/ })).toBeInTheDocument();
    expect(screen.getByText("v2")).toBeInTheDocument();
  });

  it("switching asks for confirmation before it evicts anybody", async () => {
    renderIt();
    await userEvent.click(screen.getByRole("button", { name: /main/ }));
    // Exact name, not /v2/: the menu also has a "Delete v2" menuitem, which /v2/
    // would match too (RTL then throws "multiple elements"). The switch item's
    // accessible name is exactly the branch name.
    await userEvent.click(screen.getByRole("menuitem", { name: "v2" }));
    // A switch disconnects every other editor and throws away unsaved keystrokes.
    // It must not happen on a single stray click.
    expect(state.switchTo).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /switch branch/i }));
    await waitFor(() => expect(state.switchTo).toHaveBeenCalledWith("v2"));
  });

  it("deleting warns that it destroys commits", async () => {
    renderIt();
    await userEvent.click(screen.getByRole("button", { name: /main/ }));
    await userEvent.click(screen.getByRole("menuitem", { name: /delete v2/i }));
    expect(screen.getByText(/cannot be undone/i)).toBeInTheDocument();
    expect(state.remove).not.toHaveBeenCalled();
  });

  it("offers no delete for the branch you are on", async () => {
    renderIt();
    await userEvent.click(screen.getByRole("button", { name: /main/ }));
    expect(screen.queryByRole("menuitem", { name: /delete main/i })).toBeNull();
  });
});
