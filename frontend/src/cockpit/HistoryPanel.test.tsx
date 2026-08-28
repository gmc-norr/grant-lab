import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as versionsMod from "../api/versions";
import { theme } from "../theme/theme";
import { HistoryPanel } from "./HistoryPanel";

function wrap(ui: ReactNode, initial = "/cockpit/ws1?tab=history") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter initialEntries={[initial]}>
        <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
      </MemoryRouter>
    </MantineProvider>,
  );
}

const ROW = (
  sha: string,
  subject: string,
  created_by: string | null = "user",
  turn_prompt: string | null = null,
) => ({
  commit_sha: sha,
  committed_at: "2026-08-28T10:00:00+00:00",
  subject,
  created_by,
  triggering_turn_id: turn_prompt == null ? null : "turn-1",
  turn_prompt,
});

function mockList(data: Partial<versionsMod.VersionList>) {
  vi.spyOn(versionsMod, "useVersions").mockReturnValue({
    data: { versions: [], truncated: false, history_available: true, ...data },
    isLoading: false,
    isError: false,
  } as never);
}

function mockVersion(markdown: string | null) {
  vi.spyOn(versionsMod, "useVersion").mockReturnValue({
    data: markdown == null ? null : { commit_sha: "x", artifact: "draft", markdown },
    isLoading: false,
    isError: false,
  } as never);
}

describe("HistoryPanel", () => {
  beforeEach(() => {
    // Block body, not a concise arrow: `restoreAllMocks()` returns VitestUtils,
    // and `pnpm exec tsc --noEmit` rejects returning it from a void callback.
    vi.restoreAllMocks();
  });

  it("shows the zero-version invitation, not an empty list", () => {
    mockList({ versions: [], history_available: true });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(screen.getByText(/No versions yet/i)).toBeInTheDocument();
  });

  it("distinguishes an unreadable history from an empty one", () => {
    mockList({ versions: [], history_available: false });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(screen.getByText(/not available for this workspace yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/No versions yet/i)).not.toBeInTheDocument();
  });

  it("says so when the list is truncated", () => {
    mockList({ versions: [ROW("aaaaaaa", "one")], truncated: true });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(screen.getByText(/200 most recent versions/i)).toBeInTheDocument();
  });

  it("renders a row's role badge and its subject verbatim", () => {
    mockList({ versions: [ROW("aaaaaaa", "user draft edit: Aims (user:7)")] });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(screen.getByText("user draft edit: Aims (user:7)")).toBeInTheDocument();
    expect(screen.getByText("user")).toBeInTheDocument();
  });

  it("shows WHY a version exists when a prompt caused it", () => {
    mockList({
      versions: [ROW("aaaaaaa", "agent draft edit", "agent", "Tighten the aims section")],
    });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(screen.getByText(/because you asked: Tighten the aims section/i)).toBeInTheDocument();
  });

  it("shows no reason line at all when there is no prompt", () => {
    mockList({ versions: [ROW("aaaaaaa", "collab checkpoint: draft", null)] });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(screen.queryByText(/because you asked/i)).not.toBeInTheDocument();
  });

  it("renders a row with no provenance as ordinary, not as an error", () => {
    mockList({ versions: [ROW("aaaaaaa", "collab checkpoint: draft", null)] });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(screen.getByText("not recorded")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("opens a version read-only when one row is selected", async () => {
    mockList({ versions: [ROW("aaaaaaa", "one"), ROW("bbbbbbb", "two")] });
    mockVersion("# an old draft\n");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa");
    expect(screen.getByText(/# an old draft/)).toBeInTheDocument();
    // Read-only: no textbox anywhere in the panel.
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("selecting a row puts its sha in the URL", async () => {
    // MemoryRouter keeps its history in memory and never touches
    // window.location, so the URL itself isn't observable here; the round-trip
    // through ?a= is covered directly by historyNav.test.ts. What this test can
    // observe is the row reacting to its own selection.
    mockList({ versions: [ROW("aaaaaaa", "one")] });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    await userEvent.click(screen.getByRole("button", { name: /one/ }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /one/ })).toHaveAttribute(
        "aria-pressed",
        "true",
      ),
    );
  });
});
