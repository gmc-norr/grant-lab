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

it("omits the match badge when the score is null", () => {
  renderRow(<CallRow call={call({ aggregate_score: null })} />);
  expect(screen.queryByText("82")).toBeNull();
});

it("Start drafting links to /cockpit/new with the call prefilled + profile", () => {
  renderRow(<CallRow call={call()} profileId={7} />);
  const href = screen.getByRole("link", { name: "Start drafting" }).getAttribute("href") ?? "";
  expect(href).toContain("/cockpit/new?");
  expect(href).toContain("call_id=3");
  expect(href).toContain("call_title=Grant+X"); // URLSearchParams encodes space as +
  expect(href).toContain("profile_id=7");
});

it("omits profile_id from the Start-drafting link when there is no resolved profile", () => {
  renderRow(<CallRow call={call()} />);
  const href = screen.getByRole("link", { name: "Start drafting" }).getAttribute("href") ?? "";
  expect(href).not.toContain("profile_id");
});
