import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { useCockpitNav } from "./cockpitNav";
import { resetEditorStore, setDirty } from "./editorStore";

function Probe() {
  const nav = useCockpitNav();
  const loc = useLocation();
  return (
    <div>
      <span data-testid="tab">{nav.tab}</span>
      <span data-testid="sec">{nav.sec}</span>
      <span data-testid="search">{loc.search}</span>
      <button onClick={() => nav.setTab("spec")}>setTab</button>
      <button onClick={() => nav.setSec(3)}>setSec</button>
      <button onClick={() => nav.navigate({ top_tab: "draft", section_index: 2 })}>nav</button>
    </div>
  );
}
function renderAt(url: string) {
  return render(<MemoryRouter initialEntries={[url]}><Probe /></MemoryRouter>);
}

it("defaults to draft / 0 with no params", () => {
  renderAt("/cockpit/x");
  expect(screen.getByTestId("tab").textContent).toBe("draft");
  expect(screen.getByTestId("sec").textContent).toBe("0");
});
it("reads ?tab and ?sec", () => {
  renderAt("/cockpit/x?tab=spec&sec=2");
  expect(screen.getByTestId("tab").textContent).toBe("spec");
  expect(screen.getByTestId("sec").textContent).toBe("2");
});
it("falls back on an invalid tab / non-numeric sec", () => {
  renderAt("/cockpit/x?tab=bogus&sec=abc");
  expect(screen.getByTestId("tab").textContent).toBe("draft");
  expect(screen.getByTestId("sec").textContent).toBe("0");
});
it("setTab writes ?tab", () => {
  renderAt("/cockpit/x");
  fireEvent.click(screen.getByText("setTab"));
  expect(screen.getByTestId("tab").textContent).toBe("spec");
  expect(screen.getByTestId("search").textContent).toContain("tab=spec");
});
it("setSec writes ?sec", () => {
  renderAt("/cockpit/x");
  fireEvent.click(screen.getByText("setSec"));
  expect(screen.getByTestId("sec").textContent).toBe("3");
});
it("navigate writes ?tab and ?sec from a target (section_index)", () => {
  renderAt("/cockpit/x?tab=scorecard");
  fireEvent.click(screen.getByText("nav"));
  expect(screen.getByTestId("tab").textContent).toBe("draft");
  expect(screen.getByTestId("sec").textContent).toBe("2");
});

import { cockpitIdFromPath } from "./cockpitNav";

it("cockpitIdFromPath returns the id only for the editor route", () => {
  expect(cockpitIdFromPath("/cockpit/abc")).toBe("abc");
  expect(cockpitIdFromPath("/cockpit/abc/sub")).toBe("abc");
  expect(cockpitIdFromPath("/cockpit/new")).toBe(""); // create route, NOT an id
  expect(cockpitIdFromPath("/cockpit")).toBe(""); // list route
  expect(cockpitIdFromPath("/")).toBe(""); // off cockpit
});

// --- navigate() and the unsaved-editor guard ---------------------------------
//
// Changing ?tab unmounts the artifact editor and discards its per-section
// buffers. Cockpit.onTab confirms before that discard, but navigate() is a
// SECOND writer of ?tab and was silent: measured at zero window.confirm calls
// while the editor was dirty, with the URL moving to tab=scorecard. Reached
// from every Criteria row (CockpitSidebar.tsx:127).

function GuardProbe({ to }: { to: "scorecard" | "draft" }) {
  const nav = useCockpitNav();
  const loc = useLocation();
  return (
    <>
      <button onClick={() => nav.navigate({ top_tab: to, section_index: 2 })}>go</button>
      <span data-testid="search2">{loc.search}</span>
    </>
  );
}

function clickGo(url: string, to: "scorecard" | "draft") {
  render(
    <MemoryRouter initialEntries={[url]}>
      <GuardProbe to={to} />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByText("go"));
  return new URLSearchParams(screen.getByTestId("search2").textContent ?? "");
}

afterEach(() => {
  act(() => resetEditorStore());
  vi.restoreAllMocks();
});

