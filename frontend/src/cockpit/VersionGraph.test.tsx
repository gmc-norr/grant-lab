import { MantineProvider } from "@mantine/core";
import { act, createEvent, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useSearchParams } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as branchesMod from "../api/branches";
import * as versionsMod from "../api/versions";
import { VersionGraph } from "./VersionGraph";

const ROW = (
  sha: string,
  subject: string,
  created_by: string | null = "user",
  parents: string[] = [],
) => ({
  commit_sha: sha,
  committed_at: "2026-08-28T10:00:00+00:00",
  subject,
  parents,
  created_by,
  triggering_turn_id: null,
  turn_prompt: null,
});

function mockList(data: Partial<versionsMod.VersionList>) {
  vi.spyOn(versionsMod, "useVersions").mockReturnValue({
    data: { versions: [], truncated: false, history_available: true, ...data },
    isLoading: false,
    isError: false,
  } as never);
}

function UrlProbe() {
  const [sp] = useSearchParams();
  return <output data-testid="url">{sp.toString()}</output>;
}

function wrap(initial = "/cockpit/ws1?tab=draft") {
  const result = render(
    <MantineProvider env="test">
      <MemoryRouter initialEntries={[initial]}>
        <VersionGraph id="ws1" />
        <UrlProbe />
      </MemoryRouter>
    </MantineProvider>,
  );
  return result;
}

function mockBranches(current: string, tips: Record<string, string>) {
  vi.spyOn(branchesMod, "useBranches").mockReturnValue({
    data: { current, branches: Object.keys(tips), tips },
    isError: false,
  } as never);
}

/** Mirrors `BranchBar.test.tsx`'s stub: a mutation result that succeeds and runs
 *  the caller's `onSuccess`, carrying the `reset`/`isError`/`error` fields the
 *  bar reads. */
