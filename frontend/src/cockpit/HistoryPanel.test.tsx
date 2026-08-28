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
  // mockImplementation, NOT mockReturnValue: the returned spy must record the
  // arguments the panel passed. `useVersion(id, artifact, sha)` is the whole
  // mechanism that decides WHETHER and WHICH version is fetched, and a mock
  // that ignores its arguments cannot fail when that decision is wrong.
  return vi.spyOn(versionsMod, "useVersion").mockImplementation(
    (() => ({
      data: markdown == null ? null : { commit_sha: "x", artifact: "draft", markdown },
      isLoading: false,
      isError: false,
    })) as never,
  );
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
    expect(screen.getByText(/Older versions exist/i)).toBeInTheDocument();
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

  it("asks for nothing while no row is selected", () => {
    mockList({ versions: [ROW("aaaaaaa", "one")] });
    const spy = mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(spy).toHaveBeenCalledWith("ws1", "draft", null);
  });

  it("asks for the selected sha, and leaves the second slot idle", () => {
    mockList({ versions: [ROW("aaaaaaa", "one")] });
    const spy = mockVersion("# an old draft\n");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa");
    expect(spy).toHaveBeenCalledWith("ws1", "draft", "aaaaaaa");
    // The second slot stays null so its query stays disabled — one selection
    // must cost one request, not two.
    expect(spy).toHaveBeenCalledWith("ws1", "draft", null);
  });

  it("asks for BOTH versions once two rows are selected", () => {
    // This assertion is the INVERSE of the one it replaces. While the panel had
    // a single `useVersion` call and comparing did not exist, "stop asking for a
    // single version once two are selected" was the correct gate. Comparing
    // fetches both sides, so the gate moves rather than disappears: both shas
    // must be requested and neither slot may be left idle. Dropping one of the
    // two calls, or leaving the second one `null`, reddens this.
    mockList({ versions: [ROW("bbbbbbb", "newer"), ROW("aaaaaaa", "older")] });
    const spy = mockVersion(null);
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb");
    expect(spy).toHaveBeenCalledWith("ws1", "draft", "aaaaaaa");
    expect(spy).toHaveBeenCalledWith("ws1", "draft", "bbbbbbb");
    expect(spy).not.toHaveBeenCalledWith("ws1", "draft", null);
  });

  it("says so when the version list itself fails to load", () => {
    // Distinct from history_available:false, which is a successful response.
    // Without this branch a failed request renders an empty header, which reads
    // as "no versions yet".
    vi.spyOn(versionsMod, "useVersions").mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
    } as never);
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(screen.getByText(/Could not load version history/i)).toBeInTheDocument();
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

