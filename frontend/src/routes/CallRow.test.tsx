// frontend/src/routes/CallRow.test.tsx
import { MantineProvider } from "@mantine/core";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { expect, it } from "vitest";

import type { CallItem } from "../api/dashboard";
import { theme } from "../theme/theme";
import { CallRow } from "./CallRow";

function call(over: Partial<CallItem> = {}): CallItem {
  return {
    id: 3,
    title: "Grant X",
    source_name: "Vinnova",
    source_slug: "vinnova",
    deadline: null,
    published_at: null,
    aggregate_score: 82,
    ...over,
  } as CallItem;
}

function renderRow(ui: React.ReactElement, url = "/") {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter initialEntries={[url]}>{ui}</MemoryRouter>
    </MantineProvider>,
  );
}

it("links the title to the reading-pane selection URL, shows source + match badge", () => {
  renderRow(<CallRow call={call()} />);
  const href = screen.getByRole("link", { name: "Grant X" }).getAttribute("href") ?? "";
  expect(href).toContain("selected=3");
  expect(href).not.toContain("profile=");
  expect(screen.getByText("Vinnova")).toBeInTheDocument();
  expect(screen.getByText("82")).toHaveAttribute("data-band", "strong");
});

it("pins the resolved profile into the selection URL", () => {
  renderRow(<CallRow call={call()} profileId={7} />);
  const href = screen.getByRole("link", { name: "Grant X" }).getAttribute("href") ?? "";
  expect(href).toContain("selected=3");
  expect(href).toContain("profile=7");
});

it("preserves existing list filters/pagination in the selection URL", () => {
  // Guards the core selectionSearch contract at the CallRow level: a row that
  // forgot to pass the live `sp` would still pass the helper unit tests.
  renderRow(<CallRow call={call()} profileId={7} />, "/?source=eu&page=2&q=dna");
  const href = screen.getByRole("link", { name: "Grant X" }).getAttribute("href") ?? "";
  expect(href).toContain("selected=3");
  expect(href).toContain("source=eu");
  expect(href).toContain("page=2");
  expect(href).toContain("q=dna");
  expect(href).toContain("profile=7");
});

it("marks the row as current when it is the selected call", () => {
  renderRow(<CallRow call={call()} />, "/?selected=3");
  expect(screen.getByRole("listitem")).toHaveAttribute("aria-current", "true");
});

it("is not current when a different call is selected", () => {
  renderRow(<CallRow call={call()} />, "/?selected=99");
  expect(screen.getByRole("listitem")).not.toHaveAttribute("aria-current");
});

it("clicking empty row area selects the call", () => {
  function Probe() {
    return <span data-testid="search">{useLocation().search}</span>;
  }
  renderRow(
    <Routes>
      <Route path="/" element={<><CallRow call={call()} profileId={7} /><Probe /></>} />
    </Routes>,
  );
  fireEvent.click(screen.getByRole("listitem"));
  expect(screen.getByTestId("search").textContent).toContain("selected=3");
  expect(screen.getByTestId("search").textContent).toContain("profile=7");
});

it("the row's only interactive child is the title — Start drafting is gone", () => {
  // INVERTED, not deleted (release-1 edition split). This used to click the
  // Start-drafting anchor and prove the row's `closest("a,button")` guard sent
  // you to /cockpit/new instead of selecting the row. That anchor is removed, so
  // what stays pinned is its ABSENCE plus the count: re-adding any second link
  // fails the length assertion even if it is renamed.
  //
  // COVERAGE NOTE: the guard itself is no longer observable by destination. The
  // one remaining interactive child is the title anchor, whose href IS the
  // selection URL the row onClick would navigate to, so removing the guard
  // changes nothing a test can see. The guard is kept because a future second
  // link (release 2 re-adds drafting) needs it.
  function Probe() {
    const loc = useLocation();
    return <span data-testid="loc">{loc.pathname + loc.search}</span>;
  }
  renderRow(
    <>
      <CallRow call={call()} profileId={7} />
      <Probe />
    </>,
  );
  expect(screen.queryByRole("link", { name: /start drafting/i })).not.toBeInTheDocument();
  const links = screen.getAllByRole("link");
  expect(links).toHaveLength(1);
  expect(links[0]).toHaveAccessibleName("Grant X");
  fireEvent.click(links[0]);
  expect(screen.getByTestId("loc").textContent ?? "").not.toContain("/cockpit");
});

it("omits the match badge when the score is null", () => {
  renderRow(<CallRow call={call({ aggregate_score: null })} />);
  expect(screen.queryByText("82")).toBeNull();
});

it("emits NO link into /cockpit — the Start-drafting entry point is removed", () => {
  // INVERTED from "Start drafting links to /cockpit/new with the call prefilled
  // + profile". Release 1 does not mount /cockpit/new, so that anchor would have
  // shipped as a 404 button. Asserting on the NAME alone would pass on a row that
  // renamed the link, so every rendered href is checked too.
  renderRow(<CallRow call={call()} profileId={7} />);
  expect(screen.queryByRole("link", { name: /start drafting/i })).not.toBeInTheDocument();
  for (const a of screen.getAllByRole("link")) {
    expect(a.getAttribute("href") ?? "").not.toContain("/cockpit");
  }
});

it("emits no /cockpit link when there is no resolved profile either", () => {
  // INVERTED from "omits profile_id from the Start-drafting link when there is no
  // resolved profile". The no-profile branch built a DIFFERENT href, so it is
  // pinned separately: a partial revert that restored only that branch would pass
  // the test above.
  renderRow(<CallRow call={call()} />);
  expect(screen.queryByRole("link", { name: /start drafting/i })).not.toBeInTheDocument();
  for (const a of screen.getAllByRole("link")) {
    expect(a.getAttribute("href") ?? "").not.toContain("/cockpit");
  }
});