function mutationStub() {
  return {
    mutate: vi.fn((_vars: unknown, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.()),
    reset: vi.fn(),
    isPending: false,
    isError: false,
    error: undefined,
  };
}

describe("VersionGraph", () => {
  // The pane now reads useBranches, and BranchBar reads the three mutation
  // hooks. Every test needs them stubbed or the hooks reach TanStack Query with
  // no provider. BranchBar reads `reset`/`isError`/`error` off each of them and
  // now passes `{ onSuccess }` to `mutate`, so the stub has to behave like a
  // real (succeeding) mutation result, not a bare spy.
  beforeEach(() => {
    mockBranches("main", { main: "aaaaaaa" });
    for (const h of ["useCreateBranch", "useSwitchBranch", "useDeleteBranch"] as const) {
      vi.spyOn(branchesMod, h).mockReturnValue(mutationStub() as never);
    }
  });

  it("renders one row per version, newest first, showing the subject", () => {
    mockList({ versions: [ROW("aaaaaaa", "newer one"), ROW("bbbbbbb", "older one")] });
    wrap();
    const rows = screen.getAllByRole("button", { name: /^aaaaaaa|^bbbbbbb/ });
    expect(rows).toHaveLength(2);
    // The sha is no longer PRINTED in the row -- at 276px it left about 60px
    // for the subject. It is still in the accessible name above.
    expect(rows[0].textContent).not.toContain("aaaaaaa");
    expect(rows[0].textContent).toContain("newer one");
    expect(rows[1].textContent).toContain("older one");
  });

  it("keeps the 7-character sha in each row's accessible name", () => {
    // Subjects repeat -- every checkpoint says "collab checkpoint: draft" -- so
    // without the sha two rows would carry the SAME accessible name and a
    // screen-reader user could not tell them apart. This is the assertion that
    // fails if the sha is dropped from the label as well as from the view.
    mockList({
      versions: [
        ROW("0123456789abcdef0123456789abcdef01234567", "collab checkpoint"),
        ROW("fedcba9876543210fedcba9876543210fedcba98", "collab checkpoint"),
      ],
    });
    wrap();
    expect(screen.getByRole("button", { name: "0123456 collab checkpoint" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "fedcba9 collab checkpoint" })).toBeInTheDocument();
  });

  it("marks the role on the dot, including no provenance", () => {
    mockList({ versions: [ROW("aaaaaaa", "s", "agent"), ROW("bbbbbbb", "s", null)] });
    const { container } = wrap();
    const dots = [...container.querySelectorAll("[data-role]")].map((n) =>
      (n as HTMLElement).dataset.role,
    );
    expect(dots).toEqual(["agent", "none"]);
  });

  it("clicking a dot writes BOTH the selection and tab=diff to the URL", async () => {
    // The whole point of selectAndOpen. Asserting the URL, not the calls:
    // two separate calls both happen while the feature is broken (spec 6.1).
    const user = userEvent.setup();
    mockList({ versions: [ROW("aaaaaaa", "s")] });
    wrap();
    await user.click(screen.getByRole("button", { name: /^aaaaaaa/ }));
    const url = screen.getByTestId("url").textContent ?? "";
    expect(url).toContain("a=aaaaaaa");
    expect(url).toContain("tab=diff");
  });

  it("marks a selected version with aria-pressed", () => {
    mockList({ versions: [ROW("aaaaaaa", "s"), ROW("bbbbbbb", "s")] });
    wrap("/cockpit/ws1?tab=history&a=aaaaaaa");
    expect(screen.getByRole("button", { name: /^aaaaaaa/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /^bbbbbbb/ })).toHaveAttribute("aria-pressed", "false");
  });

  it("clicking a selected version deselects it", async () => {
    const user = userEvent.setup();
    mockList({ versions: [ROW("aaaaaaa", "s")] });
    wrap("/cockpit/ws1?tab=history&a=aaaaaaa");
    await user.click(screen.getByRole("button", { name: /^aaaaaaa/ }));
    expect(screen.getByTestId("url").textContent ?? "").not.toContain("a=aaaaaaa");
  });

  it("says there are no versions rather than showing an empty group", () => {
    mockList({ versions: [] });
    wrap();
    expect(screen.getByText(/No versions yet/i)).toBeInTheDocument();
  });

  it("distinguishes an unreadable history from an empty one", () => {
    mockList({ versions: [], history_available: false });
    wrap();
    expect(screen.getByText(/not available/i)).toBeInTheDocument();
    expect(screen.queryByText(/No versions yet/i)).toBeNull();
  });

  it("reports a failed request distinctly from an empty list", () => {
    vi.spyOn(versionsMod, "useVersions").mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
    } as never);
    wrap();
    expect(screen.getByText(/Could not load version history/i)).toBeInTheDocument();
    expect(screen.queryByText(/No versions yet/i)).toBeNull();
  });

  it("says older versions exist when the list is truncated", () => {
    mockList({ versions: [ROW("aaaaaaa", "s")], truncated: true });
    wrap();
    expect(screen.getByText(/Older versions exist/i)).toBeInTheDocument();
  });

  it("shows the untruncated subject and the timestamp on hover", async () => {
    const user = userEvent.setup();
    const long = "a subject far too long to fit inside a 276px side pane without clipping";
    mockList({ versions: [ROW("aaaaaaa", long)] });
    wrap();
    await user.hover(screen.getByRole("button", { name: /^aaaaaaa/ }));
    const card = await screen.findByTestId("version-card-aaaaaaa");
    expect(card.textContent).toContain(long);
    expect(card.textContent).toContain("aaaaaaa");
  });

  it("shows WHY a version exists when a prompt caused it", async () => {
    const user = userEvent.setup();
    mockList({
      versions: [{ ...ROW("aaaaaaa", "agent draft edit", "agent"), turn_prompt: "Tighten the aims section" }],
    });
    wrap();
    await user.hover(screen.getByRole("button", { name: /^aaaaaaa/ }));
    expect(await screen.findByText(/because you asked: Tighten the aims section/i)).toBeInTheDocument();
  });

  it("shows no reason line at all when there is no prompt", async () => {
    // Null for every reconciled row and every commit with no DB row. Absence is
    // ordinary, so there must be no placeholder — and the card still opens.
    const user = userEvent.setup();
    mockList({ versions: [ROW("aaaaaaa", "collab checkpoint: draft", null)] });
    wrap();
    await user.hover(screen.getByRole("button", { name: /^aaaaaaa/ }));
    await screen.findByTestId("version-card-aaaaaaa");
    expect(screen.queryByText(/because you asked/i)).toBeNull();
  });

  it("opens the card on keyboard focus, not only on hover — spec 5.1", async () => {
    mockList({
      versions: [{ ...ROW("aaaaaaa", "agent draft edit", "agent"), turn_prompt: "Tighten the aims section" }],
    });
    wrap();
    act(() => screen.getByRole("button", { name: /^aaaaaaa/ }).focus());
    const card = await screen.findByTestId("version-card-aaaaaaa");
    expect(card.textContent).toContain("because you asked: Tighten the aims section");
  });

  it("offers the Draft/Spec switch in the pane that lists them", () => {
    mockList({ versions: [ROW("aaaaaaa", "one")] });
    wrap();
    expect(screen.getByLabelText(/which artifact/i)).toBeInTheDocument();
  });

  it("switching artifact clears BOTH shas", async () => {
    // A sha from the draft's history is not a version of the spec; leaving one
    // selected would fetch a 404. Pinning the existing setArtifact behaviour at
    // its new call site.
    const user = userEvent.setup();
    mockList({ versions: [ROW("aaaaaaa", "one")] });
    wrap("/cockpit/ws1?tab=history&view=versions&a=aaaaaaa&b=bbbbbbb");
    // Mantine's SegmentedControl hides its radio at opacity 0 — Playwright
    // refuses to click it and userEvent must target the LABEL.
    await user.click(screen.getByText("Spec"));
    const url = new URLSearchParams(screen.getByTestId("url").textContent ?? "");
    expect(url.get("artifact")).toBe("spec");
    expect(url.get("a")).toBeNull();
    expect(url.get("b")).toBeNull();
  });

  // --- branch lanes, pills and row actions --------------------------------

  it("puts a pill on the commit each branch tip points at", () => {
    mockList({ versions: [ROW("aaaaaaa1", "newer", "user", ["bbbbbbb2"]), ROW("bbbbbbb2", "older")] });
    mockBranches("test", { test: "aaaaaaa1", main: "bbbbbbb2" });
    wrap();
    expect(screen.getByRole("button", { name: "Switch to main" })).toBeInTheDocument();
    // The branch you are already on is labelled, not offered as a switch.
    // Queried by accessible NAME, not getByText("test"): the pill renders
    // `⑂ {name}`, so its text content is "⑂ test" and an exact text query misses.
    expect(screen.getByRole("button", { name: "test" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Switch to test" })).toBeNull();
  });

  it("gives no pill to a branch whose tip is not in the list", () => {
    mockList({ versions: [ROW("aaaaaaa1", "newer")] });
    mockBranches("test", { test: "aaaaaaa1", elsewhere: "ccccccc3" });
    wrap();
    expect(screen.queryByRole("button", { name: "Switch to elsewhere" })).toBeNull();
  });

  it("gives every row's lane strip the same width", () => {
    // A fork: the second row needs two columns, the first needs one. Equal
    // widths are what keep the subject text from sliding sideways.
    mockList({
      versions: [
        ROW("aaaaaaa1", "on main", "user", ["ccccccc3"]),
        ROW("bbbbbbb2", "on side", "user", ["ccccccc3"]),
        ROW("ccccccc3", "shared root"),
      ],
    });
    mockBranches("main", { main: "aaaaaaa1" });
    const { container } = wrap();
    const widths = [...container.querySelectorAll("svg")].map((e) => e.getAttribute("width"));
    expect(widths).toHaveLength(3);
    expect(new Set(widths).size).toBe(1);
  });

  it("offers the row actions without needing a hover first", async () => {
    mockList({ versions: [ROW("aaaaaaa1", "newer")] });
    mockBranches("main", { main: "aaaaaaa1" });
    wrap();
    // Always in the DOM: a control revealed only on mouse hover is unreachable
    // by keyboard and invisible to a screen reader.
    fireEvent.click(screen.getByRole("button", { name: "Actions for aaaaaaa" }));
    expect(await screen.findByRole("menuitem", { name: "Branch from here…" })).toBeInTheDocument();
  });

  it("right-clicking a row opens the same actions menu AND suppresses the browser's", async () => {
    mockList({ versions: [ROW("aaaaaaa1", "newer")] });
    mockBranches("main", { main: "aaaaaaa1" });
    wrap();
    // The event is built by hand so its defaultPrevented can be read afterwards.
    // Without this second assertion, deleting `e.preventDefault()` leaves the
    // test green while the native context menu opens on top of ours -- measured
    // 2026-08-31.
    const ev = createEvent.contextMenu(screen.getByText("newer"));
    fireEvent(screen.getByText("newer"), ev);
    expect(await screen.findByRole("menuitem", { name: "Branch from here…" })).toBeInTheDocument();
    expect(ev.defaultPrevented).toBe(true);
  });

  it("offers Branch from here as DISABLED when the branch list cannot be read", async () => {
    // The user has to see why nothing happens. BranchBar refuses the request
    // too, but a silently dead menu item reads as a broken app.
    mockList({ versions: [ROW("aaaaaaa1", "newer")] });
    vi.spyOn(branchesMod, "useBranches").mockReturnValue({
      data: undefined,
      isError: true,
    } as never);
    wrap();
    fireEvent.click(screen.getByRole("button", { name: "Actions for aaaaaaa" }));
    expect(await screen.findByRole("menuitem", { name: "Branch from here…" })).toBeDisabled();
    // Copying a sha needs no branch list, so it stays live.
    expect(screen.getByRole("menuitem", { name: "Copy commit id" })).not.toBeDisabled();
  });

  it("Branch from here opens the new-branch dialog for THAT commit", async () => {
    mockList({ versions: [ROW("aaaaaaa1", "newer"), ROW("bbbbbbb2", "the older one")] });
    mockBranches("main", { main: "aaaaaaa1" });
    wrap();
    fireEvent.click(screen.getByRole("button", { name: "Actions for bbbbbbb" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Branch from here…" }));
    const dialog = await screen.findByRole("dialog", { name: "New branch" });
    // Scoped to the dialog: the subject also appears in the row behind it, and
    // an unscoped query would pass on a dialog that named the WRONG commit.
    expect(within(dialog).getByText(/bbbbbbb · the older one/)).toBeInTheDocument();
  });

  it("clicking a branch pill confirms first, and only then switches", async () => {
    // Without this the entire pill-to-dialog wiring is untested, and a pill that
    // silently did nothing would ship green.
    const stub = mutationStub();
    const switchTo = stub.mutate;
    vi.spyOn(branchesMod, "useSwitchBranch").mockReturnValue(stub as never);
    mockList({ versions: [ROW("aaaaaaa1", "newer"), ROW("bbbbbbb2", "older")] });
    mockBranches("test", { test: "aaaaaaa1", main: "bbbbbbb2" });
    wrap();

    fireEvent.click(screen.getByRole("button", { name: "Switch to main" }));
    expect(switchTo).not.toHaveBeenCalled();
    expect(await screen.findByRole("dialog", { name: "Switch branch" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Switch branch" }));
    expect(switchTo).toHaveBeenCalledWith("main", expect.anything());
  });

  it("a pill cancelled once still opens the dialog when clicked again", async () => {
    // The concrete failure the onSwitchHandled clearing exists to prevent:
    // `switchTo` is a string, so a stale value makes the second click a no-op.
    vi.spyOn(branchesMod, "useSwitchBranch").mockReturnValue(mutationStub() as never);
    mockList({ versions: [ROW("aaaaaaa1", "newer"), ROW("bbbbbbb2", "older")] });
    mockBranches("test", { test: "aaaaaaa1", main: "bbbbbbb2" });
    wrap();

    fireEvent.click(screen.getByRole("button", { name: "Switch to main" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Switch to main" }));

    expect(await screen.findByRole("dialog", { name: "Switch branch" })).toBeInTheDocument();
  });
});
