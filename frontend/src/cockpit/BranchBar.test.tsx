import { MantineProvider } from "@mantine/core";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import * as branchesMod from "../api/branches";
import { BranchBar } from "./BranchBar";

function mockBranches(current: string, branches: string[]) {
  vi.spyOn(branchesMod, "useBranches").mockReturnValue({
    data: {
      current,
      branches,
      tips: Object.fromEntries(branches.map((b, i) => [b, String(i).repeat(40)])),
    },
    isError: false,
  } as never);
}

function mockMutations() {
  const create = vi.fn();
  const switchTo = vi.fn();
  const del = vi.fn();
  vi.spyOn(branchesMod, "useCreateBranch").mockReturnValue({
    mutate: create,
    isPending: false,
  } as never);
  vi.spyOn(branchesMod, "useSwitchBranch").mockReturnValue({
    mutate: switchTo,
    isPending: false,
  } as never);
  vi.spyOn(branchesMod, "useDeleteBranch").mockReturnValue({
    mutate: del,
    isPending: false,
  } as never);
  return { create, switchTo, del };
}

/** `env="test"` is Mantine's own switch for this: without it the Menu dropdown
 *  sits at `display: none` for the length of its transition, which jsdom never
 *  finishes, so every item is invisible to `getByRole`. Measured -- the element
 *  is in the DOM the whole time, its parent just never becomes visible. */
function setup(node: ReactNode) {
  return render(<MantineProvider env="test">{node}</MantineProvider>);
}

const NOOP = {
  branchFrom: null,
  onBranchFromHandled: () => {},
  switchTo: null,
  onSwitchHandled: () => {},
};