describe("HistoryPanel comparison", () => {
  beforeEach(() => {
    // Block body, not a concise arrow: `restoreAllMocks()` returns VitestUtils,
    // and `pnpm exec tsc --noEmit` rejects returning it from a void callback.
    vi.restoreAllMocks();
  });

  /** Two versions: the OLDER one is second in the list (newest-first). */
  function mockPair(olderMd: string, newerMd: string) {
    mockList({
      versions: [ROW("bbbbbbb", "newer"), ROW("aaaaaaa", "older")],
    });
    vi.spyOn(versionsMod, "useVersion").mockImplementation(
      ((_id: string, _artifact: string, sha: string | null) => ({
        data:
          sha === "aaaaaaa"
            ? { commit_sha: "aaaaaaa", artifact: "draft", markdown: olderMd }
            : sha === "bbbbbbb"
              ? { commit_sha: "bbbbbbb", artifact: "draft", markdown: newerMd }
              : null,
        isLoading: false,
        isError: false,
      })) as never,
    );
  }

  it("diffs older against newer, refined to word level", () => {
    mockPair("We recruit 40 patients.\n", "We recruit 120 patients.\n");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb");
    expect(
      Array.from(document.querySelectorAll('[data-diff="del"] span[data-span="del"]')).map(
        (s) => s.textContent,
      ),
    ).toEqual(["40"]);
    expect(
      Array.from(document.querySelectorAll('[data-diff="add"] span[data-span="add"]')).map(
        (s) => s.textContent,
      ),
    ).toEqual(["120"]);
  });

  it("selection ORDER does not change the view — direction is a contract", () => {
    // THE gate. lineDiff assigns `del` to its FIRST argument, so the panel must
    // sort by the list's own order (newest-first) and always pass older, newer.
    mockPair("We recruit 40 patients.\n", "We recruit 120 patients.\n");
    const forward = wrap(
      <HistoryPanel id="ws1" />,
      "/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb",
    );
    const forwardHtml = document.querySelector("[data-testid='version-diff']")?.innerHTML;
    forward.unmount();

    mockPair("We recruit 40 patients.\n", "We recruit 120 patients.\n");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=bbbbbbb&b=aaaaaaa");
    const reverseHtml = document.querySelector("[data-testid='version-diff']")?.innerHTML;

    expect(reverseHtml).toBe(forwardHtml);
  });

  it("shows NO diff until the list has loaded — direction comes from the list", () => {
    // The direction rule reads the version list to learn which sha is older.
    // Before the list resolves `order` is [], both indexOf calls return -1, and
    // `-1 < -1` is false — so the panel would fall through to raw URL order and
    // render every addition as a deletion. The two version payloads are smaller
    // than the list and can easily win the race, so this is reachable from an
    // ordinary deep link, and every other comparison test mocks the list as
    // already loaded and therefore cannot enter this state.
    vi.spyOn(versionsMod, "useVersions").mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
    } as never);
    vi.spyOn(versionsMod, "useVersion").mockImplementation(
      ((_id: string, _artifact: string, sha: string | null) => ({
        data: sha == null ? null : { commit_sha: sha, artifact: "draft", markdown: "x\n" },
        isLoading: false,
        isError: false,
      })) as never,
    );
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=bbbbbbb&b=aaaaaaa");
    expect(document.querySelector("[data-testid='version-diff']")).toBeNull();
  });

  it("says a selected version is not in this list instead of loading forever", () => {
    // Reachable three ways, all from a hand-made or stale URL: an ABBREVIATED
    // sha (valid to git, resolves server-side, matches no row here), a sha from
    // the OTHER artifact, and a sha whose commit has since been filtered out.
    // Without this the panel waits on a direction it can never determine, and
    // shows "Loading both versions…" indefinitely.
    mockList({ versions: [ROW("bbbbbbb", "newer"), ROW("aaaaaaa", "older")] });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=abc1234&b=bbbbbbb");
    expect(screen.getByText(/not in this list/i)).toBeInTheDocument();
    expect(screen.queryByText(/Loading both versions/i)).not.toBeInTheDocument();
  });

  it("falls back to the two versions SIDE BY SIDE above the ceiling, with a reason", () => {
    // Spec §7.1 and §11 both require the two versions to be rendered side by
    // side, not just a sentence. Both payloads are already in hand, and being
    // told "too large" with nothing to read is a dead end — the older assertion
    // (note present, no diff) passed happily while showing the user nothing.
    const huge = Array(2001).fill("x").join("\n");
    mockPair(huge + "\nOLDMARKER", huge + "\nNEWMARKER");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb");
    expect(screen.getByText(/too large to compare/i)).toBeInTheDocument();
    expect(document.querySelector("[data-testid='version-diff']")).toBeNull();
    const panes = document.querySelectorAll("[data-testid='version-side']");
    expect(panes).toHaveLength(2);
    expect(panes[0].textContent).toContain("OLDMARKER");
    expect(panes[1].textContent).toContain("NEWMARKER");
  });

  it("reports a failed read when only ?b= is set", () => {
    // The single-version error was gated on `nav.a != null`, but the FETCH falls
    // back to `nav.a ?? nav.b`. A hand-trimmed `?b=` whose read fails therefore
    // rendered nothing at all — the dead state the `?? nav.b` fix was meant to
    // remove, just moved one branch along.
    mockList({ versions: [ROW("bbbbbbb", "newer")] });
    vi.spyOn(versionsMod, "useVersion").mockReturnValue({
      data: null,
      isLoading: false,
      isError: true,
    } as never);
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&b=bbbbbbb");
    expect(screen.getByText(/could not be read/i)).toBeInTheDocument();
  });

  it("exactly 2,000 lines still diffs — the boundary is inclusive", () => {
    const atLimit = Array(2000).fill("x").join("\n");
    mockPair(atLimit, atLimit.replace("x", "y"));
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb");
    expect(screen.queryByText(/too large to compare/i)).toBeNull();
    expect(document.querySelector("[data-testid='version-diff']")).not.toBeNull();
  });
});
