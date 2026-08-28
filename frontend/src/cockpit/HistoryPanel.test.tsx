import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import type { ReactNode } from "react";
import { MemoryRouter, useSearchParams } from "react-router-dom";
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

function UrlProbe() {
  const [sp] = useSearchParams();
  return <output data-testid="url">{sp.toString()}</output>;
}

function wrapWithUrl(ui: ReactNode, initial: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter initialEntries={[initial]}>
        <QueryClientProvider client={qc}>
          {ui}
          <UrlProbe />
        </QueryClientProvider>
      </MemoryRouter>
    </MantineProvider>,
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

  it("no longer renders version rows — the side pane owns them now", () => {
    mockList({ versions: [ROW("aaaaaaa", "one"), ROW("bbbbbbb", "two")] });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(screen.queryByRole("button", { name: /one/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /two/ })).toBeNull();
  });

  it("tells you where to pick versions when NEITHER is selected", () => {
    mockList({ versions: [ROW("aaaaaaa", "one"), ROW("bbbbbbb", "two")] });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(screen.getByText(/side pane/i)).toBeInTheDocument();
  });

  it("never asks for two versions when the workspace has only one", () => {
    // "Pick two" is impossible advice against a one-version history: the side
    // pane has exactly one row to click. Asserting the absence of "two" is the
    // point — a message that merely mentions the side pane would pass a
    // getByText(/side pane/) check while still being unfollowable.
    mockList({ versions: [ROW("aaaaaaa", "one")] });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(screen.getByText(/only one version/i)).toBeInTheDocument();
    expect(screen.queryByText(/pick two/i)).toBeNull();
  });

  it("still asks for two once a second version exists", () => {
    mockList({ versions: [ROW("aaaaaaa", "one"), ROW("bbbbbbb", "two")] });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(screen.getByText(/pick two/i)).toBeInTheDocument();
    expect(screen.queryByText(/only one version/i)).toBeNull();
  });

  it("still reads ONE selected version — the empty state is not 'fewer than two'", () => {
    // A single selection renders a read-only document today and must keep doing
    // so. Gating the empty state on "fewer than two" would replace working
    // behaviour with a placeholder.
    mockList({ versions: [ROW("aaaaaaa", "one"), ROW("bbbbbbb", "two")] });
    mockVersion("# an old draft\n");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa");
    expect(screen.getByText(/# an old draft/)).toBeInTheDocument();
    expect(screen.queryByText(/side pane/i)).toBeNull();
  });

  it("does not say 'pick two' when a stale link leaves one version unfindable", () => {
    // `both` comes from the URL, not from clicking: a shared or stale link can
    // carry two shas into a one-version workspace. The recovery message then
    // told the user to pick two rows from a pane holding one.
    mockList({ versions: [ROW("aaaaaaa", "one")] });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=abc1234&b=aaaaaaa");
    expect(screen.getByText(/not in the Versions list/i)).toBeInTheDocument();
    expect(screen.queryByText(/pick two/i)).toBeNull();
    // Absence alone would pass if `pickAdvice` were simply deleted. The
    // replacement must actually say why, and offer the exit.
    expect(screen.getByText(/only one version/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /clear the comparison/i })).toBeInTheDocument();
  });

  it("does not say 'pick two' when a one-version workspace fails to read both", () => {
    mockList({ versions: [ROW("aaaaaaa", "one")] });
    vi.spyOn(versionsMod, "useVersion").mockReturnValue({
      data: null,
      isLoading: false,
      isError: true,
    } as never);
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa&b=aaaaaaa");
    // The same sha in both slots is not a comparison. It reads as a single
    // selection, so this is the single-read failure, and it names the version
    // correctly rather than claiming two.
    expect(screen.getByText(/could not be read/i)).toBeInTheDocument();
    expect(screen.getByText(/only version this workspace has/i)).toBeInTheDocument();
    expect(screen.queryByText(/pick two/i)).toBeNull();
  });

  it("the same sha in both slots reads one version, it does not diff it against itself", () => {
    // nextSelection can never produce a === b (clicking a selected row
    // deselects it), so this arrives only by hand-edited or truncated URL.
    // Diffing a document against itself renders an empty diff and tells the
    // user nothing.
    mockList({ versions: [ROW("bbbbbbb", "two"), ROW("aaaaaaa", "one")] });
    mockVersion("# just this one\n");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa&b=aaaaaaa");
    expect(screen.getByText(/# just this one/)).toBeInTheDocument();
    expect(screen.queryByTestId("version-diff")).toBeNull();
    expect(screen.queryByLabelText(/compare layout/i)).toBeNull();
  });

  it("a failed read in a two-version history offers the exit, not more picking", () => {
    // With exactly two versions and one unreadable there is no readable pair,
    // so "pick two from the list" cannot be followed however many times it is
    // tried. The control is the action.
    mockList({ versions: [ROW("bbbbbbb", "two"), ROW("aaaaaaa", "one")] });
    vi.spyOn(versionsMod, "useVersion").mockReturnValue({
      data: null,
      isLoading: false,
      isError: true,
    } as never);
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb");
    expect(screen.getByText(/could not be read/i)).toBeInTheDocument();
    expect(screen.queryByText(/pick two/i)).toBeNull();
    expect(screen.getAllByRole("button", { name: /clear the comparison/i })).toHaveLength(1);
  });

  it("shows ONE recovery message, not two, when a stale sha is also unreadable", () => {
    // The failed-read and not-in-list branches were independent: a stale sha
    // whose read also failed rendered BOTH, with two Clear buttons. Direction is
    // unknowable here, so the not-in-list message is the only true one.
    mockList({ versions: [ROW("aaaaaaa", "one")] });
    vi.spyOn(versionsMod, "useVersion").mockReturnValue({
      data: null,
      isLoading: false,
      isError: true,
    } as never);
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb");
    expect(screen.getByText(/not in the Versions list/i)).toBeInTheDocument();
    expect(screen.queryByText(/could not be read/i)).toBeNull();
    expect(screen.getAllByRole("button", { name: /clear the comparison/i })).toHaveLength(1);
  });

  it("does not call a stale single selection the workspace's only version", () => {
    // `?a=` carries a sha that is not in this list at all. Saying "it is the
    // only version this workspace has" names the wrong thing entirely — the
    // workspace's one version is a DIFFERENT sha.
    mockList({ versions: [ROW("aaaaaaa", "one")] });
    vi.spyOn(versionsMod, "useVersion").mockReturnValue({
      data: null,
      isLoading: false,
      isError: true,
    } as never);
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=abc1234");
    expect(screen.queryByText(/only version this workspace has/i)).toBeNull();
    expect(screen.getByText(/not in the Versions list/i)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /clear the comparison/i })).toHaveLength(1);
  });

  it("still says 'pick two' in those same states when two versions DO exist", () => {
    // The paired half of the two tests above: the advice must not vanish for
    // the histories where it is followable.
    mockList({ versions: [ROW("bbbbbbb", "two"), ROW("aaaaaaa", "one")] });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=abc1234&b=aaaaaaa");
    expect(screen.getByText(/pick two/i)).toBeInTheDocument();
  });


  it("does not claim 'only one version' when the workspace has none", () => {
    // `canCompare` is false for BOTH zero and one. A shared link into an empty
    // workspace would otherwise render "No versions yet" and "only one
    // version" side by side, contradicting each other.
    mockList({ versions: [] });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=abc1234&b=aaaaaaa");
    expect(screen.queryByText(/only one version/i)).toBeNull();
  });

  it("still says 'pick two' when a TWO-version history fails to read one", () => {
    // The converse the round-1 tests missed: the failed-read branch, not just
    // the stale-link branch.
    mockList({ versions: [ROW("bbbbbbb", "two"), ROW("aaaaaaa", "one")] });
    vi.spyOn(versionsMod, "useVersion").mockReturnValue({
      data: null,
      isLoading: false,
      isError: true,
    } as never);
    // A stale THIRD sha, so this is the not-in-list branch: with two real
    // versions "pick two" is followable there, and no exit control is needed.
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=ccccccc&b=bbbbbbb");
    expect(screen.getByText(/pick two/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /clear the comparison/i })).toBeNull();
  });

  it("the single-read failure does not tell a one-version workspace to pick another", () => {
    mockList({ versions: [ROW("aaaaaaa", "one")] });
    vi.spyOn(versionsMod, "useVersion").mockReturnValue({
      data: null,
      isLoading: false,
      isError: true,
    } as never);
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa");
    expect(screen.getByText(/could not be read/i)).toBeInTheDocument();
    expect(screen.queryByText(/pick one from the Versions list/i)).toBeNull();
  });

  it("clearing the comparison empties BOTH shas from the URL", async () => {
    // The exit must actually work. Clicking the side pane cannot escape a stale
    // pair (pinned in historyNav.test.tsx), so this control is the only way out.
    mockList({ versions: [ROW("aaaaaaa", "one")] });
    mockVersion(null);
    wrapWithUrl(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=abc1234&b=aaaaaaa");
    await userEvent.click(screen.getByRole("button", { name: /clear the comparison/i }));
    const url = new URLSearchParams(screen.getByTestId("url").textContent ?? "");
    expect(url.get("a")).toBeNull();
    expect(url.get("b")).toBeNull();
    expect(url.get("tab")).toBe("history");
  });

  it("says a single version is loading instead of rendering nothing", () => {
    // Between the list landing and the payload arriving, this branch had no
    // message at all: the panel showed a header and blank space, which reads as
    // "there is nothing here" rather than "wait".
    mockList({ versions: [ROW("aaaaaaa", "one")] });
    vi.spyOn(versionsMod, "useVersion").mockReturnValue({
      data: null,
      isLoading: true,
      isError: false,
    } as never);
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa");
    expect(screen.getByText(/loading that version/i)).toBeInTheDocument();
  });

  it("says a comparison is loading once the list has already landed", () => {
    // The existing "Loading both versions…" is gated on the LIST loading. Once
    // the list is in hand and the two payloads are still in flight, nothing
    // rendered.
    mockList({ versions: [ROW("bbbbbbb", "two"), ROW("aaaaaaa", "one")] });
    vi.spyOn(versionsMod, "useVersion").mockReturnValue({
      data: null,
      isLoading: true,
      isError: false,
    } as never);
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb");
    expect(screen.getByText(/loading both versions/i)).toBeInTheDocument();
  });

  it("no message in this panel still says 'above' — scanned from source, not from one render", () => {
    // Rendering one state and asserting the absence of a phrase only ever
    // checks the branches that state happens to reach. This reads the
    // component source instead, so it covers every message in the file
    // including ones no test renders.
    // vitest runs from `frontend/`; import.meta.url is a transformed module
    // URL here, not a file: URL, so resolve from the project root instead.
    const src = readFileSync("src/cockpit/HistoryPanel.tsx", "utf8");
    expect(src).toContain("Version history"); // the read reached the real file
    expect(src).not.toMatch(/from the list above|from above|list above/i);
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
    // Asserting presence BEFORE reading innerHTML: a missing element yields
    // undefined on both sides and the comparison passes while proving nothing.
    const forwardEl = screen.getByTestId("side-by-side-diff");
    const forwardHtml = forwardEl.innerHTML;
    expect(forwardHtml).toContain("120");
    forward.unmount();

    mockPair("We recruit 40 patients.\n", "We recruit 120 patients.\n");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=bbbbbbb&b=aaaaaaa");
    const reverseHtml = screen.getByTestId("side-by-side-diff").innerHTML;

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
    // BOTH renderers, not just the unified one. `version-diff` identifies only
    // the unified view, and split is now the default — so checking it alone
    // would let a wrongly-ordered side-by-side diff render here and still pass.
    expect(document.querySelector("[data-testid='version-diff']")).toBeNull();
    expect(screen.queryByTestId("side-by-side-diff")).toBeNull();
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
    expect(screen.getByText(/not in the Versions list/i)).toBeInTheDocument();
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

  it("says which too-large pane is older and which is newer", () => {
    // Two bare <pre> blocks in a row carry no order. "Older on the left" lived
    // only in a source comment: sighted users had to infer it, and assistive
    // technology had nothing at all. The diff view labels its columns; this
    // fallback must too, or the note is the only thing the user can act on.
    const huge = Array(2001).fill("x").join("\n");
    mockPair(huge + "\nOLDMARKER", huge + "\nNEWMARKER");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb");

    const older = screen.getByTestId("version-side-header-older");
    const newer = screen.getByTestId("version-side-header-newer");
    expect(older.textContent).toMatch(/older/i);
    expect(older.textContent).toContain("aaaaaaa");
    expect(newer.textContent).toMatch(/newer/i);
    expect(newer.textContent).toContain("bbbbbbb");

    // The heading must be MACHINE-linked to its pane, not merely nearby: a
    // screen reader lands on the <pre>, not on the text above it.
    const panes = document.querySelectorAll("[data-testid='version-side']");
    expect(panes[0].getAttribute("aria-labelledby")).toBe(older.id);
    expect(panes[1].getAttribute("aria-labelledby")).toBe(newer.id);
    expect(older.id).not.toBe("");
    expect(older.id).not.toBe(newer.id);
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
    expect(screen.getByTestId("side-by-side-diff")).toBeInTheDocument();
  });

  it("renders the side-by-side view by default when two versions are selected", () => {
    mockPair("We recruit 40 patients.\n", "We recruit 120 patients.\n");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb");
    expect(screen.getByTestId("side-by-side-diff")).toBeInTheDocument();
    expect(document.querySelector("[data-testid='version-diff']")).toBeNull();
  });

  it("renders the unified view when the URL asks for it", () => {
    mockPair("We recruit 40 patients.\n", "We recruit 120 patients.\n");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb&layout=unified");
    expect(document.querySelector("[data-testid='version-diff']")).not.toBeNull();
    expect(screen.queryByTestId("side-by-side-diff")).toBeNull();
  });

  it("offers no layout control until two versions are selected", () => {
    mockPair("We recruit 40 patients.\n", "We recruit 120 patients.\n");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa");
    expect(screen.queryByRole("radio", { name: "Unified" })).toBeNull();
  });

  it("switches to unified when the control is used", async () => {
    const user = userEvent.setup();
    mockPair("We recruit 40 patients.\n", "We recruit 120 patients.\n");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb");
    await user.click(screen.getByRole("radio", { name: "Unified" }));
    await waitFor(() =>
      expect(document.querySelector("[data-testid='version-diff']")).not.toBeNull(),
    );
  });

  it("labels the left column older and the right column newer", () => {
    mockPair("We recruit 40 patients.\n", "We recruit 120 patients.\n");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb");
    // mockPair lists bbbbbbb first (newest-first), so aaaaaaa is the older one.
    expect(screen.getByTestId("side-header-older").textContent).toContain("aaaaaaa");
    expect(screen.getByTestId("side-header-newer").textContent).toContain("bbbbbbb");
  });

  it("offers no layout control for a comparison too large to diff", () => {
    const huge = Array(2001).fill("x").join("\n");
    mockPair(huge + "\nOLDMARKER", huge + "\nNEWMARKER");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb");
    expect(screen.getByText(/too large to compare/i)).toBeInTheDocument();
    // There is no diff to lay out, so a layout choice would be a dead control.
    expect(screen.queryByRole("radio", { name: "Unified" })).toBeNull();
  });

  it("does not describe the too-large fallback as if it were the feature", () => {
    const huge = Array(2001).fill("x").join("\n");
    mockPair(huge + "\nOLDMARKER", huge + "\nNEWMARKER");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb");
    const note = screen.getByText(/too large to compare/i);
    // The normal view IS side by side now, so the old copy described the
    // fallback as if it were the feature.
    expect(note.textContent).toContain("the full text of each is shown without highlighting");
    expect(note.textContent).not.toContain("shown side by side instead");
  });

  it("points a failed single read at the side pane, not at a list above", () => {
    // TWO versions, not one: this test's subject is WHERE the message points,
    // and pointing at the side pane is only correct when there is another row
    // to pick. The one-version wording has its own test above ("does not tell a
    // one-version workspace to pick another"). The "not above" half of this
    // assertion is now also enforced across the whole file by the source scan.
    mockList({ versions: [ROW("bbbbbbb", "newer"), ROW("aaaaaaa", "older")] });
    vi.spyOn(versionsMod, "useVersion").mockReturnValue({
      data: null,
      isLoading: false,
      isError: true,
    } as never);
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&b=bbbbbbb");
    const msg = screen.getByText(/could not be read/i);
    expect(msg.textContent).toMatch(/side pane/i);
    expect(msg.textContent).not.toMatch(/above/i);
  });

  it("points the not-in-this-list message at the side pane", () => {
    mockList({ versions: [ROW("bbbbbbb", "newer"), ROW("aaaaaaa", "older")] });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=abc1234&b=bbbbbbb");
    const msg = screen.getByText(/not in the Versions list/i);
    expect(msg.textContent).not.toMatch(/above/i);
  });
});
