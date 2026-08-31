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
  // resetEditorStore() emits to still-mounted store subscribers (this afterEach runs
  // before Testing-Library's auto-cleanup), so flush that re-render inside act().
  act(() => resetEditorStore());
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

it("renders the References pane with a citation", async () => {
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
  renderSidebar("/cockpit/ws-1?view=references");
  expect(await screen.findByText("Smith 2021")).toBeInTheDocument();
});

it("renders the Criteria pane with a band pill", async () => {
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
  renderSidebar("/cockpit/ws-1?view=criteria");
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
  renderSidebar("/cockpit/ws-1?view=criteria");
  fireEvent.click(await screen.findByRole("button", { name: /Impact/ }));
  expect(screen.getByTestId("search").textContent).toContain("tab=scorecard");
});

it("owner sees the Versions commits", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.includes("/history"))
      return Promise.resolve({
        data: {
          versions: [
            {
              commit_sha: "a".repeat(40),
              committed_at: "2026-06-24T00:00:00Z",
              subject: "user spec init",
              // The real /history always sends this; a mock that omits it is
              // not the API, and assignLanes reads it.
              parents: [],
              created_by: "user",
              triggering_turn_id: null,
              turn_prompt: null,
            },
          ],
          truncated: false,
          history_available: true,
        },
        error: undefined,
      });
    if (path.endsWith("/members"))
      return Promise.resolve({ data: [{ email: "owner@ki.se", role: "owner" }, { email: "m@ki.se", role: "member" }], error: undefined });
    if (path.endsWith("/cockpit-meta"))
      return Promise.resolve({ data: { workspace_title: "W", workspace_deadline: null, current_phase: "active", role: "owner", spec_locked: false, member_count: 2 }, error: undefined });
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/scorecard")) return Promise.resolve({ data: null, error: undefined });
    return Promise.resolve(draft as never);
  }) as never);
  renderSidebar("/cockpit/ws-1?view=versions");
  expect(await screen.findByText("user spec init")).toBeInTheDocument();
});

it("owner sees the named Members list", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.includes("/history"))
      return Promise.resolve({
        data: {
          versions: [],
          truncated: false,
          history_available: true,
        },
        error: undefined,
      });
    if (path.endsWith("/members"))
      return Promise.resolve({ data: [{ email: "owner@ki.se", role: "owner" }, { email: "m@ki.se", role: "member" }], error: undefined });
    if (path.endsWith("/cockpit-meta"))
      return Promise.resolve({ data: { workspace_title: "W", workspace_deadline: null, current_phase: "active", role: "owner", spec_locked: false, member_count: 2 }, error: undefined });
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/scorecard")) return Promise.resolve({ data: null, error: undefined });
    return Promise.resolve(draft as never);
  }) as never);
  renderSidebar("/cockpit/ws-1?view=members");
  expect(await screen.findByText("owner@ki.se")).toBeInTheDocument();
});

it("a non-owner member sees the member count only (no emails)", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/members"))
      return Promise.resolve({ data: undefined, error: { error: { code: "forbidden", message: "owner only" } } });
    if (path.endsWith("/cockpit-meta"))
      return Promise.resolve({ data: { workspace_title: "W", workspace_deadline: null, current_phase: "active", role: "member", spec_locked: false, member_count: 3 }, error: undefined });
    if (path.includes("/history"))
      return Promise.resolve({
        data: { versions: [], truncated: false, history_available: true },
        error: undefined,
      });
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/scorecard")) return Promise.resolve({ data: null, error: undefined });
    return Promise.resolve(draft as never);
  }) as never);
  renderSidebar("/cockpit/ws-1?view=members");
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
    if (path.includes("/history"))
      return Promise.resolve({
        data: { versions: [], truncated: false, history_available: true },
        error: undefined,
      });
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/scorecard")) return Promise.resolve({ data: null, error: undefined });
    return Promise.resolve(draft as never);
  }) as never);
  vi.spyOn(window, "confirm").mockReturnValue(true);
  renderSidebar("/cockpit/ws-1?view=members");
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
    if (path.includes("/history"))
      return Promise.resolve({
        data: { versions: [], truncated: false, history_available: true },
        error: undefined,
      });
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/scorecard")) return Promise.resolve({ data: null, error: undefined });
    return Promise.resolve(draft as never);
  }) as never);
  renderSidebar("/cockpit/ws-1?view=criteria");
  fireEvent.click(await screen.findByRole("button", { name: /edit rubric/i }));
  fireEvent.click(await screen.findByRole("button", { name: /save/i }));
  expect(rb.update).toHaveBeenCalled();
});

