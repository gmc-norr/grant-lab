import { MantineProvider } from "@mantine/core";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import * as branchesMod from "../api/branches";
import { ApiError } from "../api/client";
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

/** A stand-in for one `useMutation` result.
 *
 *  The previous version of this file mocked all three as bare `vi.fn()` with no
 *  `isError`, no `error` and no `reset` — which is exactly why the review found
 *  the swallowed switch/delete failures: the error path could not be reached
 *  from this harness at all, so no test here could have caught it. Passing an
 *  `error` makes the mutation behave like a failed one; the default succeeds and
 *  invokes the caller's `onSuccess`, as TanStack does. */
function mutationStub(error?: unknown) {
  const mutate = vi.fn((_vars: unknown, opts?: { onSuccess?: () => void }) => {
    if (!error) opts?.onSuccess?.();
  });
  return {
    mutate,
    reset: vi.fn(),
    isPending: false,
    isError: error !== undefined,
    error,
  };
}

function mockMutations(errors?: { create?: unknown; switchTo?: unknown; del?: unknown }) {
  const create = mutationStub(errors?.create);
  const switchTo = mutationStub(errors?.switchTo);
  const del = mutationStub(errors?.del);
  vi.spyOn(branchesMod, "useCreateBranch").mockReturnValue(create as never);
  vi.spyOn(branchesMod, "useSwitchBranch").mockReturnValue(switchTo as never);
  vi.spyOn(branchesMod, "useDeleteBranch").mockReturnValue(del as never);
  return { create: create.mutate, switchTo: switchTo.mutate, del: del.mutate };
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
    expect(switchTo).toHaveBeenCalledWith("main", expect.anything());
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
    expect(del).toHaveBeenCalledWith("main", expect.anything());
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

  it("keeps the switch modal open and says why when the server refuses", async () => {
    // Review 2026-09-02 A7/F5. The confirm modal closed unconditionally on
    // click, so a 409 (the server ABORTS a switch it cannot linearize against
    // in-flight persists) or a 404 (branch already gone) left the bar showing
    // the old branch with nothing said at all. The user reads that as "nothing
    // happened" and clicks again — silently, every time.
    mockBranches("test", ["main", "test"]);
    const { switchTo } = mockMutations({
      switchTo: new ApiError("conflict", "switch aborted: a persist is in flight"),
    });
    setup(<BranchBar id="w1" {...NOOP} />);
    fireEvent.click(screen.getByRole("button", { name: "Branch: test" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "main" }));
    await userEvent.click(await screen.findByRole("button", { name: "Switch branch" }));

    expect(switchTo).toHaveBeenCalledWith("main", expect.anything());
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("switch aborted: a persist is in flight");
    // Still open: closing it is what threw the failure away.
    expect(screen.getByRole("dialog", { name: "Switch branch" })).toBeInTheDocument();
  });

  it("keeps the delete modal open and says why when the delete fails", async () => {
    mockBranches("test", ["main", "test"]);
    const { del } = mockMutations({ del: new ApiError("not_found", "no such branch") });
    setup(<BranchBar id="w1" {...NOOP} />);
    fireEvent.click(screen.getByRole("button", { name: "Branch: test" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Delete main" }));
    await userEvent.click(await screen.findByRole("button", { name: "Delete branch" }));

    expect(del).toHaveBeenCalledWith("main", expect.anything());
    expect(await screen.findByRole("alert")).toHaveTextContent("no such branch");
    expect(screen.getByRole("dialog", { name: "Delete branch" })).toBeInTheDocument();
  });

  it("says why a create failed instead of leaving a blank dialog", async () => {
    mockBranches("test", ["main", "test"]);
    mockMutations({ create: new ApiError("conflict", "branch name already taken") });
    setup(<BranchBar id="w1" {...NOOP} />);
    fireEvent.click(screen.getByRole("button", { name: "Branch: test" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Create new branch…" }));
    await userEvent.type(await screen.findByLabelText("Branch name"), "spike");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("branch name already taken");
  });

  it("a successful switch still closes its modal", async () => {
    // The other half of the contract: "keep it open" must not become "never close".
    mockBranches("test", ["main", "test"]);
    mockMutations();
    setup(<BranchBar id="w1" {...NOOP} />);
    fireEvent.click(screen.getByRole("button", { name: "Branch: test" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "main" }));
    await userEvent.click(await screen.findByRole("button", { name: "Switch branch" }));

    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Switch branch" })).toBeNull(),
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