describe("BranchBar", () => {
  it("shows the current branch", () => {
    mockBranches("test", ["main", "test"]);
    mockMutations();
    setup(<BranchBar id="w1" {...NOOP} />);
    expect(screen.getByRole("button", { name: "Branch: test" })).toBeInTheDocument();
  });

  it("puts switching and deleting in separately labelled groups", async () => {
    mockBranches("test", ["main", "test"]);
    mockMutations();
    setup(<BranchBar id="w1" {...NOOP} />);
    fireEvent.click(screen.getByRole("button", { name: "Branch: test" }));

    // Await the ITEM, not just the label: the label renders a tick before the
    // items' accessible names settle.
    expect(await screen.findByRole("menuitem", { name: "main" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Delete main" })).toBeInTheDocument();
    expect(screen.getByText("Switch to")).toBeInTheDocument();
    expect(screen.getByText("Delete")).toBeInTheDocument();
  });

  it("never offers to switch to, or delete, the branch you are on", async () => {
    mockBranches("test", ["main", "test"]);
    mockMutations();
    setup(<BranchBar id="w1" {...NOOP} />);
    fireEvent.click(screen.getByRole("button", { name: "Branch: test" }));
    await screen.findByRole("menuitem", { name: "main" });

    expect(screen.queryByRole("menuitem", { name: "test" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Delete test" })).toBeNull();
  });

  it("confirms before switching, and only then mutates", async () => {
    mockBranches("test", ["main", "test"]);
    const { switchTo } = mockMutations();
    setup(<BranchBar id="w1" {...NOOP} />);
    fireEvent.click(screen.getByRole("button", { name: "Branch: test" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "main" }));

    expect(switchTo).not.toHaveBeenCalled();
    expect(await screen.findByRole("dialog", { name: "Switch branch" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Switch branch" }));
    expect(switchTo).toHaveBeenCalledWith("main");
  });

  it("confirms before deleting, and only then mutates", async () => {
    mockBranches("test", ["main", "test"]);
    const { del } = mockMutations();
    setup(<BranchBar id="w1" {...NOOP} />);
    fireEvent.click(screen.getByRole("button", { name: "Branch: test" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Delete main" }));

    expect(del).not.toHaveBeenCalled();
    expect(await screen.findByRole("dialog", { name: "Delete branch" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Delete branch" }));
    expect(del).toHaveBeenCalledWith("main");
  });

  it("creates from HEAD with the trimmed name when opened from the bar", async () => {
    mockBranches("test", ["main", "test"]);
    const { create } = mockMutations();
    setup(<BranchBar id="w1" {...NOOP} />);
    fireEvent.click(screen.getByRole("button", { name: "Branch: test" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Create new branch…" }));
    await userEvent.type(await screen.findByLabelText("Branch name"), "  spike  ");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(create).toHaveBeenCalledWith({ name: "spike", fromSha: undefined }, expect.anything());
  });

  it("creates from the given sha when the pane asked to branch from a row", async () => {
    mockBranches("test", ["main", "test"]);
    const { create } = mockMutations();
    const sha = "a".repeat(40);
    setup(
      <BranchBar
        id="w1"
        branchFrom={{ sha, subject: "seed proposal" }}
        onBranchFromHandled={() => {}}
        switchTo={null}
        onSwitchHandled={() => {}}
      />,
    );

    // The dialog opens by itself; the row already made the choice.
    expect(screen.getByRole("dialog", { name: "New branch" })).toBeInTheDocument();
    expect(screen.getByText(/seed proposal/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Branch name"), "from-row");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(create).toHaveBeenCalledWith({ name: "from-row", fromSha: sha }, expect.anything());
  });

  it("opens the switch confirm when the pane asks, and clears the request on cancel", async () => {
    mockBranches("test", ["main", "test"]);
    const { switchTo } = mockMutations();
    const onSwitchHandled = vi.fn();
    setup(
      <BranchBar
        id="w1"
        branchFrom={null}
        onBranchFromHandled={() => {}}
        switchTo="main"
        onSwitchHandled={onSwitchHandled}
      />,
    );

    expect(screen.getByRole("dialog", { name: "Switch branch" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    // Clearing is what lets the SAME pill be clicked again: `switchTo` is a
    // string, so a stale value makes the second click a React no-op.
    expect(onSwitchHandled).toHaveBeenCalled();
    expect(switchTo).not.toHaveBeenCalled();
  });

  it("stays visible and inert when the branch list cannot be read", () => {
    vi.spyOn(branchesMod, "useBranches").mockReturnValue({
      data: undefined,
      isError: true,
    } as never);
    const { create, switchTo: doSwitch } = mockMutations();
    const onBranchFromHandled = vi.fn();
    const onSwitchHandled = vi.fn();
    // Both pending requests are supplied, because both arrive as PROPS from
    // VersionGraph and neither goes through the disabled button.
    setup(
      <BranchBar
        id="w1"
        branchFrom={{ sha: "a".repeat(40), subject: "a commit" }}
        onBranchFromHandled={onBranchFromHandled}
        switchTo="main"
        onSwitchHandled={onSwitchHandled}
      />,
    );
    // A workspace still in setup answers 409. That is ordinary, not an error to
    // shout about -- but the bar must not vanish and leave a headless pane.
    expect(screen.getByRole("button", { name: /^Branch:/ })).toBeDisabled();

    // "Inert" has to mean the whole component. Asserting only that the button
    // is disabled was a gate that could not fail: until 2026-08-31 either
    // pending request opened a fully enabled dialog whose action fired a
    // mutation at the same server that had just refused the branch list.
    expect(screen.queryByRole("dialog", { name: "New branch" })).toBeNull();
    expect(screen.queryByRole("dialog", { name: "Switch branch" })).toBeNull();
    expect(create).not.toHaveBeenCalled();
    expect(doSwitch).not.toHaveBeenCalled();

    // And both requests are cleared, so neither springs open by itself the
    // moment the list recovers.
    expect(onBranchFromHandled).toHaveBeenCalled();
    expect(onSwitchHandled).toHaveBeenCalled();
  });
});
