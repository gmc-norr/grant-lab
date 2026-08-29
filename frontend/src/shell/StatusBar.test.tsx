import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { resetEditorStore, setCursor, setSave } from "../cockpit/editorStore";
import { StatusBar } from "./StatusBar";

afterEach(() => {
  // resetEditorStore() emits to still-mounted store subscribers (this afterEach runs
  // before Testing-Library's auto-cleanup), so flush that re-render inside act().
  act(() => resetEditorStore());
  vi.restoreAllMocks();
});

beforeEach(() => {
  // Default no-network stub: both gated queries resolve to null → deadline/lock/band
  // absent. The "deadline/spec-lock/band" test below overrides this via mockImplementation.
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: null, error: undefined } as never);
});

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <StatusBar />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

it("off the cockpit, shows only the global label", () => {
  renderAt("/");
  expect(screen.getByText("Grant Lab")).toBeInTheDocument();
  expect(screen.queryByText("Markdown")).not.toBeInTheDocument();
});

it("on the cockpit, shows language + Ln/Col + save state", () => {
  setCursor({ line: 12, col: 4 });
  setSave("dirty");
  renderAt("/cockpit/ws-1");
  expect(screen.getByText("Markdown")).toBeInTheDocument();
  expect(screen.getByText("Ln 12, Col 4")).toBeInTheDocument();
  expect(screen.getByText("Unsaved")).toBeInTheDocument();
});

it("on the cockpit shows the deadline, spec-lock, and scorecard band", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/cockpit-meta"))
      return Promise.resolve({ data: { workspace_title: "W", workspace_deadline: "2099-01-01T00:00:00Z", current_phase: "active", role: "owner", spec_locked: true, member_count: 1 }, error: undefined });
    if (path.endsWith("/scorecard"))
      return Promise.resolve({ data: { scorecard: { criteria: [], overall_band: "adequate", summary: "" }, evaluated_draft_sha: null, evaluated_spec_sha: null, completed_at: null, stale: false, stale_reasons: [] }, error: undefined });
    return Promise.resolve({ data: null, error: undefined });
  }) as never);
  renderAt("/cockpit/ws-1");
  expect(await screen.findByText(/Spec locked/i)).toBeInTheDocument();
  expect(await screen.findByText(/Adequate/)).toBeInTheDocument();
  expect(screen.getByText(/2099-01-01/)).toBeInTheDocument();
});

it("off the cockpit it does NOT fetch cockpit-meta / scorecard", () => {
  const get = vi.spyOn(clientMod.api, "GET");
  renderAt("/");
  expect(get).not.toHaveBeenCalled();
});

it("does NOT fetch on the cockpit create route (no editor chrome)", () => {
  const get = vi.spyOn(clientMod.api, "GET");
  renderAt("/cockpit/new");
  expect(get).not.toHaveBeenCalled();
});

// What the middle is SHOWING you. The cockpit can display the live draft, one
// historical version, or two side by side, and until now the status bar said
// nothing — you could be reading a two-month-old version and not know it.
// (Branch is deliberately absent: every workspace repo is on `main` and nothing
// creates another, so a branch chip would be a permanent constant.)

function mockHead(sha: string) {
  vi.spyOn(clientMod.api, "GET").mockImplementation((async (path: string) => {
    if (String(path).endsWith("/draft") || String(path).endsWith("/spec")) {
      return { data: { head: sha, sections: [], can_edit: true }, error: undefined };
    }
    return { data: null, error: undefined };
  }) as never);
}

it("editing the draft names the artifact and the commit it is at", async () => {
  mockHead("a1b2c3d4e5f6");
  renderAt("/cockpit/ws-1?tab=draft");
  expect(await screen.findByText(/Draft · a1b2c3d/)).toBeInTheDocument();
  // Short sha, not the full 40 — the bar is one line.
  expect(screen.queryByText(/a1b2c3d4e5f6/)).toBeNull();
});

it("editing the spec says SPEC, not draft", async () => {
  mockHead("9999999999");
  renderAt("/cockpit/ws-1?tab=spec");
  expect(await screen.findByText(/Spec · 9999999/)).toBeInTheDocument();
});

it("viewing ONE version says viewing, and says read-only", () => {
  renderAt("/cockpit/ws-1?tab=diff&a=6d22146aaa");
  expect(screen.getByText(/Viewing 6d22146/)).toBeInTheDocument();
  expect(screen.getByText(/read-only/i)).toBeInTheDocument();
});

it("comparing TWO versions names both", () => {
  renderAt("/cockpit/ws-1?tab=diff&a=6d22146aaa&b=1340282bbb");
  const t = screen.getByText(/Comparing/).textContent ?? "";
  expect(t).toMatch(/6d22146/);
  expect(t).toMatch(/1340282/);
});

it("a legacy ?tab=history is the comparison too", () => {
  renderAt("/cockpit/ws-1?tab=history&a=6d22146aaa");
  expect(screen.getByText(/Viewing 6d22146/)).toBeInTheDocument();
});

it("off the cockpit it says nothing about commits", () => {
  renderAt("/");
  expect(screen.queryByText(/Viewing|Comparing|Draft ·|Spec ·/)).toBeNull();
});

it("does not claim a commit it has not got", async () => {
  // The draft query resolving to null must not render "Draft · undefined".
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: null, error: undefined } as never);
  renderAt("/cockpit/ws-1?tab=draft");
  expect(await screen.findByText("Markdown")).toBeInTheDocument();
  expect(screen.queryByText(/Draft ·/)).toBeNull();
});
