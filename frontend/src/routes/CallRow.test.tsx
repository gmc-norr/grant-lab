import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
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

function renderRow(ui: React.ReactElement) {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter>{ui}</MemoryRouter>
    </MantineProvider>,
  );
}

it("links the title to the React detail route, shows source + match badge", () => {
  renderRow(<CallRow call={call()} />);
  expect(screen.getByRole("link", { name: "Grant X" })).toHaveAttribute("href", "/calls/3");
  expect(screen.getByText("Vinnova")).toBeInTheDocument();
  expect(screen.getByText("82")).toHaveAttribute("data-band", "strong");
});

it("threads the resolved profile into the detail link", () => {
  renderRow(<CallRow call={call()} profileId={7} />);
  expect(screen.getByRole("link", { name: "Grant X" })).toHaveAttribute(
    "href",
    "/calls/3?profile=7",
  );
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