it("navigate() asks before discarding unsaved edits, and does nothing if refused", () => {
  act(() => setDirty([1]));
  const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
  const sp = clickGo("/cockpit/ws1?tab=draft&sec=1", "scorecard");
  expect(confirmSpy).toHaveBeenCalledTimes(1);
  expect(sp.get("tab")).toBe("draft");
  // The section must not move either: a refused discard changes nothing.
  expect(sp.get("sec")).toBe("1");
});

it("navigate() proceeds when the discard is accepted", () => {
  act(() => setDirty([1]));
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const sp = clickGo("/cockpit/ws1?tab=draft&sec=1", "scorecard");
  expect(sp.get("tab")).toBe("scorecard");
  expect(sp.get("sec")).toBe("2");
});

it("navigate() does NOT ask for a jump within the SAME tab", () => {
  // An Outline row on the tab it belongs to moves the section without
  // unmounting anything. Prompting there would train the user to dismiss the
  // prompt that matters.
  act(() => setDirty([1]));
  const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
  const sp = clickGo("/cockpit/ws1?tab=draft&sec=1", "draft");
  expect(confirmSpy).not.toHaveBeenCalled();
  expect(sp.get("sec")).toBe("2");
});

it("navigate() does NOT ask when the editor is clean", () => {
  const confirmSpy = vi.spyOn(window, "confirm");
  const sp = clickGo("/cockpit/ws1?tab=draft", "scorecard");
  expect(confirmSpy).not.toHaveBeenCalled();
  expect(sp.get("tab")).toBe("scorecard");
});

import { VIEWS, viewFromParams, viewHref } from "./cockpitNav";

const sp = (q: string) => new URLSearchParams(q);

it("parses every valid view", () => {
  for (const v of VIEWS) expect(viewFromParams(sp(`view=${v}`))).toBe(v);
  expect(VIEWS).toEqual(["outline", "versions", "references", "criteria", "members"]);
});

it("normalises an unrecognised view instead of erroring", () => {
  // Matches tabFromParams and artifactParam: a URL is user input, and a typo
  // shows the default rather than an error.
  expect(viewFromParams(sp("view=banana"))).toBe("outline");
  expect(viewFromParams(sp(""))).toBe("outline");
});

it("defaults to versions when the middle is showing the comparison", () => {
  // Otherwise a bare ?tab=history renders "pick two versions from the Versions
  // list in the side pane" beside the OUTLINE — the defect this design removes,
  // rebuilt by a default. Slice 2 adds "diff" to the same set.
  expect(viewFromParams(sp("tab=history"))).toBe("versions");
  expect(viewFromParams(sp("tab=draft"))).toBe("outline");
});

it("an explicit view always wins over the comparison default", () => {
  expect(viewFromParams(sp("tab=history&view=criteria"))).toBe("criteria");
});

it("viewHref changes ONLY view and keeps every other parameter", () => {
  // Seeded with every parameter the merge protects. Seeding with two of them
  // passes on an implementation that preserves those two and drops the rest,
  // and asserting only that view=versions is present passes on a plain
  // <Link to="?view=versions">, which REPLACES the search string.
  const start = sp(
    "tab=scorecard&sec=3&artifact=spec&a=aaaaaaa&b=bbbbbbb&layout=unified&view=outline",
  );
  const href = viewHref(start, "versions", "/cockpit/ws-1");
  expect(href.pathname).toBe("/cockpit/ws-1");
  const out = sp(href.search);
  expect(out.get("view")).toBe("versions");
  expect(out.get("tab")).toBe("scorecard");
  expect(out.get("sec")).toBe("3");
  expect(out.get("artifact")).toBe("spec");
  expect(out.get("a")).toBe("aaaaaaa");
  expect(out.get("b")).toBe("bbbbbbb");
  expect(out.get("layout")).toBe("unified");
});

it("viewHref does not mutate the params it was given", () => {
  const start = sp("tab=draft&view=outline");
  viewHref(start, "members", "/cockpit/ws-1");
  expect(start.get("view")).toBe("outline");
});