it("renders the version GRAPH, not the old plain list", async () => {
  // Must assert something only the graph produces. "No versions yet." is NOT
  // that: the old sidebar renders those exact words too (CockpitSidebar.tsx:139),
  // so an empty-list assertion passes either way and proves nothing.
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.includes("/history"))
      return Promise.resolve({
        data: {
          versions: [
            {
              commit_sha: "abcdef1234567890abcdef1234567890abcdef12",
              committed_at: "2026-06-24T00:00:00Z",
              subject: "user spec init",
              // The real /history always sends this; assignLanes reads it.
              parents: [],
              created_by: "agent",
              triggering_turn_id: null,
              turn_prompt: null,
            },
          ],
          truncated: false,
          history_available: true,
        },
        error: undefined,
      });
    if (path.endsWith("/members")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/cockpit-meta"))
      return Promise.resolve({ data: { workspace_title: "W", workspace_deadline: null, current_phase: "active", role: "owner", spec_locked: false, member_count: 1 }, error: undefined });
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/scorecard")) return Promise.resolve({ data: null, error: undefined });
    return Promise.resolve(draft as never);
  }) as never);
  renderSidebar("/cockpit/ws-1?view=versions");
  // Graph-only output: an SVG lane strip with a dot in it, plus the row's
  // accessible name carrying the 7-char sha. The old list rendered neither — it
  // showed the full message and the role as plain text, in no SVG at all.
  //
  // The sha is no longer PRINTED in the row (it left about 60px for the subject
  // at 276px), so asserting visible text would now be asserting the old design.
  expect(await screen.findByRole("button", { name: /^abcdef1 / })).toBeInTheDocument();
  expect(document.querySelector('svg circle[data-role="agent"]')).not.toBeNull();
});

it("renders ONLY the view the URL asks for", async () => {
  // Asserting the OTHERS are absent on ?view=outline would prove nothing:
  // SidebarGroup mounts children only when open (SidebarGroup.tsx:44) and
  // References, Criteria, Members and Versions are all defaultOpen={false}, so
  // those lists are already absent under the accordion. The gate has to name a
  // NON-default view and assert the default one is gone.
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draft as never);
  renderSidebar("/cockpit/ws-1?view=members");
  expect(await screen.findByRole("heading", { name: /members/i })).toBeInTheDocument();
  // Outline is the group the accordion leaves OPEN, so its absence is the
  // assertion the accordion cannot satisfy.
  expect(screen.queryByRole("list", { name: "Outline" })).toBeNull();
  expect(screen.queryByRole("button", { name: /Methods/ })).toBeNull();
  // References is named EXPLICITLY because Step 10 mutates ReferencesPane to
  // unconditional and expects THIS test to go red. Without this line that
  // mutation is caught only by `heads EVERY pane` — a different test than the
  // one the mutation step names.
  expect(screen.queryByRole("list", { name: "References" })).toBeNull();
});

it("heads EVERY pane with its own name, and shows no other", async () => {
  // Parameterised: checking one pane's heading lets a missing or wrong heading
  // on the other four pass.
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draft as never);
  const pairs = [
    ["outline", /outline/i],
    ["versions", /versions/i],
    ["references", /references/i],
    ["criteria", /criteria/i],
    ["members", /members/i],
  ] as const;
  for (const [view, title] of pairs) {
    const { unmount } = renderSidebar(`/cockpit/ws-1?view=${view}`);
    const headings = await screen.findAllByRole("heading");
    expect(headings).toHaveLength(1);
    expect(headings[0].textContent ?? "").toMatch(title);
    unmount();
  }
});

it("has no collapsible group chrome left", async () => {
  // SidebarGroup rendered a header BUTTON with aria-expanded. One view at a
  // time needs no toggle, and leaving one invites the folded-below-the-fold
  // problem back.
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draft as never);
  const { container } = renderSidebar("/cockpit/ws-1?view=outline");
  await screen.findByRole("button", { name: /Methods/ });
  expect(container.querySelectorAll("[aria-expanded]")).toHaveLength(0);
});

