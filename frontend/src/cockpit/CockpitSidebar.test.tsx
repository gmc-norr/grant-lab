import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { CockpitSidebar } from "./CockpitSidebar";
import { resetEditorStore, setDirty } from "./editorStore";

afterEach(() => {
  vi.restoreAllMocks();
  resetEditorStore();
});

const draft = {
  data: {
    head: "a".repeat(40),
    can_edit: true,
    sections: [
      { index: 0, heading: "", text: "preamble\n", sha: "s0" },
      { index: 1, heading: "Methods", text: "## Methods\n", sha: "s1" },
    ],
    citations: {},
  },
  error: undefined,
};

function Probe() {
  return <span data-testid="search">{useLocation().search}</span>;
}
function renderSidebar(url = "/cockpit/ws-1") {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route
            path="/cockpit/:id"
            element={
              <>
                <CockpitSidebar />
                <Probe />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

it("renders the Outline sections", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draft as never);
  renderSidebar();
  expect(await screen.findByRole("button", { name: /Methods/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /preamble/i })).toBeInTheDocument();
});

it("clicking a section navigates to ?tab=draft&sec=N", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draft as never);
  renderSidebar();
  fireEvent.click(await screen.findByRole("button", { name: /Methods/ }));
  expect(screen.getByTestId("search").textContent).toContain("tab=draft");
  expect(screen.getByTestId("search").textContent).toContain("sec=1");
});

it("shows a dirty marker for a section the store reports", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draft as never);
  renderSidebar();
  await screen.findByRole("button", { name: /Methods/ });
  setDirty([1]);
  expect(await screen.findByLabelText("unsaved")).toBeInTheDocument();
});

it("renders References and Criteria groups with a band pill", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/references"))
      return Promise.resolve({ data: [{ id: "r1", citekey: "smith2021", title: "Smith 2021", kind: "paper", text_status: "done" }], error: undefined });
    if (path.endsWith("/scorecard"))
      return Promise.resolve({
        data: { scorecard: { criteria: [{ index: 0, name: "Impact", band: "gap", note: "", deep_link: null, weight: 1, effective_weight: 1 }], overall_band: "gap", summary: "" }, evaluated_draft_sha: null, evaluated_spec_sha: null, completed_at: null, stale: false, stale_reasons: [] },
        error: undefined,
      });
    return Promise.resolve(draft as never); // /draft + /spec
  }) as never);
  renderSidebar();
  // Open the (default-collapsed) groups.
  fireEvent.click(await screen.findByRole("button", { name: /references/i }));
  expect(await screen.findByText("Smith 2021")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /criteria/i }));
  expect(await screen.findByText("Impact")).toBeInTheDocument();
  expect(screen.getByText("Gap")).toBeInTheDocument();
});

it("clicking a criterion navigates to the Scorecard tab", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/scorecard"))
      return Promise.resolve({
        data: { scorecard: { criteria: [{ index: 0, name: "Impact", band: "gap", note: "", deep_link: null, weight: 1, effective_weight: 1 }], overall_band: "gap", summary: "" }, evaluated_draft_sha: null, evaluated_spec_sha: null, completed_at: null, stale: false, stale_reasons: [] },
        error: undefined,
      });
    return Promise.resolve(draft as never);
  }) as never);
  renderSidebar();
  fireEvent.click(await screen.findByRole("button", { name: /criteria/i }));
  fireEvent.click(await screen.findByRole("button", { name: /Impact/ }));
  expect(screen.getByTestId("search").textContent).toContain("tab=scorecard");
});
