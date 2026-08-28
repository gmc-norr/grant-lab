import { MantineProvider } from "@mantine/core";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useSearchParams } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import * as versionsMod from "../api/versions";
import { VersionGraph } from "./VersionGraph";

const ROW = (sha: string, subject: string, created_by: string | null = "user") => ({
  commit_sha: sha,
  committed_at: "2026-08-28T10:00:00+00:00",
  subject,
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
    <MantineProvider>
      <MemoryRouter initialEntries={[initial]}>
        <VersionGraph id="ws1" />
        <UrlProbe />
      </MemoryRouter>
    </MantineProvider>,
  );
  // SidebarGroup with defaultOpen={false} mounts children only when open.
  // Click the group header to expand it so tests can query rows and notes.
  fireEvent.click(screen.getByRole("button", { name: /versions/i }));
  return result;
}

describe("VersionGraph", () => {
  it("renders one row per version, newest first, with sha and subject", () => {
    mockList({ versions: [ROW("aaaaaaa", "newer one"), ROW("bbbbbbb", "older one")] });
    wrap();
    const rows = screen.getAllByRole("button", { name: /aaaaaaa|bbbbbbb/ });
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain("aaaaaaa");
    expect(rows[0].textContent).toContain("newer one");
    expect(rows[1].textContent).toContain("bbbbbbb");
  });

  it("shortens the sha to 7 characters", () => {
    mockList({ versions: [ROW("0123456789abcdef0123456789abcdef01234567", "s")] });
    wrap();
    expect(screen.getByText("0123456")).toBeInTheDocument();
  });

  it("marks the role on the dot, including no provenance", () => {
    mockList({ versions: [ROW("aaaaaaa", "s", "agent"), ROW("bbbbbbb", "s", null)] });
    const { container } = wrap();
    const dots = [...container.querySelectorAll("[data-role]")].map((n) =>
      (n as HTMLElement).dataset.role,
    );
    expect(dots).toEqual(["agent", "none"]);
  });

  it("clicking a dot writes BOTH the selection and tab=history to the URL", async () => {
    // The whole point of selectAndOpen. Asserting the URL, not the calls:
    // two separate calls both happen while the feature is broken (spec 6.1).
    const user = userEvent.setup();
    mockList({ versions: [ROW("aaaaaaa", "s")] });
    wrap();
    await user.click(screen.getByRole("button", { name: /aaaaaaa/ }));
    const url = screen.getByTestId("url").textContent ?? "";
    expect(url).toContain("a=aaaaaaa");
    expect(url).toContain("tab=history");
  });

  it("marks a selected version with aria-pressed", () => {
    mockList({ versions: [ROW("aaaaaaa", "s"), ROW("bbbbbbb", "s")] });
    wrap("/cockpit/ws1?tab=history&a=aaaaaaa");
    expect(screen.getByRole("button", { name: /aaaaaaa/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /bbbbbbb/ })).toHaveAttribute("aria-pressed", "false");
  });

  it("clicking a selected version deselects it", async () => {
    const user = userEvent.setup();
    mockList({ versions: [ROW("aaaaaaa", "s")] });
    wrap("/cockpit/ws1?tab=history&a=aaaaaaa");
    await user.click(screen.getByRole("button", { name: /aaaaaaa/ }));
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
    await user.hover(screen.getByRole("button", { name: /aaaaaaa/ }));
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
    await user.hover(screen.getByRole("button", { name: /aaaaaaa/ }));
    expect(await screen.findByText(/because you asked: Tighten the aims section/i)).toBeInTheDocument();
  });

  it("shows no reason line at all when there is no prompt", async () => {
    // Null for every reconciled row and every commit with no DB row. Absence is
    // ordinary, so there must be no placeholder — and the card still opens.
    const user = userEvent.setup();
    mockList({ versions: [ROW("aaaaaaa", "collab checkpoint: draft", null)] });
    wrap();
    await user.hover(screen.getByRole("button", { name: /aaaaaaa/ }));
    await screen.findByTestId("version-card-aaaaaaa");
    expect(screen.queryByText(/because you asked/i)).toBeNull();
  });
});
