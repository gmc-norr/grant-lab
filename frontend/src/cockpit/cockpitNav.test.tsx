import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { expect, it } from "vitest";

import { useCockpitNav } from "./cockpitNav";

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
