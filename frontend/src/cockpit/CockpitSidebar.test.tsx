import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { theme } from "../theme/theme";

// vi.mock is hoisted above top-level consts, so the factory must reference a
// vi.hoisted() object (a plain `const addMutate = vi.fn()` here trips
// "cannot access before initialization"). Reset these in beforeEach.
const m = vi.hoisted(() => ({ addMutate: vi.fn(), removeMutate: vi.fn() }));
vi.mock("../api/settings", () => ({
  useAddMember: () => ({ mutate: m.addMutate, isPending: false, isError: false }),
  useRemoveMember: () => ({ mutate: m.removeMutate, isPending: false }),
}));

const rb = vi.hoisted(() => ({
  update: vi.fn(),
  // Stable reference so the useEffect([editOpen, rubric.data]) dependency doesn't
  // fire on every render (a new object literal would cause an infinite loop).
  rubricData: { criteria: [{ name: "Impact", description: "d", weight: 1, scale_max: 5 }], funder_notes: "", updated_at: "x" },
}));
vi.mock("../api/rubric", () => ({
  useUpdateRubric: () => ({ mutate: rb.update, isPending: false, isError: false }),
  useRubric: () => ({ data: rb.rubricData, isLoading: false }),
}));

import * as clientMod from "../api/client";
import { CockpitSidebar } from "./CockpitSidebar";
import { resetEditorStore, setCursor, setDirty, useEditorDirty } from "./editorStore";

beforeEach(() => {
  m.addMutate.mockReset();
  m.removeMutate.mockReset();
  rb.update.mockReset();
});

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
    <MantineProvider theme={theme} defaultColorScheme="dark">
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
      </QueryClientProvider>
    </MantineProvider>,
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
  act(() => setDirty([1]));
  expect(await screen.findByLabelText("unsaved")).toBeInTheDocument();
});

it("a caret-only store update does NOT re-render a dirty subscriber (Defect 4)", () => {
  // The sidebar subscribes to ONLY the dirty set via useEditorDirty, so a caret move
  // (cursor updates on EVERY keystroke) must not re-render it. We exercise the exact
  // subscription the sidebar uses and count renders directly.
  let renders = 0;
  function DirtyProbe() {
    renders += 1;
    const dirty = useEditorDirty();
    return <span data-testid="dirty">{dirty.join(",")}</span>;
  }
  render(<DirtyProbe />);
  const before = renders;
  act(() => setCursor({ line: 3, col: 7 }));
  act(() => setCursor({ line: 9, col: 2 }));
  expect(renders).toBe(before); // caret-only updates -> no re-render
  // A genuine dirty change DOES re-render it.
  act(() => setDirty([1]));
  expect(renders).toBe(before + 1);
  expect(screen.getByTestId("dirty").textContent).toBe("1");
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

it("owner sees the named Members list and the Versions commits", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/versions"))
      return Promise.resolve({ data: [{ commit_sha: "a".repeat(40), message: "user spec init", created_by: "user", created_at: "2026-06-24T00:00:00Z" }], error: undefined });
    if (path.endsWith("/members"))
      return Promise.resolve({ data: [{ email: "owner@ki.se", role: "owner" }, { email: "m@ki.se", role: "member" }], error: undefined });
    if (path.endsWith("/cockpit-meta"))
      return Promise.resolve({ data: { workspace_title: "W", workspace_deadline: null, current_phase: "active", role: "owner", spec_locked: false, member_count: 2 }, error: undefined });
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/scorecard")) return Promise.resolve({ data: null, error: undefined });
    return Promise.resolve(draft as never);
  }) as never);
  renderSidebar();
  fireEvent.click(await screen.findByRole("button", { name: /versions/i }));
  expect(await screen.findByText("user spec init")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /members/i }));
  expect(await screen.findByText("owner@ki.se")).toBeInTheDocument();
});

it("a non-owner member sees the member count only (no emails)", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/members"))
      return Promise.resolve({ data: undefined, error: { error: { code: "forbidden", message: "owner only" } } });
    if (path.endsWith("/cockpit-meta"))
      return Promise.resolve({ data: { workspace_title: "W", workspace_deadline: null, current_phase: "active", role: "member", spec_locked: false, member_count: 3 }, error: undefined });
    if (path.endsWith("/versions")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/scorecard")) return Promise.resolve({ data: null, error: undefined });
    return Promise.resolve(draft as never);
  }) as never);
  renderSidebar();
  fireEvent.click(await screen.findByRole("button", { name: /members/i }));
  expect(await screen.findByText(/3 members/)).toBeInTheDocument();
  expect(screen.queryByText("owner@ki.se")).toBeNull();
});

it("owner can remove a viewer (confirm-gated) from the Members group", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/members"))
      return Promise.resolve({
        data: [
          { user_id: 1, email: "owner@ki.se", role: "owner" },
          { user_id: 2, email: "viewer@ki.se", role: "member" },
        ],
        error: undefined,
      });
    if (path.endsWith("/cockpit-meta"))
      return Promise.resolve({
        data: { workspace_title: "W", workspace_deadline: null, current_phase: "active", role: "owner", spec_locked: false, member_count: 2 },
        error: undefined,
      });
    if (path.endsWith("/versions")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/scorecard")) return Promise.resolve({ data: null, error: undefined });
    return Promise.resolve(draft as never);
  }) as never);
  vi.spyOn(window, "confirm").mockReturnValue(true);
  renderSidebar();
  // The Members group is defaultOpen={false} — open it before the rows render:
  fireEvent.click(await screen.findByRole("button", { name: /members/i }));
  fireEvent.click(await screen.findByRole("button", { name: /remove viewer@ki\.se/i }));
  expect(m.removeMutate).toHaveBeenCalledWith(2);
  // owner row has NO remove control:
  expect(screen.queryByRole("button", { name: /remove owner@ki\.se/i })).toBeNull();
});

it("owner can open Edit rubric and save (active phase)", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/cockpit-meta"))
      return Promise.resolve({ data: { workspace_title: "W", workspace_deadline: null, current_phase: "active", role: "owner", spec_locked: false, member_count: 1 }, error: undefined });
    if (path.endsWith("/members"))
      return Promise.resolve({ data: [{ user_id: 1, email: "owner@ki.se", role: "owner" }], error: undefined });
    if (path.endsWith("/versions")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/scorecard")) return Promise.resolve({ data: null, error: undefined });
    return Promise.resolve(draft as never);
  }) as never);
  renderSidebar();
  fireEvent.click(await screen.findByRole("button", { name: /criteria/i })); // open the group
  fireEvent.click(await screen.findByRole("button", { name: /edit rubric/i }));
  fireEvent.click(await screen.findByRole("button", { name: /save/i }));
  expect(rb.update).toHaveBeenCalled();
});
